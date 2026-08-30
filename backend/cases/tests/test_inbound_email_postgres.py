from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.db import connection

from automation.tenant_context import database_org_context
from cases.models import Case, InboundMailbox, InboundMailboxWebhookRoute

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL forced RLS is required.",
    ),
    pytest.mark.django_db(transaction=True),
]

SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:postgres-inbound-test"
SNS_SIGNING_CERT_URL = (
    "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"
)


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


def _raw_email():
    return (
        "From: Customer <customer@example.com>\r\n"
        "To: support@example.com\r\n"
        "Subject: PostgreSQL webhook\r\n"
        "Date: Sat, 9 May 2026 12:00:00 +0000\r\n"
        "Message-ID: <postgres-webhook@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        "Please help."
    )


def test_anonymous_webhook_bootstraps_tenant_before_forced_rls_read(
    transactional_db,
    unauthenticated_client,
    org_a,
):
    with database_org_context(org_a.id):
        mailbox = InboundMailbox.objects.create(
            org=org_a,
            address="support@example.com",
            provider="ses",
            sns_topic_arn=SNS_TOPIC_ARN,
            is_active=True,
        )

    with _empty_database_org_context():
        # Only the deliberately minimal route is visible before tenant
        # selection; the PII/config-bearing mailbox remains hidden by RLS.
        assert InboundMailboxWebhookRoute.objects.filter(
            mailbox_id=mailbox.id,
            org_id=org_a.id,
        ).exists()
        assert not InboundMailbox.objects.filter(id=mailbox.id).exists()

        with patch("cases.inbound_views.verify_sns_message") as verify:
            response = unauthenticated_client.post(
                f"/api/cases/inbound/{mailbox.id}/",
                {
                    "Type": "Notification",
                    "TopicArn": SNS_TOPIC_ARN,
                    "Message": _raw_email(),
                    "Signature": "test",
                    "SigningCertURL": SNS_SIGNING_CERT_URL,
                    "SignatureVersion": "1",
                },
                format="json",
                secure=True,
            )

        assert response.status_code == 200, response.content
        verify.assert_called_once()
        # The view and middleware must not leave the selected tenant on a
        # pooled connection after the anonymous request finishes.
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_org', true)")
            assert (cursor.fetchone()[0] or "") == ""
        assert not Case.objects.exists()

    with database_org_context(org_a.id):
        assert Case.objects.filter(name="PostgreSQL webhook").count() == 1


def test_corrupt_bootstrap_org_cannot_read_another_tenants_mailbox(
    transactional_db,
    unauthenticated_client,
    org_a,
    org_b,
):
    with database_org_context(org_a.id):
        mailbox = InboundMailbox.objects.create(
            org=org_a,
            address="tenant-a@example.com",
            provider="ses",
            sns_topic_arn="arn:aws:sns:us-east-1:123456789012:tenant-a",
            is_active=True,
        )

    # The bootstrap is intentionally non-RLS, so simulate corruption or a
    # privileged operational mistake.  The view must reload the mailbox under
    # the routed tenant and return the same 404 as an unknown UUID.
    with _empty_database_org_context():
        InboundMailboxWebhookRoute.objects.filter(mailbox_id=mailbox.id).update(
            org_id=org_b.id
        )
        with patch("cases.inbound_views.verify_sns_message") as verify:
            response = unauthenticated_client.post(
                f"/api/cases/inbound/{mailbox.id}/",
                {
                    "Type": "Notification",
                    "TopicArn": mailbox.sns_topic_arn,
                    "Message": _raw_email(),
                    "Signature": "test",
                    "SigningCertURL": SNS_SIGNING_CERT_URL,
                    "SignatureVersion": "1",
                },
                format="json",
                secure=True,
            )

        assert response.status_code == 404
        verify.assert_not_called()
        assert not Case.objects.exists()

    with database_org_context(org_a.id):
        assert not Case.objects.exists()
    with database_org_context(org_b.id):
        assert not Case.objects.exists()
