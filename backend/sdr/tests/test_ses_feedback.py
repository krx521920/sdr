import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from cases.inbound.sns import SNSVerificationError
from leads.models import Lead
from sdr import ses_views
from sdr.models import (
    LeadIntake,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    OutboundCampaignStatus,
    SDREmailProviderEvent,
    SDREmailSuppression,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRResponseSettings,
)


def sent_delivery(org, *, email="ada@example.com", provider_message_id="ses-message-1"):
    lead = Lead.objects.create(org=org, email=email, status="in process")
    intake = LeadIntake.objects.create(
        org=org,
        source="website_form",
        source_record_id=f"ses-feedback:{email}",
        status="completed",
        crm_lead=lead,
        processed_at=timezone.now(),
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org,
        name=f"SES feedback {email}",
        is_active=True,
    )
    step = SDRNurtureStep.objects.create(
        org=org,
        sequence=sequence,
        position=1,
        subject_a="Hello",
        body_a="Hello",
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    delivery = LeadNurtureDelivery.objects.create(
        org=org,
        enrollment=enrollment,
        step=step,
        step_position=1,
        recipient=email,
        subject_template="Hello",
        body_template="Hello",
        status=NurtureDeliveryStatus.SENT,
        scheduled_for=timezone.now() - timedelta(minutes=2),
        sent_at=timezone.now() - timedelta(minutes=1),
        provider_message_id=provider_message_id,
    )
    return delivery


def feedback_message(delivery, event_type, *, recipient=None, bounce_type="Permanent"):
    recipient = recipient or delivery.recipient
    section = {"timestamp": "2026-07-30T12:00:00Z"}
    if event_type == "Bounce":
        section.update(
            {
                "bounceType": bounce_type,
                "bounceSubType": "General",
                "feedbackId": f"bounce-{delivery.id}",
                "bouncedRecipients": [
                    {
                        "emailAddress": recipient,
                        "action": "failed",
                        "status": "5.1.1",
                        "diagnosticCode": "smtp; 550 mailbox unavailable",
                    }
                ],
            }
        )
    elif event_type == "Complaint":
        section.update(
            {
                "feedbackId": f"complaint-{delivery.id}",
                "complaintFeedbackType": "abuse",
                "complainedRecipients": [{"emailAddress": recipient}],
            }
        )
    else:
        section.update(
            {
                "recipients": [recipient],
                "processingTimeMillis": 842,
            }
        )
    return json.dumps(
        {
            "notificationType": event_type,
            "mail": {
                "messageId": delivery.provider_message_id,
                "timestamp": "2026-07-30T11:59:00Z",
                "tags": {
                    "sdr_org": [str(delivery.org_id)],
                    "sdr_delivery": [str(delivery.id)],
                },
            },
            event_type.lower(): section,
        }
    )


def post_feedback(client, delivery, event_type, *, event_id, **message_kwargs):
    return client.post(
        "/api/sdr/public/ses-feedback/",
        {
            "Type": "Notification",
            "MessageId": event_id,
            "Message": feedback_message(delivery, event_type, **message_kwargs),
        },
        format="json",
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_permanent_ses_bounce_is_idempotent_and_suppresses_email(
    admin_client,
    unauthenticated_client,
    org_a,
    monkeypatch,
):
    monkeypatch.setattr(ses_views, "verify_sns_message", lambda payload: None)
    delivery = sent_delivery(org_a)

    first = post_feedback(
        unauthenticated_client,
        delivery,
        "Bounce",
        event_id="sns-bounce-1",
    )
    replay = post_feedback(
        unauthenticated_client,
        delivery,
        "Bounce",
        event_id="sns-bounce-1",
    )

    assert first.status_code == 200
    assert first.json()["status"] == "processed"
    assert replay.json()["status"] == "duplicate"
    delivery.refresh_from_db()
    delivery.enrollment.refresh_from_db()
    assert delivery.bounced_at is not None
    assert delivery.bounce_type == "Permanent"
    assert delivery.bounce_subtype == "General"
    assert delivery.enrollment.status == NurtureEnrollmentStatus.CANCELLED
    assert SDREmailProviderEvent.objects.filter(delivery=delivery).count() == 1
    assert SDREmailSuppression.objects.filter(
        org=org_a,
        email=delivery.recipient,
        reason="hard_bounce",
        source="provider",
        is_active=True,
    ).exists()

    summary = admin_client.get("/api/sdr/nurture/sequences/").json()["summary"]
    assert summary["bounced"] == 1
    assert summary["bounce_rate"] == 100.0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_ses_complaint_suppresses_and_delivery_event_updates_metrics(
    admin_client,
    unauthenticated_client,
    org_a,
    monkeypatch,
):
    monkeypatch.setattr(ses_views, "verify_sns_message", lambda payload: None)
    delivery = sent_delivery(
        org_a,
        email="complaint@example.com",
        provider_message_id="ses-message-complaint",
    )

    delivered = post_feedback(
        unauthenticated_client,
        delivery,
        "Delivery",
        event_id="sns-delivery-1",
    )
    complained = post_feedback(
        unauthenticated_client,
        delivery,
        "Complaint",
        event_id="sns-complaint-1",
    )

    assert delivered.json()["event_type"] == "delivery"
    assert complained.json()["event_type"] == "complaint"
    delivery.refresh_from_db()
    assert delivery.delivered_at is not None
    assert delivery.complained_at is not None
    assert SDREmailSuppression.objects.filter(
        email=delivery.recipient,
        reason="complaint",
        is_active=True,
    ).exists()
    summary = admin_client.get("/api/sdr/nurture/sequences/").json()["summary"]
    assert summary["delivery_rate"] == 100.0
    assert summary["complaint_rate"] == 100.0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_ses_bounce_threshold_auto_pauses_outbound_campaign(
    admin_client,
    unauthenticated_client,
    org_a,
    monkeypatch,
):
    monkeypatch.setattr(ses_views, "verify_sns_message", lambda payload: None)
    delivery = sent_delivery(
        org_a,
        email="circuit-breaker@example.com",
        provider_message_id="ses-message-circuit-breaker",
    )
    campaign = SDROutboundCampaign.objects.create(
        org=org_a,
        name="Circuit breaker",
        channels=["email"],
        sequence=delivery.enrollment.sequence,
        status=OutboundCampaignStatus.ACTIVE,
        run_count=1,
    )
    SDROutboundProspect.objects.create(
        org=org_a,
        campaign=campaign,
        company_name="Circuit Breaker Inc",
        email=delivery.recipient,
        dedupe_key="circuit-breaker",
        intake=delivery.enrollment.intake,
    )
    SDRResponseSettings.objects.create(
        org=org_a,
        safety_min_sample_size=1,
        bounce_rate_threshold=5,
        complaint_rate_threshold=100,
    )

    response = post_feedback(
        unauthenticated_client,
        delivery,
        "Bounce",
        event_id="sns-bounce-circuit-breaker",
    )

    assert response.status_code == 200
    assert response.json()["campaign_safety"]["paused"] is True
    campaign.refresh_from_db()
    delivery.enrollment.refresh_from_db()
    assert campaign.status == OutboundCampaignStatus.PAUSED
    assert campaign.safety_hold is True
    assert campaign.safety_snapshot["bounce_rate"] == 100.0
    # The bounced recipient is cancelled by suppression; the campaign hold
    # pauses every other still-active enrollment in the same campaign.
    assert delivery.enrollment.status == NurtureEnrollmentStatus.CANCELLED

    blocked = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    cleared = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "clear_safety_hold"},
        format="json",
    )
    assert blocked.status_code == 409
    assert cleared.status_code == 200
    assert cleared.json()["execution"]["cleared"] is True
    campaign.refresh_from_db()
    assert campaign.safety_hold is False
    assert campaign.safety_cleared_at is not None


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_transient_bounce_is_audited_without_suppression(
    unauthenticated_client,
    org_a,
    monkeypatch,
):
    monkeypatch.setattr(ses_views, "verify_sns_message", lambda payload: None)
    delivery = sent_delivery(
        org_a,
        email="temporary@example.com",
        provider_message_id="ses-message-temporary",
    )

    response = post_feedback(
        unauthenticated_client,
        delivery,
        "Bounce",
        event_id="sns-bounce-transient-1",
        bounce_type="Transient",
    )

    assert response.status_code == 200
    assert response.json()["suppression_id"] is None
    delivery.refresh_from_db()
    delivery.enrollment.refresh_from_db()
    assert delivery.bounce_type == "Transient"
    assert delivery.enrollment.status == NurtureEnrollmentStatus.ACTIVE
    assert not SDREmailSuppression.objects.filter(email=delivery.recipient).exists()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_ses_feedback_rejects_bad_signature_and_ignores_recipient_mismatch(
    unauthenticated_client,
    org_a,
    monkeypatch,
):
    delivery = sent_delivery(
        org_a,
        email="secure@example.com",
        provider_message_id="ses-message-secure",
    )

    def reject_signature(payload):
        raise SNSVerificationError("invalid")

    monkeypatch.setattr(ses_views, "verify_sns_message", reject_signature)
    rejected = post_feedback(
        unauthenticated_client,
        delivery,
        "Bounce",
        event_id="sns-forged-1",
    )
    assert rejected.status_code == 403
    assert not SDREmailProviderEvent.objects.exists()

    monkeypatch.setattr(ses_views, "verify_sns_message", lambda payload: None)
    mismatch = post_feedback(
        unauthenticated_client,
        delivery,
        "Bounce",
        event_id="sns-mismatch-1",
        recipient="someone-else@example.com",
    )
    assert mismatch.status_code == 200
    assert mismatch.json() == {
        "ok": True,
        "status": "ignored",
        "reason": "recipient_mismatch",
    }
    assert not SDREmailProviderEvent.objects.exists()
