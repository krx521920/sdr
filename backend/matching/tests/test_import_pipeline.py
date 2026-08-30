import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from automation.tenant_context import database_org_context
from matching.import_pipeline import execute_person_import, expire_stale_import_previews
from matching.models import (
    Evidence,
    MatchOpportunity,
    MatchRun,
    Person,
    PersonIdentity,
    PersonIdentityObservation,
    PersonImportBatch,
    PersonImportBatchStatus,
    PersonImportDecision,
    PersonImportImpact,
    PersonImportRecord,
)

PREVIEW_PATH = "/api/matching/person-imports/preview/"
DEFAULT_MAPPING = {"display_name": "Name", "email": "Email"}


@pytest.fixture(autouse=True)
def _disable_ssl_redirect_for_api_tests():
    with override_settings(SECURE_SSL_REDIRECT=False):
        yield


def _csv_upload(csv_text, *, filename="people.csv"):
    return SimpleUploadedFile(
        filename,
        csv_text.encode("utf-8"),
        content_type="text/csv",
    )


def _preview(
    client,
    csv_text,
    *,
    idempotency_key=None,
    mapping=None,
    filename="people.csv",
):
    key = idempotency_key or uuid4()
    response = client.post(
        PREVIEW_PATH,
        {
            "file": _csv_upload(csv_text, filename=filename),
            "mapping": json.dumps(mapping or DEFAULT_MAPPING),
        },
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    return response, key


def _commit(client, batch, *, idempotency_key=None, expected_revision=None):
    key = idempotency_key or uuid4()
    response = client.post(
        f"/api/matching/person-imports/{batch.id}/commit/",
        {
            "expected_revision": (
                batch.revision if expected_revision is None else expected_revision
            )
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    return response, key


def _execute(batch):
    batch.refresh_from_db()
    return execute_person_import(
        org_id=batch.org_id,
        batch_id=batch.id,
        request_hash=batch.request_hash,
    )


def _preview_and_execute(
    client,
    csv_text,
    *,
    mapping=None,
    filename="people.csv",
):
    preview, _ = _preview(
        client,
        csv_text,
        mapping=mapping,
        filename=filename,
    )
    assert preview.status_code == 201, preview.content
    batch = PersonImportBatch.objects.get(id=preview.json()["id"])
    commit, _ = _commit(client, batch)
    assert commit.status_code == 202, commit.content
    _execute(batch)
    batch.refresh_from_db()
    return batch


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_preview_and_commit_idempotency_replay_and_payload_conflict(
    admin_client,
):
    preview_key = uuid4()
    csv_text = "Name,Email\nAlice Example,ALICE@example.com\n"

    created, _ = _preview(admin_client, csv_text, idempotency_key=preview_key)
    replayed, _ = _preview(admin_client, csv_text, idempotency_key=preview_key)
    changed, _ = _preview(
        admin_client,
        "Name,Email\nDifferent Person,different@example.com\n",
        idempotency_key=preview_key,
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert changed.status_code == 409
    assert created.json()["replayed"] is False
    assert replayed.json()["replayed"] is True
    assert replayed.json()["id"] == created.json()["id"]
    assert "alice@example.com" not in json.dumps(created.json()).lower()
    assert PersonImportBatch.objects.count() == 1
    assert PersonImportRecord.objects.count() == 1

    first_batch = PersonImportBatch.objects.get(id=created.json()["id"])
    commit_key = uuid4()
    queued, _ = _commit(
        admin_client,
        first_batch,
        idempotency_key=commit_key,
    )
    queue_replay, _ = _commit(
        admin_client,
        first_batch,
        idempotency_key=commit_key,
        expected_revision=0,
    )

    same_key_changed_commit, _ = _commit(
        admin_client,
        first_batch,
        idempotency_key=commit_key,
        expected_revision=1,
    )

    assert queued.status_code == 202
    assert queue_replay.status_code == 202
    assert queue_replay.json()["replayed"] is True
    assert same_key_changed_commit.status_code == 409
    first_batch.refresh_from_db()
    assert first_batch.automation_job_id is not None


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_csv_identity_normalization_merges_across_sources(
    admin_client,
    org_a,
):
    existing = Person.objects.create(org=org_a, display_name="Alice Example")
    PersonIdentity.objects.create(
        org=org_a,
        person=existing,
        kind="email",
        normalized_value="alice@example.com",
        display_value="alice@example.com",
        source="apollo",
    )

    batch = _preview_and_execute(
        admin_client,
        "Name,Email\nAlice Example,  ALICE@EXAMPLE.COM  \n",
        filename="crm-export.csv",
    )

    assert Person.objects.filter(org=org_a).count() == 1
    assert PersonIdentity.objects.filter(org=org_a).count() == 1
    record = batch.records.get()
    assert record.status == "merged"
    assert record.normalized_payload == {}
    assert record.person_id == existing.id
    observation = PersonIdentityObservation.objects.get(batch=batch, record=record)
    assert observation.person_id == existing.id
    assert observation.identity.person_id == existing.id
    assert observation.normalized_value_hash


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_ambiguous_identity_match_creates_conflict_without_evidence_or_impact(
    admin_client,
    org_a,
):
    email_owner = Person.objects.create(org=org_a, display_name="Email owner")
    phone_owner = Person.objects.create(org=org_a, display_name="Phone owner")
    PersonIdentity.objects.create(
        org=org_a,
        person=email_owner,
        kind="email",
        normalized_value="shared@example.com",
        source="manual",
    )
    PersonIdentity.objects.create(
        org=org_a,
        person=phone_owner,
        kind="phone",
        normalized_value="+8613800138000",
        source="manual",
    )
    evidence_before = Evidence.objects.count()

    preview, _ = _preview(
        admin_client,
        "Name,Email,Phone\nAmbiguous,shared@example.com,+8613800138000\n",
        mapping={
            "display_name": "Name",
            "email": "Email",
            "phone": "Phone",
        },
    )

    assert preview.status_code == 201, preview.content
    batch = PersonImportBatch.objects.get(id=preview.json()["id"])
    commit, _ = _commit(admin_client, batch)
    assert commit.status_code == 202, commit.content
    _execute(batch)
    batch.refresh_from_db()
    record = batch.records.get()
    conflict = batch.conflicts.get(record=record)
    assert record.status == "conflict"
    assert set(conflict.person_ids) == {str(email_owner.id), str(phone_owner.id)}
    assert Evidence.objects.count() == evidence_before
    assert PersonImportImpact.objects.filter(record=record).count() == 0
    assert PersonIdentityObservation.objects.filter(record=record).count() == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_duplicate_rows_and_metadata_only_reimports_do_not_create_impacts(
    admin_client,
    org_a,
):
    duplicate_csv = (
        "Name,Email,Record ID\n"
        "Alice Example,alice@example.com,source-a\n"
        "Alice Example,alice@example.com,source-a\n"
    )
    mapping = {
        **DEFAULT_MAPPING,
        "source_record_id": "Record ID",
    }
    first_batch = _preview_and_execute(
        admin_client,
        duplicate_csv,
        mapping=mapping,
        filename="source-a.csv",
    )

    assert Person.objects.filter(org=org_a).count() == 1
    assert PersonImportImpact.objects.filter(batch=first_batch).count() == 1
    assert first_batch.records.exclude(impacts__isnull=False).count() == 1
    terminal_records = first_batch.records.exclude(status="invalid")
    assert all(
        not value
        for value in terminal_records.values_list("normalized_payload", flat=True)
    )
    assert first_batch.records.get(status="invalid").normalized_payload
    evidence_count = Evidence.objects.count()
    MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        status="open",
        title="Metadata replay guard",
    )
    match_run_count = MatchRun.objects.count()

    metadata_only_batch = _preview_and_execute(
        admin_client,
        "Name,Email,Record ID\nAlice Example,alice@example.com,source-b\n",
        mapping=mapping,
        filename="renamed-source-file.csv",
    )

    assert Person.objects.filter(org=org_a).count() == 1
    assert metadata_only_batch.records.get().status == "replayed"
    assert metadata_only_batch.records.get().normalized_payload == {}
    assert Evidence.objects.count() == evidence_count
    assert PersonImportImpact.objects.filter(batch=metadata_only_batch).count() == 0
    assert (
        PersonIdentityObservation.objects.filter(batch=metadata_only_batch).count() == 1
    )
    assert metadata_only_batch.match_run_ids == []
    assert MatchRun.objects.count() == match_run_count


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_same_source_record_with_changed_confidence_is_a_conflict(
    admin_client,
    org_a,
):
    mapping = {
        **DEFAULT_MAPPING,
        "source_record_id": "Record ID",
        "confidence": "Confidence",
    }
    first_batch = _preview_and_execute(
        admin_client,
        (
            "Name,Email,Record ID,Confidence\n"
            "Alice Example,alice@example.com,source-a,0.500\n"
        ),
        mapping=mapping,
    )
    evidence_count = Evidence.objects.count()

    changed_batch = _preview_and_execute(
        admin_client,
        (
            "Name,Email,Record ID,Confidence\n"
            "Alice Example,alice@example.com,source-a,0.900\n"
        ),
        mapping=mapping,
    )
    changed_record = changed_batch.records.get()

    assert first_batch.records.get().status == "created"
    assert changed_record.status == "conflict"
    assert changed_record.normalized_payload
    assert changed_record.error_code == "source_record_conflict"
    assert changed_batch.conflicts.get(record=changed_record).code == (
        "source_record_conflict"
    )
    assert Evidence.objects.count() == evidence_count
    assert PersonImportImpact.objects.filter(batch=changed_batch).count() == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_only_changed_people_are_requested_for_targeted_recompute(
    admin_client,
    org_a,
):
    changed = Person.objects.create(org=org_a, display_name="Changed person")
    unaffected = Person.objects.create(org=org_a, display_name="Unaffected person")
    PersonIdentity.objects.create(
        org=org_a,
        person=changed,
        kind="email",
        normalized_value="changed@example.com",
        source="manual",
    )
    PersonIdentity.objects.create(
        org=org_a,
        person=unaffected,
        kind="email",
        normalized_value="unaffected@example.com",
        source="manual",
    )
    MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        status="open",
        title="Python project",
        required_criteria={"skills": ["python"]},
    )

    batch = _preview_and_execute(
        admin_client,
        "Name,Email,Skills\nChanged person,changed@example.com,python\n",
        mapping={
            "display_name": "Name",
            "email": "Email",
            "skills": "Skills",
        },
    )

    impact = PersonImportImpact.objects.get(batch=batch)
    assert impact.person_id == changed.id
    assert "skills" in impact.changed_fields
    assert batch.match_run_ids
    runs = MatchRun.objects.filter(id__in=batch.match_run_ids)
    assert runs.count() == 1
    assert runs.get().requested_person_ids == [str(changed.id)]
    assert str(unaffected.id) not in runs.get().requested_person_ids


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_resolve_uses_revision_idempotency_and_append_only_decision(
    admin_client,
    org_a,
):
    first = Person.objects.create(org=org_a, display_name="First candidate")
    second = Person.objects.create(org=org_a, display_name="Second candidate")
    PersonIdentity.objects.create(
        org=org_a,
        person=first,
        kind="email",
        normalized_value="ambiguous@example.com",
        source="manual",
    )
    PersonIdentity.objects.create(
        org=org_a,
        person=second,
        kind="phone",
        normalized_value="+8613913913913",
        source="manual",
    )
    preview, _ = _preview(
        admin_client,
        "Name,Email,Phone\nAmbiguous,ambiguous@example.com,+8613913913913\n",
        mapping={
            "display_name": "Name",
            "email": "Email",
            "phone": "Phone",
        },
    )
    batch = PersonImportBatch.objects.get(id=preview.json()["id"])
    commit, _ = _commit(admin_client, batch)
    assert commit.status_code == 202, commit.content
    _execute(batch)
    batch.refresh_from_db()
    record = batch.records.get()
    conflict = record.conflict
    resolve_path = f"/api/matching/person-import-records/{record.id}/resolve/"
    key = uuid4()
    payload = {
        "action": "link_existing",
        "person_id": str(first.id),
        "expected_revision": conflict.revision,
    }

    resolved = admin_client.post(
        resolve_path,
        deepcopy(payload),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    replayed = admin_client.post(
        resolve_path,
        deepcopy(payload),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    changed_payload = {**payload, "person_id": str(second.id)}
    mismatched_retry = admin_client.post(
        resolve_path,
        changed_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    stale = admin_client.post(
        resolve_path,
        {**payload, "expected_revision": conflict.revision},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert resolved.status_code == 201, resolved.content
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert mismatched_retry.status_code == 409
    assert stale.status_code == 409
    decision = PersonImportDecision.objects.get(record=record)
    assert decision.target_person_id == first.id
    assert decision.expected_revision == payload["expected_revision"]
    assert decision.resulting_revision == payload["expected_revision"] + 1
    assert PersonImportDecision.objects.filter(record=record).count() == 1
    record.refresh_from_db()
    assert record.status == "merged"
    assert record.normalized_payload == {}

    decision.request_hash = "0" * 64
    with pytest.raises(ValidationError, match="cannot be updated"):
        decision.save()
    with pytest.raises(ValidationError, match="cannot be updated"):
        PersonImportDecision.objects.filter(id=decision.id).update(
            request_hash="0" * 64
        )
    with pytest.raises(ValidationError, match="cannot be deleted"):
        PersonImportDecision.objects.filter(id=decision.id).delete()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_failed_and_skipped_rows_scrub_executable_staging_payload(
    admin_client,
    org_a,
):
    failed_preview, _ = _preview(
        admin_client,
        "Name,Email\nBroken Row,broken@example.com\n",
    )
    failed_batch = PersonImportBatch.objects.get(id=failed_preview.json()["id"])
    failed_record = failed_batch.records.get()
    PersonImportRecord.objects.filter(id=failed_record.id).update(
        normalized_payload={"person": {"display_name": "private"}}
    )
    queued, _ = _commit(admin_client, failed_batch)
    assert queued.status_code == 202
    _execute(failed_batch)
    failed_record.refresh_from_db()
    assert failed_record.status == "failed"
    assert failed_record.normalized_payload == {}

    email_owner = Person.objects.create(org=org_a, display_name="Email owner")
    phone_owner = Person.objects.create(org=org_a, display_name="Phone owner")
    PersonIdentity.objects.create(
        org=org_a,
        person=email_owner,
        kind="email",
        normalized_value="skip@example.com",
        source="manual",
    )
    PersonIdentity.objects.create(
        org=org_a,
        person=phone_owner,
        kind="phone",
        normalized_value="+8613700000000",
        source="manual",
    )
    conflict_preview, _ = _preview(
        admin_client,
        "Name,Email,Phone\nSkip Me,skip@example.com,+8613700000000\n",
        mapping={
            "display_name": "Name",
            "email": "Email",
            "phone": "Phone",
        },
    )
    conflict_batch = PersonImportBatch.objects.get(id=conflict_preview.json()["id"])
    queued, _ = _commit(admin_client, conflict_batch)
    assert queued.status_code == 202
    _execute(conflict_batch)
    conflict_record = conflict_batch.records.get()
    assert conflict_record.status == "conflict"
    assert conflict_record.normalized_payload

    response = admin_client.post(
        f"/api/matching/person-import-records/{conflict_record.id}/resolve/",
        {
            "action": "skip",
            "expected_revision": conflict_record.conflict.revision,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 201, response.content
    conflict_record.refresh_from_db()
    assert conflict_record.status == "skipped"
    assert conflict_record.normalized_payload == {}


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_expire_stale_previews_is_bounded_org_scoped_and_preserves_ledger(
    admin_client,
    org_a,
    org_b,
    profile_b,
):
    own_batches = []
    for index in range(2):
        response, _ = _preview(
            admin_client,
            f"Name,Email\nStale {index},stale-{index}@example.com\n",
        )
        own_batches.append(PersonImportBatch.objects.get(id=response.json()["id"]))
    with database_org_context(org_b.id):
        foreign = PersonImportBatch.objects.create(
            org=org_b,
            requested_by=profile_b,
            idempotency_key=uuid4(),
            request_hash="a" * 64,
            content_hash="b" * 64,
            original_filename="foreign.csv",
            file_size=10,
            mapping=DEFAULT_MAPPING,
            total_count=1,
            ready_count=1,
        )
        foreign_record = PersonImportRecord.objects.create(
            org=org_b,
            batch=foreign,
            row_number=2,
            row_hash="c" * 64,
            source_record_id="foreign-record",
            display_name="Foreign PII",
            normalized_payload={"person": {"display_name": "Foreign PII"}},
            masked_identities=[
                {"kind": "email", "masked_value": "fo***@example.com"}
            ],
        )
    cutoff = timezone.now() - timedelta(days=30)
    stale_created_at = cutoff - timedelta(days=1)
    with database_org_context(org_a.id):
        PersonImportBatch.objects.filter(id__in=[value.id for value in own_batches]).update(
            created_at=stale_created_at
        )
    with database_org_context(org_b.id):
        PersonImportBatch.objects.filter(id=foreign.id).update(
            created_at=stale_created_at
        )

    with database_org_context(org_a.id):
        first = expire_stale_import_previews(org=org_a, older_than=cutoff, limit=1)
        second = expire_stale_import_previews(org=org_a, older_than=cutoff, limit=1)

    assert first["expired_count"] == 1
    assert second["expired_count"] == 1
    for batch in own_batches:
        batch.refresh_from_db()
        record = batch.records.get()
        assert batch.status == PersonImportBatchStatus.FAILED
        assert batch.error_code == "preview_expired"
        assert batch.total_count == 1
        assert batch.ready_count == 1
        assert record.status == "ready"
        assert record.display_name == ""
        assert record.normalized_payload == {}
        assert record.masked_identities == []
        assert record.field_errors == []
    with database_org_context(org_b.id):
        foreign.refresh_from_db()
        foreign_record.refresh_from_db()
    assert foreign.status == PersonImportBatchStatus.PREVIEWED
    assert foreign_record.display_name == "Foreign PII"
    assert foreign_record.normalized_payload
