import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from automation.models import AutomationJob
from automation.tenant_context import database_org_context
from common.models import Org, Profile
from matching.import_pipeline import (
    PersonImportServiceError,
    commit_person_import,
    execute_person_import,
    expire_stale_import_previews,
    preview_person_import,
)
from matching.models import (
    Person,
    PersonIdentity,
    PersonIdentityObservation,
    PersonImportBatch,
    PersonImportConflict,
    PersonImportDecision,
    PersonImportImpact,
    PersonImportRecord,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS, triggers, and row-level concurrency are required.",
    ),
    pytest.mark.django_db(transaction=True),
]


CHILD_ORG_GUARD_MESSAGE = "matching import child organization mismatch"
APPEND_ONLY_MESSAGE = "matching import decision is append-only"
CSV_BYTES = b"Name,Email\nAlice Example,ALICE@example.com\n"
MAPPING = {"display_name": "Name", "email": "Email"}


@contextmanager
def _empty_database_org_context():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_org', true)")
        previous = cursor.fetchone()[0] or ""
        cursor.execute("SELECT set_config('app.current_org', '', false)")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_org', %s, false)", [previous]
            )


def _make_graph(*, org, profile, suffix):
    person = Person.objects.create(org=org, display_name=f"Person {suffix}")
    identity = PersonIdentity.objects.create(
        org=org,
        person=person,
        kind="email",
        normalized_value=f"person-{suffix}@example.test",
        display_value=f"person-{suffix}@example.test",
        source="manual",
    )
    job = AutomationJob.objects.create(
        org=org,
        name="matching.import_people",
        idempotency_key=f"import-{suffix}",
        scheduled_for=timezone.now(),
    )
    batch = PersonImportBatch.objects.create(
        org=org,
        requested_by=profile,
        automation_job=job,
        idempotency_key=uuid4(),
        request_hash=("a" if suffix == "a" else "b") * 64,
        commit_idempotency_key=uuid4(),
        commit_request_hash=("c" if suffix == "a" else "d") * 64,
        content_hash=("e" if suffix == "a" else "f") * 64,
        original_filename=f"people-{suffix}.csv",
        file_size=10,
        mapping=MAPPING,
        status="partial",
        total_count=1,
        processed_count=1,
        conflict_count=1,
    )
    record = PersonImportRecord.objects.create(
        org=org,
        batch=batch,
        row_number=2,
        row_hash=("1" if suffix == "a" else "2") * 64,
        source_record_id=f"record-{suffix}",
        display_name=person.display_name,
        normalized_payload={"display_name": person.display_name},
        masked_identities=[{"kind": "email", "value": "p***@example.test"}],
        status="merged",
        person=person,
    )
    conflict = PersonImportConflict.objects.create(
        org=org,
        batch=batch,
        record=record,
        code="ambiguous_identity",
        person_ids=[str(person.id)],
        status="resolved",
        revision=1,
        resolved_at=timezone.now(),
    )
    decision = PersonImportDecision.objects.create(
        org=org,
        batch=batch,
        record=record,
        conflict=conflict,
        action="link_existing",
        target_person=person,
        actor=profile,
        idempotency_key=uuid4(),
        request_hash=("3" if suffix == "a" else "4") * 64,
        expected_revision=0,
        resulting_revision=1,
    )
    impact = PersonImportImpact.objects.create(
        org=org,
        batch=batch,
        record=record,
        person=person,
        impact_type="merged",
        changed_fields=["skills"],
    )
    observation = PersonIdentityObservation.objects.create(
        org=org,
        batch=batch,
        record=record,
        person=person,
        identity=identity,
        kind="email",
        normalized_value_hash=("5" if suffix == "a" else "6") * 64,
        source="manual",
        source_namespace="manual:csv",
        source_record_id=f"record-{suffix}",
    )
    return {
        "person": person,
        "identity": identity,
        "job": job,
        "batch": batch,
        "record": record,
        "conflict": conflict,
        "decision": decision,
        "impact": impact,
        "observation": observation,
    }


def _raw_update(instance, column, value, *, json_value=False):
    quote = connection.ops.quote_name
    table = quote(instance._meta.db_table)
    expression = "%s::jsonb" if json_value else "%s"
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET {quote(column)} = {expression} "
            f"WHERE {quote('id')} = %s",
            [json.dumps(value) if json_value else value, instance.id],
        )


def _raw_insert_clone(instance, replacements):
    quote = connection.ops.quote_name
    table = quote(instance._meta.db_table)
    columns = []
    expressions = []
    params = []
    for field in instance._meta.local_concrete_fields:
        columns.append(quote(field.column))
        if field.column in replacements:
            expressions.append("%s")
            params.append(replacements[field.column])
        else:
            expressions.append(quote(field.column))
    params.append(instance.id)
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"SELECT {', '.join(expressions)} FROM {table} WHERE id = %s",
            params,
        )


