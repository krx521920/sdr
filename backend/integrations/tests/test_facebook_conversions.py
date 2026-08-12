from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.models import AutomationJob
from integrations.models import (
    FacebookConversionEvent,
    FacebookConversionEventStatus,
    FacebookConversionSettings,
)
from integrations.providers.facebook.conversions import (
    FACEBOOK_CONVERSION_JOB,
    process_facebook_conversion_job,
    schedule_conversion_events_for_intake,
)
from leads.models import Lead
from sdr.models import LeadIntake, LeadIntakeStatus


class FakeConversionClient:
    def __init__(self):
        self.calls = []

    def send_conversion_event(
        self,
        *,
        pixel_id,
        access_token,
        event,
        test_event_code,
    ):
        self.calls.append(
            {
                "pixel_id": pixel_id,
                "access_token": access_token,
                "event": event,
                "test_event_code": test_event_code,
            }
        )
        return {"events_received": 1, "fbtrace_id": "trace-42"}


def conversion_settings(org, **overrides):
    values = {
        "is_enabled": True,
        "pixel_id": "987654321",
        "lead_event_source": "BottleCRM",
        "qualified_bands": ["high"],
        "test_event_code": "TEST42",
    }
    values.update(overrides)
    configuration = FacebookConversionSettings(org=org, **values)
    configuration.set_access_token("secret-conversion-token")
    configuration.save()
    return configuration


def facebook_intake(org, *, band="high", lead=None, processed_at=None):
    lead = lead or Lead.objects.create(
        org=org,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        status="assigned",
    )
    return LeadIntake.objects.create(
        org=org,
        source="facebook_ad",
        source_record_id="1234567890123456",
        raw_payload={"lead": {"id": "1234567890123456"}},
        status=LeadIntakeStatus.COMPLETED,
        qualification_score=88,
        qualification_band=band,
        crm_lead=lead,
        processed_at=processed_at or timezone.now(),
    )


@pytest.mark.django_db
def test_qualified_facebook_intake_queues_idempotent_funnel_events(
    org_a,
    monkeypatch,
):
    conversion_settings(org_a)
    intake = facebook_intake(org_a)
    dispatched = []
    monkeypatch.setattr(
        "integrations.providers.facebook.conversions.dispatch_job",
        lambda job: dispatched.append(job.id),
    )

    first = schedule_conversion_events_for_intake(intake)
    replay = schedule_conversion_events_for_intake(intake)

    assert [event.event_name for event in first] == [
        "RawLead",
        "MarketingQualifiedLead",
    ]
    assert [event.id for event in replay] == [event.id for event in first]
    assert FacebookConversionEvent.objects.count() == 2
    assert AutomationJob.objects.filter(name=FACEBOOK_CONVERSION_JOB).count() == 2
    job_ids = [job.id for job in AutomationJob.objects.order_by("created_at")]
    assert dispatched == job_ids + job_ids


@pytest.mark.django_db
def test_conversion_job_sends_lead_id_only_and_records_provider_result(
    org_a,
    monkeypatch,
):
    conversion_settings(org_a)
    intake = facebook_intake(org_a)
    monkeypatch.setattr(
        "integrations.providers.facebook.conversions.dispatch_job",
        lambda job: None,
    )
    event = schedule_conversion_events_for_intake(intake)[1]
    client = FakeConversionClient()

    result = process_facebook_conversion_job(
        {"org_id": str(org_a.id), "event_id": str(event.id)},
        client=client,
    )

    event.refresh_from_db()
    assert result["status"] == FacebookConversionEventStatus.SENT
    assert event.status == FacebookConversionEventStatus.SENT
    assert event.provider_trace_id == "trace-42"
    payload = client.calls[0]["event"]
    assert payload["user_data"] == {"lead_id": 1234567890123456}
    assert payload["action_source"] == "system_generated"
    assert payload["custom_data"] == {
        "event_source": "crm",
        "lead_event_source": "BottleCRM",
    }
    assert "ada@example.com" not in str(payload)


@pytest.mark.django_db
def test_expired_conversion_event_is_not_sent(org_a, monkeypatch):
    conversion_settings(org_a)
    intake = facebook_intake(
        org_a,
        processed_at=timezone.now() - timedelta(days=8),
    )
    monkeypatch.setattr(
        "integrations.providers.facebook.conversions.dispatch_job",
        lambda job: None,
    )
    event = schedule_conversion_events_for_intake(intake)[0]

    with pytest.raises(PermanentJobError, match="older than seven days"):
        process_facebook_conversion_job(
            {"org_id": str(org_a.id), "event_id": str(event.id)},
            client=FakeConversionClient(),
        )

    event.refresh_from_db()
    assert event.status == FacebookConversionEventStatus.FAILED
    assert event.error_code == "conversion_event_expired"


@pytest.mark.django_db
def test_crm_conversion_status_queues_one_meta_event(
    org_a,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    conversion_settings(org_a)
    intake = facebook_intake(org_a)
    monkeypatch.setattr(
        "integrations.providers.facebook.conversions.dispatch_job",
        lambda job: None,
    )

    with django_capture_on_commit_callbacks(execute=True):
        intake.crm_lead.status = "converted"
        intake.crm_lead.save()
    with django_capture_on_commit_callbacks(execute=True):
        intake.crm_lead.company_name = "Analytical Engines"
        intake.crm_lead.save()

    converted = FacebookConversionEvent.objects.filter(event_name="Converted")
    assert converted.count() == 1
    assert converted.get().event_key == f"facebook:converted:{intake.id}"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_admin_configures_encrypted_conversion_feedback_per_tenant(
    org_a,
    admin_client,
    org_b_client,
):
    response = admin_client.put(
        "/api/integrations/facebook/conversions/",
        {
            "is_enabled": True,
            "pixel_id": "987654321",
            "access_token": "tenant-secret-token",
            "lead_event_source": "BottleCRM",
            "raw_lead_event_name": "RawLead",
            "qualified_lead_event_name": "MarketingQualifiedLead",
            "converted_event_name": "Converted",
            "qualified_bands": ["high"],
            "test_event_code": "",
        },
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["access_token_configured"] is True
    assert "access_token" not in response.json()
    configuration = FacebookConversionSettings.objects.get(org=org_a)
    assert configuration.get_access_token() == "tenant-secret-token"
    assert "tenant-secret-token" not in configuration.access_token_ciphertext

    other_tenant = org_b_client.get("/api/integrations/facebook/conversions/")
    assert other_tenant.status_code == 200
    assert other_tenant.json()["is_enabled"] is False
    assert other_tenant.json()["access_token_configured"] is False
