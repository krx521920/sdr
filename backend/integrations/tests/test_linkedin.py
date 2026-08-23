import pytest
from django.test import override_settings

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from integrations.models import (
    LinkedInConnection,
    LinkedInInvitation,
    LinkedInInvitationStatus,
)
from integrations.providers.linkedin.client import (
    LinkedInInvitationResponse,
    LinkedInInvitationsClient,
)
from sdr.models import SDROutboundCampaign, SDROutboundProspect


class AcceptedResponse:
    status_code = 201
    headers = {"x-linkedin-id": "urn:li:invitation:accepted-1"}
    content = b""


class RecordingSession:
    def __init__(self):
        self.call = None

    def post(self, url, *, headers, json, timeout):
        self.call = (url, headers, json, timeout)
        return AcceptedResponse()


def test_linkedin_client_sends_official_invitation_payload():
    session = RecordingSession()
    client = LinkedInInvitationsClient(session=session)

    response = client.send_email_invitation(
        access_token="partner-token",
        recipient_email="Buyer@Example.com",
        message_body="Let's connect.",
    )

    url, headers, payload, timeout = session.call
    assert response.invitation_id == "urn:li:invitation:accepted-1"
    assert url == "https://api.linkedin.com/v2/invitations"
    assert headers == {
        "Authorization": "Bearer partner-token",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    assert payload == {
        "invitee": "urn:li:email:buyer@example.com",
        "message": {
            "com.linkedin.invitations.InvitationMessage": {
                "body": "Let's connect."
            }
        },
    }
    assert timeout == 10.0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_linkedin_connection_requires_confirmed_partner_access(admin_client, org_a):
    rejected = admin_client.put(
        "/api/integrations/linkedin/connection/",
        {
            "access_token": "official-partner-token",
            "is_active": True,
            "partner_access_confirmed": False,
        },
        format="json",
    )
    assert rejected.status_code == 400
    assert not LinkedInConnection.objects.filter(org=org_a).exists()

    response = admin_client.put(
        "/api/integrations/linkedin/connection/",
        {
            "access_token": "official-partner-token",
            "is_active": True,
            "partner_access_confirmed": True,
        },
        format="json",
    )
    assert response.status_code == 200, response.json()
    connection = LinkedInConnection.objects.get(org=org_a)
    assert connection.access_token_ciphertext != "official-partner-token"
    assert connection.get_access_token() == "official-partner-token"
    assert response.json()["access_token_configured"] is True
    assert "access_token" not in response.json()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_linkedin_only_campaign_promotes_and_sends_invitation(
    admin_client,
    org_a,
    monkeypatch,
):
    configured = admin_client.put(
        "/api/integrations/linkedin/connection/",
        {
            "access_token": "official-partner-token",
            "is_active": True,
            "partner_access_confirmed": True,
        },
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    created = admin_client.post(
        "/api/sdr/outbound/campaigns/",
        {
            "name": "LinkedIn industrial buyers",
            "channels": ["linkedin"],
            "linkedin_invitation_message": (
                "Hi {{ first_name }}, I'd like to learn more about {{ company_name }}."
            ),
        },
        format="json",
    )
    assert created.status_code == 201, created.json()
    campaign = SDROutboundCampaign.objects.get(id=created.json()["id"])
    imported = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {
            "csv_text": (
                "company_name,first_name,email,linkedin_url\n"
                "Factory One,Ada,ada@example.com,https://linkedin.com/in/ada"
            )
        },
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

    invitation = LinkedInInvitation.objects.get(prospect=prospect)
    assert invitation.recipient == "ada@example.com"
    assert invitation.message_body == (
        "Hi Ada, I'd like to learn more about Factory One."
    )

    class AcceptedClient:
        def send_email_invitation(self, **kwargs):
            assert kwargs == {
                "access_token": "official-partner-token",
                "recipient_email": "ada@example.com",
                "message_body": invitation.message_body,
            }
            return LinkedInInvitationResponse(
                invitation_id="urn:li:invitation:campaign-1",
                snapshot={"accepted": True},
            )

    monkeypatch.setattr(
        "integrations.providers.linkedin.outbound._client",
        lambda: AcceptedClient(),
    )
    send_job = AutomationJob.objects.get(
        name="linkedin.send_campaign_invitation",
        payload__invitation_id=str(invitation.id),
    )
    run_automation_job.run(str(send_job.id), str(org_a.id))
    invitation.refresh_from_db()
    assert invitation.status == LinkedInInvitationStatus.SENT
    assert invitation.provider_invitation_id == "urn:li:invitation:campaign-1"

    analytics = admin_client.get(
        f"/api/sdr/outbound/campaigns/{campaign.id}/analytics/"
    )
    assert analytics.status_code == 200, analytics.json()
    assert analytics.json()["linkedin"] == {
        "queued": 0,
        "sent": 1,
        "failed": 0,
        "skipped": 0,
    }

    invitation.status = LinkedInInvitationStatus.FAILED
    invitation.attempt_count = 1
    invitation.save(update_fields=["status", "attempt_count", "updated_at"])
    retried = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "retry_failed"},
        format="json",
    )
    assert retried.status_code == 200, retried.json()
    assert retried.json()["execution"]["linkedin_queued"] == 1
    assert (
        AutomationJob.objects.filter(
            name="linkedin.send_campaign_invitation",
            payload__invitation_id=str(invitation.id),
        ).count()
        == 2
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_linkedin_campaign_launch_is_blocked_without_partner_connection(
    admin_client,
):
    created = admin_client.post(
        "/api/sdr/outbound/campaigns/",
        {"name": "Unconfigured LinkedIn", "channels": ["linkedin"]},
        format="json",
    )
    launched = admin_client.post(
        f"/api/sdr/outbound/campaigns/{created.json()['id']}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launched.status_code == 409
    assert "approved partner API access" in launched.json()["detail"]
