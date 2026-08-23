from datetime import datetime
from datetime import timezone as datetime_timezone

import pytest
from django.test import override_settings

from leads.models import Lead
from sdr.email_safety import reserve_delivery_send
from sdr.models import (
    LeadIntake,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    OutboundCampaignStatus,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRResponseSettings,
)


def make_outbound_delivery(
    org,
    *,
    suffix: str,
    recipient_timezone: str = "",
) -> LeadNurtureDelivery:
    email = f"{suffix}@example.com"
    lead = Lead.objects.create(org=org, email=email, status="in process")
    intake = LeadIntake.objects.create(
        org=org,
        source="outbound",
        source_record_id=f"safety:{suffix}",
        status="completed",
        crm_lead=lead,
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org,
        name=f"Safety sequence {suffix}",
        is_active=True,
        sources=["outbound"],
        from_email="sales@example.com",
    )
    step = SDRNurtureStep.objects.create(
        org=org,
        sequence=sequence,
        position=1,
        subject_a="Hello",
        body_a="Hello",
    )
    campaign = SDROutboundCampaign.objects.create(
        org=org,
        name=f"Safety campaign {suffix}",
        channels=["email"],
        sequence=sequence,
        status=OutboundCampaignStatus.ACTIVE,
        run_count=1,
    )
    SDROutboundProspect.objects.create(
        org=org,
        campaign=campaign,
        company_name=f"Safety {suffix}",
        email=email,
        country="CN",
        recipient_timezone=recipient_timezone,
        dedupe_key=f"safety-{suffix}",
        intake=intake,
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    return LeadNurtureDelivery.objects.create(
        org=org,
        enrollment=enrollment,
        step=step,
        step_position=1,
        recipient=email,
        subject_template="Hello",
        body_template="Hello",
        scheduled_for=datetime(2026, 8, 12, tzinfo=datetime_timezone.utc),
    )


@pytest.mark.django_db
def test_organization_daily_send_limit_defers_the_next_delivery(org_a):
    now = datetime(2026, 8, 12, 12, tzinfo=datetime_timezone.utc)
    first = make_outbound_delivery(org_a, suffix="quota-one")
    second = make_outbound_delivery(org_a, suffix="quota-two")
    first.status = NurtureDeliveryStatus.SENT
    first.sent_at = now
    first.save(update_fields=["status", "sent_at", "updated_at"])
    SDRResponseSettings.objects.create(
        org=org_a,
        org_daily_send_limit=1,
        default_recipient_timezone="UTC",
    )

    decision = reserve_delivery_send(second, now=now)

    assert decision.allowed is False
    assert decision.reason == "organization_daily_send_limit"
    assert decision.used_today == 1
    second.refresh_from_db()
    assert second.status == NurtureDeliveryStatus.PENDING
    assert second.deferral_count == 1
    assert second.scheduled_for == datetime(2026, 8, 13, tzinfo=datetime_timezone.utc)


@pytest.mark.django_db
def test_recipient_timezone_defers_until_local_working_window(org_a):
    delivery = make_outbound_delivery(
        org_a,
        suffix="working-hours",
        recipient_timezone="Asia/Shanghai",
    )
    SDRResponseSettings.objects.create(
        org=org_a,
        enforce_recipient_working_hours=True,
        recipient_send_weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    before_open = datetime(2026, 8, 12, 0, tzinfo=datetime_timezone.utc)

    decision = reserve_delivery_send(delivery, now=before_open)

    assert decision.allowed is False
    assert decision.reason == "recipient_working_hours"
    assert decision.next_attempt_at == datetime(
        2026, 8, 12, 1, tzinfo=datetime_timezone.utc
    )
    delivery.refresh_from_db()
    assert delivery.deferral_count == 1
    assert delivery.scheduled_for == decision.next_attempt_at


@pytest.mark.django_db
def test_campaign_safety_hold_blocks_a_send_reservation(org_a):
    delivery = make_outbound_delivery(org_a, suffix="held")
    campaign = delivery.enrollment.intake.outbound_prospect.campaign
    campaign.safety_hold = True
    campaign.save(update_fields=["safety_hold", "updated_at"])

    decision = reserve_delivery_send(delivery)

    assert decision.allowed is False
    assert decision.reason == "campaign_safety_hold"
    delivery.refresh_from_db()
    assert delivery.status == NurtureDeliveryStatus.PENDING
    assert delivery.attempt_count == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_email_safety_settings_api_validates_timezone_and_window(admin_client):
    url = "/api/sdr/response-settings/"
    invalid_timezone = admin_client.patch(
        url,
        {"default_recipient_timezone": "Mars/Phobos"},
        format="json",
    )
    invalid_window = admin_client.patch(
        url,
        {
            "recipient_send_window_start": "17:00:00",
            "recipient_send_window_end": "09:00:00",
        },
        format="json",
    )
    saved = admin_client.patch(
        url,
        {
            "org_daily_send_limit": 250,
            "bounce_rate_threshold": "4.50",
            "complaint_rate_threshold": "0.20",
            "safety_min_sample_size": 50,
            "safety_window_days": 14,
            "enforce_recipient_working_hours": True,
            "default_recipient_timezone": "Asia/Shanghai",
            "recipient_send_weekdays": [0, 1, 2, 3, 4],
        },
        format="json",
    )

    assert invalid_timezone.status_code == 400
    assert invalid_window.status_code == 400
    assert saved.status_code == 200, saved.json()
    assert saved.json()["org_daily_send_limit"] == 250
    assert saved.json()["default_recipient_timezone"] == "Asia/Shanghai"
