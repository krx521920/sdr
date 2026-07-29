import pytest

from automation.models import AutomationJob
from integrations.models import FacebookPageConnection, FacebookPageRoute
from integrations.providers.facebook.jobs import enqueue_facebook_lead_event
from integrations.providers.facebook.service import (
    FacebookConnectionUnavailable,
    FacebookPageAlreadyConnected,
    connect_facebook_page,
    process_facebook_lead_event,
)
from sdr.models import LeadIntake, LeadIntakeStatus


class FakeGraphClient:
    def __init__(self, *, page_id="page-42", page_name="Acme Page"):
        self.page_id = page_id
        self.page_name = page_name
        self.fetched_leads = []
        self.subscriptions = []

    def fetch_page_identity(self, *, access_token):
        assert access_token
        return {"id": self.page_id, "name": self.page_name}

    def fetch_lead(self, *, leadgen_id, access_token):
        self.fetched_leads.append((leadgen_id, access_token))
        return {
            "id": leadgen_id,
            "created_time": "2026-07-29T08:00:00+0000",
            "field_data": [
                {"name": "full_name", "values": ["Ada Lovelace"]},
                {"name": "email", "values": ["ada@example.com"]},
                {"name": "company_name", "values": ["Acme"]},
                {"name": "job_title", "values": ["VP Sales"]},
            ],
        }

    def subscribe_page(self, *, page_id, access_token):
        self.subscriptions.append((page_id, access_token))


@pytest.mark.django_db
def test_page_token_is_validated_encrypted_and_tenant_owned(org_a):
    connection = connect_facebook_page(
        org_id=org_a.id,
        page_access_token="very-secret-page-token",
        client=FakeGraphClient(),
    )

    connection.refresh_from_db()
    assert connection.page_id == "page-42"
    assert connection.access_token_ciphertext != "very-secret-page-token"
    assert "very-secret-page-token" not in connection.access_token_ciphertext
    assert connection.get_access_token() == "very-secret-page-token"
    assert connection.access_token_hint == "ge-token"
    assert connection.route.page_id == "page-42"


@pytest.mark.django_db
def test_same_page_cannot_be_claimed_by_two_tenants(org_a, org_b):
    connect_facebook_page(
        org_id=org_a.id,
        page_access_token="token-a",
        client=FakeGraphClient(),
    )

    with pytest.raises(FacebookPageAlreadyConnected):
        connect_facebook_page(
            org_id=org_b.id,
            page_access_token="token-b",
            client=FakeGraphClient(),
        )

    assert FacebookPageRoute.objects.get(page_id="page-42").org_id == org_a.id
    assert FacebookPageConnection.objects.count() == 1


@pytest.mark.django_db
def test_facebook_lead_runs_shared_pipeline_idempotently(org_a, admin_profile):
    client = FakeGraphClient()
    connect_facebook_page(
        org_id=org_a.id,
        page_access_token="page-token",
        client=client,
    )
    event = {
        "page_id": "page-42",
        "leadgen_id": "lead-7",
        "form_id": "form-3",
        "created_time": 1_700_000_000,
    }

    first = process_facebook_lead_event(event_payload=event, client=client)
    second = process_facebook_lead_event(event_payload=event, client=client)

    intake = LeadIntake.objects.get(id=first.intake_id)
    assert first.crm_created is True
    assert second.replayed is True
    assert second.lead_id == first.lead_id
    assert intake.status == LeadIntakeStatus.COMPLETED
    assert intake.source == "facebook_ad"
    assert intake.attempt_count == 1
    assert intake.crm_lead.email == "ada@example.com"
    assert client.fetched_leads == [
        ("lead-7", "page-token"),
        ("lead-7", "page-token"),
    ]


@pytest.mark.django_db
def test_facebook_event_is_persisted_before_dispatch(org_a, monkeypatch):
    connect_facebook_page(
        org_id=org_a.id,
        page_access_token="page-token",
        client=FakeGraphClient(),
    )
    dispatched = []
    monkeypatch.setattr(
        "integrations.providers.facebook.jobs.dispatch_job",
        lambda job: dispatched.append(job.id),
    )
    event = {"page_id": "page-42", "leadgen_id": "lead-durable"}

    first = enqueue_facebook_lead_event(event)
    replay = enqueue_facebook_lead_event(event)

    assert first.id == replay.id
    assert (
        AutomationJob.objects.filter(
            org=org_a,
            name="facebook.process_lead",
            idempotency_key="leadgen:lead-durable",
        ).count()
        == 1
    )
    assert dispatched == [first.id, first.id]


@pytest.mark.django_db
def test_durable_facebook_job_cannot_cross_tenants(org_a, org_b):
    connect_facebook_page(
        org_id=org_a.id,
        page_access_token="page-token",
        client=FakeGraphClient(),
    )

    with pytest.raises(FacebookConnectionUnavailable, match="job's organization"):
        process_facebook_lead_event(
            event_payload={"page_id": "page-42", "leadgen_id": "lead-tenant"},
            expected_org_id=org_b.id,
            client=FakeGraphClient(),
        )
