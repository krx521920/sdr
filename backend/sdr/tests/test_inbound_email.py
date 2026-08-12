from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from cases.inbound.parser import parse_raw_email
from cases.inbound.pipeline import ingest
from cases.models import Case, InboundMailbox
from cases.serializer import InboundMailboxSerializer
from common.models import Notification
from leads.models import Lead
from sdr.email import classify_reply
from sdr.models import (
    LeadIntake,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    SDREmailSuppression,
    SDRNurtureSequence,
    SDRNurtureStep,
)


def raw_email(
    *,
    message_id="inbound-1@example.com",
    sender="Ada Lovelace <ada@analytical.example>",
    subject="Interested in a demo",
    body="We are interested. Can we schedule a demo?",
):
    return (
        f"From: {sender}\n"
        "To: sales@example.com\n"
        f"Subject: {subject}\n"
        f"Message-ID: <{message_id}>\n"
        "Date: Wed, 29 Jul 2026 10:00:00 +0800\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        f"{body}\n"
    )


def run_job(job, org):
    return run_automation_job.apply(args=[str(job.id), str(org.id)]).get()


def sdr_mailbox(org):
    return InboundMailbox.objects.create(
        org=org,
        address="sales@example.com",
        provider="ses",
        route_target="sdr",
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_mailbox_api_accepts_sdr_route_target(admin_client):
    response = admin_client.post(
        "/api/cases/mailboxes/",
        {
            "address": "growth@example.com",
            "provider": "ses",
            "route_target": "sdr",
            "default_priority": "Normal",
            "default_case_type": None,
            "default_assignee_id": None,
            "is_active": True,
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    assert response.json()["route_target"] == "sdr"
    assert InboundMailbox.objects.get(address="growth@example.com").route_target == "sdr"


@pytest.mark.django_db
def test_sdr_mailbox_persists_message_and_durable_job(org_a):
    mailbox = sdr_mailbox(org_a)
    parsed = parse_raw_email(raw_email())

    result = ingest(parsed, mailbox)

    assert result.dropped is False
    assert result.case is None
    assert result.email_message.mailbox == mailbox
    assert result.email_message.from_display_name == "Ada Lovelace"
    assert InboundMailboxSerializer(mailbox).data["route_target"] == "sdr"
    assert Case.objects.count() == 0
    job = AutomationJob.objects.get(name="sdr.process_inbound_email")
    assert job.idempotency_key == f"inbound-email:{result.email_message.id}"

    replay = ingest(parse_raw_email(raw_email()), mailbox)
    assert replay.email_message == result.email_message
    assert AutomationJob.objects.filter(name="sdr.process_inbound_email").count() == 1


@pytest.mark.django_db
def test_new_inbound_email_enters_shared_sdr_pipeline(org_a):
    mailbox = sdr_mailbox(org_a)
    result = ingest(parse_raw_email(raw_email(message_id="new-lead@example.com")), mailbox)
    job = AutomationJob.objects.get(name="sdr.process_inbound_email")

    outcome = run_job(job, org_a)

    assert outcome["status"] == "succeeded"
    intake = LeadIntake.objects.get(
        org=org_a,
        source="email",
        source_record_id=f"email:{result.email_message.id}",
    )
    assert intake.status == "completed"
    assert intake.crm_lead.email == "ada@analytical.example"
    assert intake.crm_lead.first_name == "Ada"
    assert intake.crm_lead.last_name == "Lovelace"
    assert intake.crm_lead.company_name == "Analytical"
    assert Case.objects.count() == 0


@pytest.mark.django_db
def test_inbound_email_records_positive_nurture_reply(
    org_a,
    admin_profile,
):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    lead = Lead.objects.create(
        org=org_a,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@analytical.example",
        company_name="Analytical Engines",
        status="in process",
    )
    intake = LeadIntake.objects.create(
        org=org_a,
        source="website_form",
        source_record_id="email-reply-intake",
        status="completed",
        crm_lead=lead,
        assigned_profile=admin_profile,
        qualification_score=75,
        qualification_band="high",
        processed_at=timezone.now(),
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org_a,
        name="Reply capture",
        is_active=True,
    )
    step = SDRNurtureStep.objects.create(
        org=org_a,
        sequence=sequence,
        position=1,
        subject_a="Checking in",
        body_a="Hello",
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org_a,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    delivery = LeadNurtureDelivery.objects.create(
        org=org_a,
        enrollment=enrollment,
        step=step,
        step_position=1,
        recipient=lead.email,
        subject_template=step.subject_a,
        body_template=step.body_a,
        status=NurtureDeliveryStatus.SENT,
        scheduled_for=timezone.now() - timedelta(days=1),
        sent_at=timezone.now() - timedelta(days=1),
    )
    mailbox = sdr_mailbox(org_a)
    inbound = ingest(
        parse_raw_email(raw_email(message_id="reply-positive@example.com")),
        mailbox,
    )
    job = AutomationJob.objects.get(name="sdr.process_inbound_email")

    assert run_job(job, org_a)["status"] == "succeeded"
    enrollment.refresh_from_db()
    delivery.refresh_from_db()
    assert enrollment.status == NurtureEnrollmentStatus.REPLIED
    assert delivery.reply_sentiment == "positive"
    assert delivery.reply_message_id == "reply-positive@example.com"
    assert delivery.replied_at is not None
    assert LeadIntake.objects.filter(source="email").count() == 0
    assert Notification.objects.filter(
        recipient=admin_profile,
        verb="sdr_nurture_reply",
        data__email_message_id=str(inbound.email_message.id),
    ).exists()


@pytest.mark.django_db
def test_unsubscribe_reply_cancels_nurture(org_a):
    lead = Lead.objects.create(org=org_a, email="ada@analytical.example")
    intake = LeadIntake.objects.create(
        org=org_a,
        source="website_form",
        source_record_id="email-opt-out-intake",
        status="completed",
        crm_lead=lead,
        processed_at=timezone.now(),
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org_a,
        name="Opt out",
        is_active=True,
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org_a,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    mailbox = sdr_mailbox(org_a)
    ingest(
        parse_raw_email(
            raw_email(
                message_id="reply-stop@example.com",
                subject="Re: checking in",
                body="Please unsubscribe and do not contact me again.",
            )
        ),
        mailbox,
    )

    run_job(AutomationJob.objects.get(name="sdr.process_inbound_email"), org_a)
    enrollment.refresh_from_db()
    assert enrollment.status == NurtureEnrollmentStatus.CANCELLED
    assert "no further email" in enrollment.stop_reason
    assert SDREmailSuppression.objects.filter(
        org=org_a,
        email="ada@analytical.example",
        is_active=True,
        source="inbound_reply",
    ).exists()


@pytest.mark.django_db
def test_standalone_unsubscribe_reply_suppresses_without_creating_lead(org_a):
    mailbox = sdr_mailbox(org_a)
    ingest(
        parse_raw_email(
            raw_email(
                message_id="standalone-stop@example.com",
                sender="Pat <pat@prospect.example>",
                subject="Stop",
                body="Please unsubscribe and do not contact me again.",
            )
        ),
        mailbox,
    )

    run_job(AutomationJob.objects.get(name="sdr.process_inbound_email"), org_a)

    assert SDREmailSuppression.objects.filter(
        org=org_a,
        email="pat@prospect.example",
        is_active=True,
    ).exists()
    assert not LeadIntake.objects.filter(source="email").exists()


def test_reply_classifier_handles_positive_negative_and_opt_out():
    assert classify_reply("Can we schedule a demo?") == ("positive", False)
    assert classify_reply("No thanks, not interested.") == ("negative", False)
    assert classify_reply("Please unsubscribe me.") == ("negative", True)
