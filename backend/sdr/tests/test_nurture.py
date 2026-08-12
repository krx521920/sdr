import re
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from sdr.models import (
    LeadIntake,
    LeadLifecycleEventType,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    LeadNurtureInteraction,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    NurtureInteractionType,
    SDREmailSuppression,
    SDRNurtureSequence,
)
from sdr.nurture import ensure_enrollment_schedule, process_nurture_email_job
from sdr.tracking import make_tracking_token, tracking_url, validate_destination


def run_job(job, org):
    return run_automation_job.apply(args=[str(job.id), str(org.id)]).get()


def sequence_payload(**overrides):
    payload = {
        "name": "Warm inbound follow-up",
        "description": "Follow up with medium-fit inbound leads.",
        "priority": 10,
        "is_active": True,
        "auto_enroll": True,
        "sources": ["website_form"],
        "qualification_bands": [],
        "from_email": "sales@example.com",
        "steps": [
            {
                "position": 1,
                "delay_minutes": 0,
                "subject_a": "A follow-up for {{ first_name }}",
                "body_a": "Hi {{ first_name }}, can we help {{ company_name }}?",
                "subject_b": "B follow-up for {{ first_name }}",
                "body_b": "Hi {{ first_name }}, a different message for {{ company_name }}.",
                "variant_b_percent": 100,
            },
            {
                "position": 2,
                "delay_minutes": 1440,
                "subject_a": "One more idea",
                "body_a": "Hi {{ first_name }}, should we keep this open?",
                "subject_b": "",
                "body_b": "",
                "variant_b_percent": 0,
            },
        ],
    }
    payload.update(overrides)
    return payload


def create_sequence(client, **overrides):
    response = client.post(
        "/api/sdr/nurture/sequences/",
        sequence_payload(**overrides),
        format="json",
    )
    assert response.status_code == 201, response.json()
    return SDRNurtureSequence.objects.get(id=response.json()["id"])


