from contextlib import contextmanager

import pytest
from django.db import DatabaseError, connection, transaction

from automation.tenant_context import database_org_context
from sdr.models import LeadIntake, SDRComplianceEvent, SDRDataProvenance

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS and trigger enforcement are required.",
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


def _intake(org, suffix):
    return LeadIntake.objects.create(
        org=org,
        source="manual",
        source_record_id=f"postgres-compliance:{suffix}",
    )


def test_compliance_rls_hides_provenance_and_events_cross_org_and_empty(
    transactional_db,
    org_a,
    org_b,
):
    with database_org_context(org_a.id):
        intake = _intake(org_a, "rls")
        provenance = SDRDataProvenance.objects.create(org=org_a, intake=intake)
        event = SDRComplianceEvent.objects.create(
            org=org_a,
            intake=intake,
            event_type="provenance_recorded",
            event_key="postgres-compliance:rls",
        )

    with database_org_context(org_b.id):
        assert not SDRDataProvenance.objects.filter(id=provenance.id).exists()
        assert not SDRComplianceEvent.objects.filter(id=event.id).exists()

    with _empty_database_org_context():
        assert not SDRDataProvenance.objects.filter(id=provenance.id).exists()
        assert not SDRComplianceEvent.objects.filter(id=event.id).exists()


def test_compliance_child_org_triggers_reject_cross_org_relations(
    transactional_db,
    org_a,
    org_b,
):
    with database_org_context(org_b.id):
        foreign_intake = _intake(org_b, "foreign")

    with database_org_context(org_a.id):
        with pytest.raises(DatabaseError, match="child organization mismatch"):
            with transaction.atomic():
                SDRDataProvenance.objects.create(
                    org=org_a,
                    intake=foreign_intake,
                )
        with pytest.raises(DatabaseError, match="child organization mismatch"):
            with transaction.atomic():
                SDRComplianceEvent.objects.create(
                    org=org_a,
                    intake=foreign_intake,
                    event_type="contact_blocked",
                    event_key="postgres-compliance:foreign-event",
                )


def test_compliance_event_postgres_trigger_rejects_raw_update_and_delete(
    transactional_db,
    org_a,
):
    with database_org_context(org_a.id):
        intake = _intake(org_a, "append-only")
        event = SDRComplianceEvent.objects.create(
            org=org_a,
            intake=intake,
            event_type="contact_blocked",
            event_key="postgres-compliance:append-only",
        )
        table = connection.ops.quote_name(SDRComplianceEvent._meta.db_table)

        with pytest.raises(DatabaseError, match="append-only"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE {table} SET reason = %s WHERE id = %s",
                        ["tampered", event.id],
                    )
        with pytest.raises(DatabaseError, match="append-only"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table} WHERE id = %s", [event.id])

        assert SDRComplianceEvent.objects.filter(id=event.id).exists()
