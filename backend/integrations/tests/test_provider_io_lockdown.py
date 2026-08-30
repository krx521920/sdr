from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from automation.errors import PermanentJobError
from integrations.providers.apollo.client import ApolloAPIError, ApolloClient
from integrations.providers.feishu_base.client import (
    FeishuBaseAPIError,
    FeishuBaseClient,
)
from integrations.providers.linkedin.client import (
    LinkedInInvitationsAPIError,
    LinkedInInvitationsClient,
)
from integrations.providers.whatsapp.client import (
    WhatsAppCloudAPIError,
    WhatsAppCloudClient,
)
from sdr.models import LeadNurtureDelivery
from sdr.nurture import process_nurture_email_job
from sdr.tests.test_nurture import accept_and_process, create_sequence

LOCKDOWN = override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)


@LOCKDOWN
def test_apollo_unguarded_search_is_blocked_before_http_request():
    session = Mock()
    client = ApolloClient(api_key="must-not-leave-process", session=session)

    with pytest.raises(ApolloAPIError) as exc_info:
        client.search_people(filters={}, page=1, per_page=1)

    assert exc_info.value.error_code == "execution_approval_required"
    assert exc_info.value.retryable is False
    session.request.assert_not_called()


@LOCKDOWN
def test_feishu_unguarded_token_request_is_blocked_before_http_request():
    session = Mock()
    client = FeishuBaseClient(session=session)

    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.tenant_access_token(
            app_id="must-not-leave-process",
            app_secret="must-not-leave-process",
        )

    assert exc_info.value.error_code == "execution_approval_required"
    assert exc_info.value.retryable is False
    session.request.assert_not_called()


@LOCKDOWN
def test_whatsapp_unguarded_send_is_blocked_before_http_request():
    session = Mock()
    client = WhatsAppCloudClient(api_version="v25.0", session=session)

    with pytest.raises(WhatsAppCloudAPIError) as exc_info:
        client.send_template(
            phone_number_id="123456789",
            access_token="must-not-leave-process",
            recipient="15551234567",
            template_name="approved_test_template",
            language_code="en_US",
        )

    assert exc_info.value.error_code == "execution_approval_required"
    assert exc_info.value.retryable is False
    session.post.assert_not_called()


@LOCKDOWN
def test_linkedin_unguarded_invitation_is_blocked_before_http_request():
    session = Mock()
    client = LinkedInInvitationsClient(session=session)

    with pytest.raises(LinkedInInvitationsAPIError) as exc_info:
        client.send_email_invitation(
            access_token="must-not-leave-process",
            recipient_email="approved-test@example.com",
            message_body="Dedicated integration test",
        )

    assert exc_info.value.error_code == "execution_approval_required"
    assert exc_info.value.retryable is False
    session.post.assert_not_called()


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SDR_NURTURE_TRACKING_BASE_URL="https://crm.example.test",
)
def test_email_unguarded_delivery_is_blocked_before_backend_send(
    admin_client,
    org_a,
):
    # Setup remains local database work; the provider boundary itself is mocked.
    with override_settings(ALLOW_UNGUARDED_PROVIDER_IO=True):
        create_sequence(admin_client)
        enrollment = accept_and_process(
            admin_client,
            org_a,
            "provider-lockdown-email-1",
        )
    delivery = LeadNurtureDelivery.objects.get(
        enrollment=enrollment,
        step_position=1,
    )

    with patch("sdr.nurture.EmailMultiAlternatives.send") as provider_send:
        with pytest.raises(PermanentJobError) as exc_info:
            process_nurture_email_job(
                {"org_id": str(org_a.id), "delivery_id": str(delivery.id)}
            )

    assert getattr(exc_info.value, "code", "") == "execution_approval_required"
    provider_send.assert_not_called()
