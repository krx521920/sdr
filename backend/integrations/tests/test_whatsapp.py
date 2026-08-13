import hashlib
import hmac
import json

import pytest
from django.test import override_settings

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from integrations.models import (
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
)
from integrations.providers.whatsapp.client import WhatsAppCloudClient
from sdr.models import LeadNurtureEnrollment, SDROutboundCampaign, SDROutboundProspect


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": "15551234567"}],
            "messages": [{"id": "wamid.accepted-1"}],
        }


class FakeSession:
    def __init__(self):
        self.call = None

    def post(self, url, *, headers, json, timeout):
        self.call = (url, headers, json, timeout)
        return FakeResponse()


def test_whatsapp_client_sends_official_template_payload():
    session = FakeSession()
    client = WhatsAppCloudClient(api_version="v25.0", session=session)

    result = client.send_template(
        phone_number_id="123456789",
        access_token="secret-token",
        recipient="15551234567",
        template_name="hello_world",
        language_code="en_US",
    )

    url, headers, payload, timeout = session.call
    assert result["messages"][0]["id"] == "wamid.accepted-1"
    assert url == "https://graph.facebook.com/v25.0/123456789/messages"
    assert headers["Authorization"] == "Bearer secret-token"
    assert payload == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "template",
        "template": {
            "name": "hello_world",
            "language": {"code": "en_US"},
        },
    }
    assert timeout == 10.0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_whatsapp_connection_api_encrypts_token(admin_client, org_a):
    response = admin_client.put(
        "/api/integrations/whatsapp/connection/",
        {
            "phone_number_id": "123456789",
            "business_account_id": "987654321",
            "display_phone_number": "+1 555 123 4567",
            "access_token": "permanent-system-user-token",
            "is_active": True,
        },
        format="json",
    )

    assert response.status_code == 200, response.json()
    connection = WhatsAppBusinessConnection.objects.get(org=org_a)
    assert connection.phone_number_id == "123456789"
    assert connection.access_token_ciphertext != "permanent-system-user-token"
    assert connection.get_access_token() == "permanent-system-user-token"
    assert response.json()["access_token_configured"] is True
    assert "access_token" not in response.json()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_whatsapp_only_campaign_promotes_and_sends_template(
    admin_client,
    org_a,
    monkeypatch,
):
    configured = admin_client.put(
        "/api/integrations/whatsapp/connection/",
        {
            "phone_number_id": "123456789",
            "access_token": "system-user-token",
            "is_active": True,
        },
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    created = admin_client.post(
        "/api/sdr/outbound/campaigns/",
        {
            "name": "WhatsApp industrial buyers",
            "channels": ["whatsapp"],
            "whatsapp_template_name": "industrial_intro",
            "whatsapp_template_language": "en_US",
        },
        format="json",
    )
    assert created.status_code == 201, created.json()
    campaign = SDROutboundCampaign.objects.get(id=created.json()["id"])
    imported = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {"csv_text": "company_name,phone\nFactory One,+1 555 123 4567"},
        format="json",
    )
    assert imported.status_code == 201, imported.json()
    launched = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launched.status_code == 200, launched.json()
    assert launched.json()["execution"]["queued"] == 1

    prospect = SDROutboundProspect.objects.get(campaign=campaign)
    promotion_job = AutomationJob.objects.get(
        name="sdr.process_outbound_prospect",
        payload__prospect_id=str(prospect.id),
    )
    run_automation_job.run(str(promotion_job.id), str(org_a.id))
    prospect.refresh_from_db()
    assert prospect.status == "promoted"
    assert prospect.intake_id is not None
    assert not LeadNurtureEnrollment.objects.filter(intake=prospect.intake).exists()

    class AcceptedClient:
        def send_template(self, **kwargs):
            assert kwargs["recipient"] == "15551234567"
            assert kwargs["template_name"] == "industrial_intro"
            return {"messages": [{"id": "wamid.campaign-1"}]}

    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        lambda: AcceptedClient(),
    )
    message = WhatsAppMessage.objects.get(prospect=prospect)
    send_job = AutomationJob.objects.get(
        name="whatsapp.send_campaign_message",
        payload__message_id=str(message.id),
    )
    run_automation_job.run(str(send_job.id), str(org_a.id))
    message.refresh_from_db()
    assert message.status == WhatsAppMessageStatus.SENT
    assert message.provider_message_id == "wamid.campaign-1"

    message.status = WhatsAppMessageStatus.FAILED
    message.attempt_count = 1
    message.save(update_fields=["status", "attempt_count", "updated_at"])
    retried = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "retry_failed"},
        format="json",
    )
    assert retried.status_code == 200, retried.json()
    assert retried.json()["execution"]["whatsapp_queued"] == 1
    assert (
        AutomationJob.objects.filter(
            name="whatsapp.send_campaign_message",
            payload__message_id=str(message.id),
        ).count()
        == 2
    )


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    META_APP_SECRET="webhook-secret",
    WHATSAPP_WEBHOOK_VERIFY_TOKEN="verify-me",
)
def test_signed_whatsapp_webhook_updates_delivery_status(
    admin_client,
    unauthenticated_client,
    org_a,
):
    admin_client.put(
        "/api/integrations/whatsapp/connection/",
        {
            "phone_number_id": "123456789",
            "access_token": "system-user-token",
            "is_active": True,
        },
        format="json",
    )
    campaign = SDROutboundCampaign.objects.create(
        org=org_a,
        name="Webhook campaign",
        channels=["whatsapp"],
        whatsapp_template_name="hello_world",
        status="active",
        run_count=1,
    )
    prospect = SDROutboundProspect.objects.create(
        org=org_a,
        campaign=campaign,
        company_name="Factory Two",
        phone="15557654321",
        dedupe_key="phone:webhook",
    )
    connection = WhatsAppBusinessConnection.objects.get(org=org_a)
    message = WhatsAppMessage.objects.create(
        org=org_a,
        connection=connection,
        campaign=campaign,
        prospect=prospect,
        campaign_run=1,
        recipient="15557654321",
        template_name="hello_world",
        status=WhatsAppMessageStatus.SENT,
        provider_message_id="wamid.webhook-1",
    )
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "123456789"},
                            "statuses": [
                                {
                                    "id": "wamid.webhook-1",
                                    "status": "delivered",
                                    "timestamp": "1786500000",
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        "sha256="
        + hmac.new(
            b"webhook-secret",
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    response = unauthenticated_client.post(
        "/api/integrations/whatsapp/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
    )

    assert response.status_code == 200, response.content
    assert response.json() == {"processed": 1, "ignored": 0}
    message.refresh_from_db()
    assert message.status == WhatsAppMessageStatus.DELIVERED
    assert message.delivered_at is not None

    verification = unauthenticated_client.get(
        "/api/integrations/whatsapp/webhook/",
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-42",
        },
    )
    assert verification.status_code == 200
    assert verification.content == b"challenge-42"