def _preview_in_thread(*, org_id, profile_id, key, barrier):
    close_old_connections()
    try:
        with database_org_context(org_id):
            org = Org.objects.get(id=org_id)
            profile = Profile.objects.get(id=profile_id)
            barrier.wait(timeout=10)
            result = preview_person_import(
                org=org,
                requested_by=profile,
                idempotency_key=key,
                file_bytes=CSV_BYTES,
                filename="concurrent.csv",
                mapping=MAPPING,
            )
            return str(result.batch.id), result.replayed
    finally:
        close_old_connections()


def _execute_in_thread(*, org_id, batch_id, request_hash, barrier):
    close_old_connections()
    try:
        with database_org_context(org_id):
            barrier.wait(timeout=10)
            return execute_person_import(
                org_id=org_id,
                batch_id=batch_id,
                request_hash=request_hash,
            )
    finally:
        close_old_connections()


def _commit_in_thread(*, org_id, profile_id, batch_id, key, barrier):
    close_old_connections()
    try:
        with database_org_context(org_id):
            org = Org.objects.get(id=org_id)
            profile = Profile.objects.get(id=profile_id)
            batch = PersonImportBatch.objects.get(org=org, id=batch_id)
            barrier.wait(timeout=10)
            try:
                commit_person_import(
                    org=org,
                    requested_by=profile,
                    batch=batch,
                    expected_revision=0,
                    idempotency_key=key,
                )
            except PersonImportServiceError as exc:
                return "rejected", exc.code
            return "committed", ""
    finally:
        close_old_connections()


def _expire_in_thread(*, org_id, cutoff, barrier):
    close_old_connections()
    try:
        with database_org_context(org_id):
            org = Org.objects.get(id=org_id)
            barrier.wait(timeout=10)
            result = expire_stale_import_previews(
                org=org,
                older_than=cutoff,
                limit=1,
            )
            return "expired", result["expired_count"]
    finally:
        close_old_connections()


def test_import_history_tables_are_isolated_by_postgres_rls(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
    profile_b,
):
    with database_org_context(org_a.id):
        graph_a = _make_graph(org=org_a, profile=admin_profile, suffix="a")
    with database_org_context(org_b.id):
        graph_b = _make_graph(org=org_b, profile=profile_b, suffix="b")

    models = (
        PersonImportBatch,
        PersonImportRecord,
        PersonImportConflict,
        PersonImportDecision,
        PersonImportImpact,
        PersonIdentityObservation,
    )
    with database_org_context(org_a.id):
        for model in models:
            assert list(model.objects.values_list("org_id", flat=True)) == [org_a.id]
            assert not model.objects.filter(id=graph_b[_model_key(model)].id).exists()
    with database_org_context(org_b.id):
        for model in models:
            assert list(model.objects.values_list("org_id", flat=True)) == [org_b.id]
            assert not model.objects.filter(id=graph_a[_model_key(model)].id).exists()
    with _empty_database_org_context():
        for model in models:
            assert model.objects.count() == 0


def _model_key(model):
    return {
        PersonImportBatch: "batch",
        PersonImportRecord: "record",
        PersonImportConflict: "conflict",
        PersonImportDecision: "decision",
        PersonImportImpact: "impact",
        PersonIdentityObservation: "observation",
    }[model]


def test_every_import_child_foreign_key_has_a_database_org_guard(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
    profile_b,
):
    with database_org_context(org_a.id):
        own = _make_graph(org=org_a, profile=admin_profile, suffix="a")
    with database_org_context(org_b.id):
        foreign = _make_graph(org=org_b, profile=profile_b, suffix="b")

    cases = [
        (own["batch"], "requested_by_id", foreign["batch"].requested_by_id, False),
        (own["batch"], "automation_job_id", foreign["job"].id, False),
        (own["record"], "batch_id", foreign["batch"].id, False),
        (own["record"], "person_id", foreign["person"].id, False),
        (own["conflict"], "batch_id", foreign["batch"].id, False),
        (own["conflict"], "record_id", foreign["record"].id, False),
        (
            own["conflict"],
            "person_ids",
            [str(foreign["person"].id)],
            True,
        ),
        (own["impact"], "batch_id", foreign["batch"].id, False),
        (own["impact"], "record_id", foreign["record"].id, False),
        (own["impact"], "person_id", foreign["person"].id, False),
        (own["observation"], "batch_id", foreign["batch"].id, False),
        (own["observation"], "record_id", foreign["record"].id, False),
        (own["observation"], "person_id", foreign["person"].id, False),
        (own["observation"], "identity_id", foreign["identity"].id, False),
    ]

    with database_org_context(org_a.id):
        for instance, column, value, json_value in cases:
            with pytest.raises(DatabaseError, match=CHILD_ORG_GUARD_MESSAGE):
                with transaction.atomic():
                    _raw_update(instance, column, value, json_value=json_value)

        decision_cases = {
            "batch_id": foreign["batch"].id,
            "record_id": foreign["record"].id,
            "conflict_id": foreign["conflict"].id,
            "target_person_id": foreign["person"].id,
            "actor_id": foreign["batch"].requested_by_id,
        }
        for column, value in decision_cases.items():
            with pytest.raises(DatabaseError, match=CHILD_ORG_GUARD_MESSAGE):
                with transaction.atomic():
                    _raw_insert_clone(
                        own["decision"],
                        {
                            "id": uuid4(),
                            "idempotency_key": uuid4(),
                            column: value,
                        },
                    )