def accept_and_process(client, org, source_record_id):
    response = client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": source_record_id,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "company_name": "Analytical Engines Ltd",
            "message": "We are evaluating an automated qualification workflow.",
        },
        format="json",
    )
    assert response.status_code == 202
    intake_job = AutomationJob.objects.get(id=response.json()["job_id"])
    assert run_job(intake_job, org)["status"] == "succeeded"
    return LeadNurtureEnrollment.objects.get(intake_id=response.json()["intake_id"])


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_sequence_api_validates_steps_and_tenant_scope(
    admin_client,
    org_b_client,
):
    invalid = admin_client.post(
        "/api/sdr/nurture/sequences/",
        sequence_payload(
            steps=[
                {
                    "position": 2,
                    "delay_minutes": 0,
                    "subject_a": "Hello",
                    "body_a": "Hi",
                    "subject_b": "",
                    "body_b": "",
                    "variant_b_percent": 0,
                }
            ]
        ),
        format="json",
    )
    assert invalid.status_code == 400

    sequence = create_sequence(admin_client)
    detail = org_b_client.get(f"/api/sdr/nurture/sequences/{sequence.id}/")
    assert detail.status_code == 404


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_completed_intake_auto_enrolls_and_advances_ab_sequence(
    admin_client,
    org_a,
):
    sequence = create_sequence(admin_client)
    enrollment = accept_and_process(admin_client, org_a, "nurture-auto-1")

    assert enrollment.sequence == sequence
    ensure_enrollment_schedule(enrollment)
    assert LeadNurtureDelivery.objects.filter(enrollment=enrollment).count() == 1
    first = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=1)
    assert first.variant == "B"
    first_job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(first.id),
    )
    assert run_job(first_job, org_a)["status"] == "succeeded"

    first.refresh_from_db()
    enrollment.refresh_from_db()
    assert first.status == NurtureDeliveryStatus.SENT
    assert enrollment.current_step_position == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "B follow-up for Ada"
    assert mail.outbox[0].extra_headers["X-SES-MESSAGE-TAGS"] == (
        f"sdr_org={org_a.id},sdr_delivery={first.id}"
    )

    second = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=2)
    assert second.status == NurtureDeliveryStatus.PENDING
    assert second.scheduled_for >= first.sent_at + timedelta(hours=23, minutes=59)

    metrics = admin_client.get("/api/sdr/nurture/sequences/")
    assert metrics.status_code == 200
    assert metrics.json()["summary"]["sent"] == 1
    assert metrics.json()["results"][0]["metrics"]["variants"]["B"]["sent"] == 1


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_paused_delivery_resumes_without_losing_the_step(admin_client, org_a):
    create_sequence(admin_client)
    enrollment = accept_and_process(admin_client, org_a, "nurture-pause-1")
    first = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=1)

    paused = admin_client.post(
        f"/api/sdr/nurture/enrollments/{enrollment.id}/action/",
        {"action": "pause"},
        format="json",
    )
    assert paused.status_code == 200
    first_job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(first.id),
    )
    assert run_job(first_job, org_a)["status"] == "succeeded"
    first.refresh_from_db()
    assert first.status == NurtureDeliveryStatus.PENDING
    assert len(mail.outbox) == 0

    resumed = admin_client.post(
        f"/api/sdr/nurture/enrollments/{enrollment.id}/action/",
        {"action": "resume"},
        format="json",
    )
    assert resumed.status_code == 200
    resumed_job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(first.id),
        idempotency_key__endswith="dispatch:1",
    )
    assert run_job(resumed_job, org_a)["status"] == "succeeded"
    first.refresh_from_db()
    assert first.status == NurtureDeliveryStatus.SENT
    assert len(mail.outbox) == 1


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_converted_lead_stops_before_delivery(admin_client, org_a):
    create_sequence(admin_client)
    enrollment = accept_and_process(admin_client, org_a, "nurture-converted-1")
    enrollment.lead.status = "converted"
    enrollment.lead.save(update_fields=["status", "updated_at"])
    delivery = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=1)
    job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(delivery.id),
    )

    assert run_job(job, org_a)["status"] == "succeeded"
    enrollment.refresh_from_db()
    delivery.refresh_from_db()
    assert enrollment.status == NurtureEnrollmentStatus.CONVERTED
    assert delivery.status == NurtureDeliveryStatus.SKIPPED
    assert len(mail.outbox) == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_reply_action_stops_sequence_and_records_sentiment(admin_client, org_a):
    create_sequence(admin_client)
    enrollment = accept_and_process(admin_client, org_a, "nurture-reply-1")
    first = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=1)
    job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(first.id),
    )
    run_job(job, org_a)

    response = admin_client.post(
        f"/api/sdr/nurture/enrollments/{enrollment.id}/action/",
        {"action": "mark_replied", "reply_sentiment": "positive"},
        format="json",
    )
    assert response.status_code == 200
    first.refresh_from_db()
    enrollment.refresh_from_db()
    assert enrollment.status == NurtureEnrollmentStatus.REPLIED
    assert first.replied_at <= timezone.now()
    assert first.reply_sentiment == "positive"

    summary = admin_client.get("/api/sdr/nurture/sequences/").json()["summary"]
    assert summary["replied"] == 1
    assert summary["positive_replies"] == 1
    assert summary["reply_rate"] == 100.0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SDR_NURTURE_TRACKING_BASE_URL="https://crm.example.test",
)
def test_tracked_email_records_deduplicated_open_and_click_metrics(
    admin_client,
    org_a,
):
    payload = sequence_payload()
    payload["steps"][0]["body_b"] = (
        "Hi {{ first_name }}, read https://example.com/demo."
    )
    create_sequence(admin_client, steps=payload["steps"])
    enrollment = accept_and_process(admin_client, org_a, "nurture-track-1")
    delivery = LeadNurtureDelivery.objects.get(
        enrollment=enrollment,
        step_position=1,
    )
    job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(delivery.id),
    )
    assert run_job(job, org_a)["status"] == "succeeded"

    message = mail.outbox[0]
    click_match = re.search(
        r"https://crm\.example\.test/api/sdr/public/nurture/click/([^/\s]+)/",
        message.body,
    )
    assert click_match is not None
    alternative = message.alternatives[0]
    html_body = (
        alternative.content if hasattr(alternative, "content") else alternative[0]
    )
    open_match = re.search(
        r"https://crm\.example\.test/api/sdr/public/nurture/open/([^/\s]+)/pixel\.gif",
        html_body,
    )
    assert open_match is not None
    assert "https://example.com/demo" in html_body

    open_path = urlsplit(open_match.group(0)).path
    click_path = urlsplit(click_match.group(0)).path
    headers = {"HTTP_USER_AGENT": "Example Mail Client/1.0"}
    assert admin_client.get(open_path, **headers).status_code == 200
    assert admin_client.get(open_path, **headers).status_code == 200
    clicked = admin_client.get(click_path, **headers)
    assert clicked.status_code == 302
    assert clicked["Location"] == "https://example.com/demo"
    assert admin_client.get(click_path, **headers).status_code == 302

    delivery.refresh_from_db()
    assert delivery.opened_at is not None
    assert delivery.clicked_at is not None
    assert delivery.open_count == 1
    assert delivery.click_count == 1
    assert delivery.last_clicked_url == "https://example.com/demo"
    assert LeadNurtureInteraction.objects.filter(delivery=delivery).count() == 2

    metrics = admin_client.get("/api/sdr/nurture/sequences/").json()
    assert metrics["summary"]["open_rate"] == 100.0
    assert metrics["summary"]["click_rate"] == 100.0
    variant = metrics["results"][0]["metrics"]["variants"]["B"]
    assert variant["open_rate"] == 100.0
    assert variant["click_rate"] == 100.0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SDR_NURTURE_TRACKING_BASE_URL="https://crm.example.test",
)
def test_tracking_tokens_reject_tampering_unsafe_redirects_and_prefetch(
    admin_client,
    org_a,
):
    create_sequence(admin_client)
    enrollment = accept_and_process(admin_client, org_a, "nurture-track-security-1")
    delivery = LeadNurtureDelivery.objects.get(
        enrollment=enrollment,
        step_position=1,
    )
    job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(delivery.id),
    )
    run_job(job, org_a)

    open_url = tracking_url(delivery, NurtureInteractionType.OPEN)
    open_path = urlsplit(open_url).path
    assert admin_client.head(open_path).status_code == 200
    assert not LeadNurtureInteraction.objects.filter(delivery=delivery).exists()

    tampered_open_path = open_path.replace("/pixel.gif", "x/pixel.gif")
    assert admin_client.get(tampered_open_path).status_code == 200
    assert not LeadNurtureInteraction.objects.filter(delivery=delivery).exists()

    open_token = make_tracking_token(delivery, NurtureInteractionType.OPEN)
    wrong_event_path = f"/api/sdr/public/nurture/click/{open_token}/"
    assert admin_client.get(wrong_event_path).status_code == 404
    assert admin_client.head(wrong_event_path).status_code == 204

    with pytest.raises(ValueError):
        validate_destination("javascript:alert(1)")
    with pytest.raises(ValueError):
        validate_destination("https://user:secret@example.com/private")


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SDR_NURTURE_TRACKING_BASE_URL="https://crm.example.test",
)
def test_one_click_unsubscribe_is_idempotent_and_stops_future_delivery(
    admin_client,
    org_a,
):
    create_sequence(admin_client)
    enrollment = accept_and_process(admin_client, org_a, "nurture-unsubscribe-1")
    first = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=1)
    first_job = AutomationJob.objects.get(
        name="sdr.send_nurture_email",
        payload__delivery_id=str(first.id),
    )
    run_job(first_job, org_a)

    message = mail.outbox[0]
    unsubscribe_header = message.extra_headers["List-Unsubscribe"]
    unsubscribe_url = unsubscribe_header.removeprefix("<").removesuffix(">")
    unsubscribe_path = urlsplit(unsubscribe_url).path
    assert message.extra_headers["List-Unsubscribe-Post"] == (
        "List-Unsubscribe=One-Click"
    )
    assert unsubscribe_url in message.body

    assert admin_client.get(unsubscribe_path).status_code == 200
    assert not SDREmailSuppression.objects.exists()
    assert admin_client.post(
        unsubscribe_path,
        {"List-Unsubscribe": "One-Click"},
    ).status_code == 200
    assert admin_client.post(
        unsubscribe_path,
        {"List-Unsubscribe": "One-Click"},
    ).status_code == 200

    suppression = SDREmailSuppression.objects.get()
    enrollment.refresh_from_db()
    assert suppression.email == "ada@example.com"
    assert suppression.reason == "unsubscribed"
    assert suppression.source == "one_click"
    assert suppression.source_delivery == first
    assert enrollment.status == NurtureEnrollmentStatus.CANCELLED
    assert SDREmailSuppression.objects.count() == 1

    second = LeadNurtureDelivery.objects.get(enrollment=enrollment, step_position=2)
    process_nurture_email_job(
        {"org_id": str(org_a.id), "delivery_id": str(second.id)}
    )
    second.refresh_from_db()
    assert second.status == NurtureDeliveryStatus.SKIPPED
    assert len(mail.outbox) == 1

    summary = admin_client.get("/api/sdr/nurture/sequences/").json()["summary"]
    assert summary["active_suppressions"] == 1
    tampered_path = f"{unsubscribe_path.rstrip('/')}x/"
    assert admin_client.post(tampered_path).status_code == 404


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_admin_suppression_blocks_auto_enrollment_until_released(
    admin_client,
    org_b_client,
    org_a,
):
    create_sequence(admin_client)
    added = admin_client.post(
        "/api/sdr/nurture/suppressions/",
        {"email": "ADA@EXAMPLE.COM", "reason": "admin"},
        format="json",
    )
    assert added.status_code == 201, added.json()
    suppression_id = added.json()["id"]
    assert added.json()["email"] == "ada@example.com"
    assert org_b_client.delete(
        f"/api/sdr/nurture/suppressions/{suppression_id}/"
    ).status_code == 404

    response = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "nurture-suppressed-intake-1",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "company_name": "Analytical Engines Ltd",
        },
        format="json",
    )
    run_job(AutomationJob.objects.get(id=response.json()["job_id"]), org_a)
    intake = LeadIntake.objects.get(id=response.json()["intake_id"])
    assert not LeadNurtureEnrollment.objects.filter(intake=intake).exists()
    assert intake.lifecycle_events.filter(
        event_type=LeadLifecycleEventType.NURTURE_SUPPRESSED
    ).exists()

    released = admin_client.delete(
        f"/api/sdr/nurture/suppressions/{suppression_id}/"
    )
    assert released.status_code == 200
    assert released.json()["is_active"] is False
    assert (
        admin_client.get("/api/sdr/nurture/suppressions/").json()["count"] == 0
    )

    enrollment = accept_and_process(
        admin_client,
        org_a,
        "nurture-after-release-1",
    )
    assert enrollment.status == NurtureEnrollmentStatus.ACTIVE
