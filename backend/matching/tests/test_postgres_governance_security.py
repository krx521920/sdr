from contextlib import contextmanager
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from automation.tenant_context import database_org_context
from matching.governance import (
    ensure_evidence_provenance,
    mutate_person_governance,
    upsert_contact_intent,
)
from matching.models import (
    Evidence,
    EvidenceGovernanceEvent,
    EvidenceProvenance,
    Person,
    PersonContactIntent,
    PersonContactIntentEvent,
    PersonGovernanceEvent,
    PersonIdentity,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS and governance triggers are required.",
    ),
    pytest.mark.django_db(transaction=True),
]


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
                "SELECT set_config('app.current_org', %s, false)",
                [previous],
            )


def _create_governance_graph(*, org, actor, suffix):
    person = Person.objects.create(
        org=org,
        display_name=f"Governed person {suffix}",
    )
    identity = PersonIdentity.objects.create(
        org=org,
        person=person,
        kind="email",
        normalized_value=f"governed-{suffix}@example.test",
        source="manual",
    )
    evidence = Evidence.objects.create(
        org=org,
        person=person,
        kind="skill",
        source="manual",
        summary="Confirmed skill evidence",
        facts={"skills": ["python"]},
    )
    provenance = ensure_evidence_provenance(evidence=evidence, actor=actor)
    evidence_event = EvidenceGovernanceEvent.objects.get(
        org=org,
        provenance=provenance,
    )
    intent_result = upsert_contact_intent(
        org=org,
        person_id=person.id,
        actor=actor,
        idempotency_key=uuid4(),
        expected_revision=0,
        channel="email",
        purpose="general_contact",
        state="open",
        source="manual",
        identity_id=identity.id,
    )
    person_result = mutate_person_governance(
        org=org,
        person_id=person.id,
        actor=actor,
        idempotency_key=uuid4(),
        expected_revision=0,
        action="request",
    )
    return {
        "person": person,
        "identity": identity,
        "evidence": evidence,
        "provenance": provenance,
        "evidence_event": evidence_event,
        "intent": intent_result.value,
        "intent_event": intent_result.event,
        "person_event": person_result.event,
    }


def _assert_raw_mutation_rejected(model, row_id):
    table = connection.ops.quote_name(model._meta.db_table)
    with pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET created_at = created_at WHERE id = %s",
                    [row_id],
                )
    with pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row_id])
    assert model.objects.filter(id=row_id).exists()


def test_all_governance_tables_are_force_rls_isolated(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
):
    with database_org_context(org_a.id):
        graph = _create_governance_graph(
            org=org_a,
            actor=admin_profile,
            suffix="rls",
        )
        rows = (
            (EvidenceProvenance, graph["provenance"].id),
            (EvidenceGovernanceEvent, graph["evidence_event"].id),
            (PersonContactIntent, graph["intent"].id),
            (PersonContactIntentEvent, graph["intent_event"].id),
            (PersonGovernanceEvent, graph["person_event"].id),
        )
        for model, row_id in rows:
            assert model.objects.filter(id=row_id).exists()

    with database_org_context(org_b.id):
        for model, row_id in rows:
            assert not model.objects.filter(id=row_id).exists()

    with _empty_database_org_context():
        for model, row_id in rows:
            assert not model.objects.filter(id=row_id).exists()


def test_all_governance_event_tables_reject_raw_update_and_delete(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        graph = _create_governance_graph(
            org=org_a,
            actor=admin_profile,
            suffix="append-only",
        )
        _assert_raw_mutation_rejected(
            EvidenceGovernanceEvent,
            graph["evidence_event"].id,
        )
        _assert_raw_mutation_rejected(
            PersonContactIntentEvent,
            graph["intent_event"].id,
        )
        _assert_raw_mutation_rejected(
            PersonGovernanceEvent,
            graph["person_event"].id,
        )


def test_governance_child_guards_reject_cross_org_relationships(
    transactional_db,
    org_a,
    org_b,
):
    with database_org_context(org_b.id):
        foreign_person = Person.objects.create(
            org=org_b,
            display_name="Foreign governed person",
        )
        foreign_evidence = Evidence.objects.create(
            org=org_b,
            person=foreign_person,
            kind="other",
            source="manual",
            summary="Foreign evidence",
            facts={},
        )

    with database_org_context(org_a.id):
        with pytest.raises(DatabaseError, match="organization mismatch"):
            with transaction.atomic():
                EvidenceProvenance.objects.create(
                    org=org_a,
                    evidence=foreign_evidence,
                    confirmation_status="confirmed",
                    confirmed_at=timezone.now(),
                    revision=1,
                )
        with pytest.raises(DatabaseError, match="organization mismatch"):
            with transaction.atomic():
                PersonContactIntent.objects.create(
                    org=org_a,
                    person=foreign_person,
                    channel="email",
                    purpose="general_contact",
                    state="unknown",
                    source="manual",
                )
        with pytest.raises(DatabaseError, match="projection mismatch"):
            with transaction.atomic():
                PersonGovernanceEvent.objects.create(
                    org=org_a,
                    person=foreign_person,
                    event_type="export_requested",
                    idempotency_key=uuid4(),
                    request_hash="a" * 64,
                    expected_revision=0,
                    resulting_revision=0,
                    safe_snapshot={},
                )


def test_ai_evidence_cannot_be_directly_confirmed_without_human_reviewer(
    transactional_db,
    org_a,
):
    with database_org_context(org_a.id):
        person = Person.objects.create(org=org_a, display_name="AI subject")
        evidence = Evidence.objects.create(
            org=org_a,
            person=person,
            kind="profile",
            source="ai",
            summary="AI extracted profile",
            facts={},
        )
        with pytest.raises(DatabaseError, match="human confirmation"):
            with transaction.atomic():
                EvidenceProvenance.objects.create(
                    org=org_a,
                    evidence=evidence,
                    collection_method="ai_extraction",
                    confirmation_status="confirmed",
                    confirmed_at=timezone.now(),
                    confirmed_by=None,
                    revision=1,
                )
