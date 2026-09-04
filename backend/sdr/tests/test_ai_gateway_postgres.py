from contextlib import contextmanager
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from automation.tenant_context import database_org_context
from sdr.models import SDRAICallAudit

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS enforcement is required.",
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


def test_ai_call_audit_is_hidden_from_other_and_empty_org_contexts(org_a, org_b):
    with database_org_context(org_a.id):
        audit = SDRAICallAudit.objects.create(
            org=org_a,
            purpose="lead_qualification",
            status="blocked",
            prompt_version="test-v1",
            configuration_sha256="a" * 64,
            failure_code="ai_disabled",
            retention_expires_at=timezone.now() + timedelta(days=1),
        )

    with database_org_context(org_b.id):
        assert not SDRAICallAudit.objects.filter(id=audit.id).exists()

    with _empty_database_org_context():
        assert not SDRAICallAudit.objects.filter(id=audit.id).exists()

    with database_org_context(org_a.id):
        assert SDRAICallAudit.objects.filter(id=audit.id).exists()