def test_import_decision_database_trigger_rejects_raw_update_and_delete(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        decision = _make_graph(org=org_a, profile=admin_profile, suffix="a")["decision"]
        table = connection.ops.quote_name(PersonImportDecision._meta.db_table)

        with pytest.raises(DatabaseError, match=APPEND_ONLY_MESSAGE):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE {table} SET request_hash = %s WHERE id = %s",
                        ["0" * 64, decision.id],
                    )
        with pytest.raises(DatabaseError, match=APPEND_ONLY_MESSAGE):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table} WHERE id = %s", [decision.id])


def test_postgres_concurrent_same_batch_key_creates_one_preview_and_replays(
    transactional_db,
    org_a,
    admin_profile,
):
    key = uuid4()
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _preview_in_thread,
                org_id=org_a.id,
                profile_id=admin_profile.id,
                key=key,
                barrier=barrier,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=20) for future in futures]

    assert len({batch_id for batch_id, _ in results}) == 1
    assert sorted(replayed for _, replayed in results) == [False, True]
    with database_org_context(org_a.id):
        assert PersonImportBatch.objects.filter(idempotency_key=key).count() == 1
        assert PersonImportRecord.objects.count() == 1


def test_postgres_concurrent_imports_converge_on_one_normalized_identity(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    monkeypatch.setattr("matching.import_pipeline._safe_dispatch", lambda job: None)
    batches = []
    with database_org_context(org_a.id):
        for suffix in ("a", "b"):
            file_bytes = (
                "Name,Email,Note\n"
                f"Alice Example,ALICE@example.com,Source note {suffix}\n"
            ).encode()
            mapping = {
                **MAPPING,
                "evidence_summary": "Note",
            }
            preview = preview_person_import(
                org=org_a,
                requested_by=admin_profile,
                idempotency_key=uuid4(),
                file_bytes=file_bytes,
                filename=f"concurrent-{suffix}.csv",
                mapping=mapping,
            )
            commit = commit_person_import(
                org=org_a,
                requested_by=admin_profile,
                batch=preview.batch,
                expected_revision=0,
                idempotency_key=uuid4(),
            )
            commit.batch.refresh_from_db()
            batches.append(commit.batch)

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _execute_in_thread,
                org_id=org_a.id,
                batch_id=batch.id,
                request_hash=batch.request_hash,
                barrier=barrier,
            )
            for batch in batches
        ]
        results = [future.result(timeout=20) for future in futures]

    assert all(result["status"] in {"completed", "partial"} for result in results)
    with database_org_context(org_a.id):
        assert Person.objects.count() == 1
        person = Person.objects.get()
        assert (
            PersonIdentity.objects.filter(
                person=person,
                kind="email",
                normalized_value="alice@example.com",
            ).count()
            == 1
        )
        assert PersonImportRecord.objects.filter(person=person).count() == 2
        assert PersonImportConflict.objects.count() == 0
        assert PersonIdentityObservation.objects.filter(person=person).count() == 2


def test_postgres_preview_expiry_and_commit_are_atomic_without_partial_scrub(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    monkeypatch.setattr("matching.import_pipeline._safe_dispatch", lambda job: None)
    cutoff = timezone.now() - timedelta(days=30)
    with database_org_context(org_a.id):
        preview = preview_person_import(
            org=org_a,
            requested_by=admin_profile,
            idempotency_key=uuid4(),
            file_bytes=CSV_BYTES,
            filename="expiry-race.csv",
            mapping=MAPPING,
        )
        PersonImportBatch.objects.filter(id=preview.batch.id).update(
            created_at=cutoff - timedelta(days=1)
        )

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        commit_future = executor.submit(
            _commit_in_thread,
            org_id=org_a.id,
            profile_id=admin_profile.id,
            batch_id=preview.batch.id,
            key=uuid4(),
            barrier=barrier,
        )
        expire_future = executor.submit(
            _expire_in_thread,
            org_id=org_a.id,
            cutoff=cutoff,
            barrier=barrier,
        )
        commit_result = commit_future.result(timeout=20)
        expire_result = expire_future.result(timeout=20)

    with database_org_context(org_a.id):
        batch = PersonImportBatch.objects.get(id=preview.batch.id)
        record = batch.records.get()
        if batch.status == "queued":
            assert commit_result == ("committed", "")
            assert expire_result == ("expired", 0)
            assert record.normalized_payload
            assert record.display_name == "Alice Example"
        else:
            assert batch.status == "failed"
            assert batch.error_code == "preview_expired"
            assert commit_result == ("rejected", "invalid_import_state")
            assert expire_result == ("expired", 1)
            assert record.normalized_payload == {}
            assert record.display_name == ""
            assert record.masked_identities == []
