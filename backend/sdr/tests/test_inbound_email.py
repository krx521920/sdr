import json
import re
from datetime import timedelta
from unittest.mock import patch

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
from matching.import_pipeline import execute_person_import
from matching.models import (
    Evidence,
    EvidenceCollectionMethod,
    EvidenceConfirmationStatus,
    EvidenceLawfulBasis,
    EvidenceSource,
    Person,
    PersonContactIntent,
    PersonImportBatch,
    PersonImportBatchStatus,
    PersonImportRecord,
)
from matching.serializers import PersonImportRecordSerializer
from sdr.email import classify_reply, process_inbound_email_job
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
    assert (
        InboundMailbox.objects.get(address="growth@example.com").route_target == "sdr"
    )


@pytest.mark.django_db
def test_sdr_mailbox_persists_message_and_durable_job(org_a):
    mailbox = sdr_mailbox(org_a)
    raw_message_id = "private-message-id@example.com"
    raw_subject = "private subject must not persist"
    raw_body = "private body must not persist"
    parsed = parse_raw_email(
        raw_email(
            message_id=raw_message_id,
            subject=raw_subject,
            body=raw_body,
        )
    )

    result = ingest(parsed, mailbox)

    assert result.dropped is False
    assert result.case is None
    assert result.email_message.mailbox == mailbox
    assert result.email_message.from_display_name == "Ada Lovelace"
    assert result.email_message.subject == ""
    assert result.email_message.body_text == ""
    assert result.email_message.body_html == ""
    assert result.email_message.to_addresses == ""
    assert result.email_message.cc_addresses == ""
    assert result.email_message.in_reply_to == ""
    assert result.email_message.references == ""
    assert InboundMailboxSerializer(mailbox).data["route_target"] == "sdr"
    assert Case.objects.count() == 0
    job = AutomationJob.objects.get(name="sdr.process_inbound_email")
    assert re.fullmatch(r"inbound-email:[0-9a-f]{64}", job.idempotency_key)
    durable_payload = json.dumps(job.payload, sort_keys=True)
    assert raw_message_id not in durable_payload
    assert raw_subject not in durable_payload
    assert raw_body not in durable_payload
    assert "ada@analytical.example" not in durable_payload
    assert job.payload["sentiment"] == "neutral"
    assert job.payload["opted_out"] is False
    assert job.payload["safe_counts"] == {
        "body_characters": len(raw_body) + 1,
        "subject_characters": len(raw_subject),
        "attachments": 0,
        "has_html": False,
    }

    replay = ingest(
        parse_raw_email(
            raw_email(
                message_id=raw_message_id,
                subject=raw_subject,
                body=raw_body,
            )
        ),
        mailbox,
    )
    assert replay.email_message == result.email_message
    assert AutomationJob.objects.filter(name="sdr.process_inbound_email").count() == 1


@pytest.mark.django_db
def test_sdr_mailbox_minimizes_dropped_message_content_without_enqueuing(org_a):
    mailbox = sdr_mailbox(org_a)
    parsed = parse_raw_email(
        raw_email(
            message_id="dropped-private-message@example.com",
            subject="DROPPED-SUBJECT-SENTINEL",
            body="DROPPED-BODY-SENTINEL",
        )
    )
    parsed.raw_headers["Auto-Submitted"] = "auto-replied"
    parsed.body_html = "<p>DROPPED-HTML-SENTINEL</p>"
    parsed.cc_addresses = ["private-copy@example.com"]
    parsed.in_reply_to = "private-parent@example.com"
    parsed.references = ["private-root@example.com"]

    result = ingest(parsed, mailbox)

    assert result.dropped is True
    assert result.drop_reason == "auto_submitted"
    assert result.email_message.subject == ""
    assert result.email_message.body_text == ""
    assert result.email_message.body_html == ""
    assert result.email_message.to_addresses == ""
    assert result.email_message.cc_addresses == ""
    assert result.email_message.in_reply_to == ""
    assert result.email_message.references == ""
    assert not AutomationJob.objects.filter(name="sdr.process_inbound_email").exists()


@pytest.mark.django_db
def test_unknown_sender_creates_replay_safe_preview_without_raw_content_or_outbound(
    org_a,
):
    mailbox = sdr_mailbox(org_a)
    raw_message_id = "new-lead-private@example.com"
    raw_subject = "private opportunity subject"
    raw_body = "private message body with confidential details"
    result = ingest(
        parse_raw_email(
            raw_email(
                message_id=raw_message_id,
                subject=raw_subject,
                body=raw_body,
            )
        ),
        mailbox,
    )
    job = AutomationJob.objects.get(name="sdr.process_inbound_email")

    with (
        patch("sdr.services.process_candidate_intake") as crm_ai_pipeline,
        patch("sdr.nurture.EmailMultiAlternatives.send") as provider_send,
    ):
        outcome = run_job(job, org_a)

    crm_ai_pipeline.assert_not_called()
    provider_send.assert_not_called()

    assert outcome["status"] == "succeeded"
    job.refresh_from_db()
    assert job.result["status"] == "person_import_previewed"
    assert job.result["replayed"] is False
    batch = PersonImportBatch.objects.get(id=job.result["batch_id"], org=org_a)
    record = PersonImportRecord.objects.get(batch=batch, org=org_a)
    assert batch.status == PersonImportBatchStatus.PREVIEWED
    assert batch.source == EvidenceSource.EMAIL
    assert re.fullmatch(r"email:inbound:[0-9a-f]{32}", batch.source_namespace)
    assert str(mailbox.id) not in batch.source_namespace
    assert mailbox.address not in batch.source_namespace
    assert re.fullmatch(r"[0-9a-f]{64}", record.source_record_id)
    assert record.source_record_id != raw_message_id
    assert record.display_name == "Ada Lovelace"
    assert record.normalized_payload["person"] == {
        "display_name": "Ada Lovelace",
        "first_name": "Ada",
        "last_name": "Lovelace",
    }
    assert record.normalized_payload["identities"] == [
        {
            "kind": "email",
            "normalized_value": "ada@analytical.example",
            "display_value": "ada@analytical.example",
            "is_primary": False,
        }
    ]
    assert record.normalized_payload["evidence"][0]["summary"] == (
        "Inbound email received"
    )
    persisted_preview = json.dumps(
        {
            "batch": {
                "filename": batch.original_filename,
                "mapping": batch.mapping,
                "headers": batch.headers,
                "namespace": batch.source_namespace,
            },
            "record": {
                "normalized_payload": record.normalized_payload,
                "masked_identities": record.masked_identities,
                "field_errors": record.field_errors,
                "source_record_id": record.source_record_id,
            },
        },
        default=str,
        sort_keys=True,
    )
    assert raw_message_id not in persisted_preview
    assert raw_subject not in persisted_preview
    assert raw_body not in persisted_preview
    assert not Person.objects.filter(org=org_a).exists()
    assert not Evidence.objects.filter(org=org_a).exists()

    replay = process_inbound_email_job(job.payload)
    assert replay == {
        "email_message_id": str(result.email_message.id),
        "status": "person_import_previewed",
        "batch_id": str(batch.id),
        "replayed": True,
    }
    assert PersonImportBatch.objects.filter(org=org_a).count() == 1

    execute_person_import(
        org_id=org_a.id,
        batch_id=batch.id,
        request_hash=batch.request_hash,
    )
    batch.refresh_from_db()
    record.refresh_from_db()
    person = Person.objects.get(org=org_a)
    evidence = Evidence.objects.get(org=org_a, person=person)
    provenance = evidence.provenance
    assert batch.status == PersonImportBatchStatus.COMPLETED
    assert record.normalized_payload == {}
    assert evidence.source == EvidenceSource.EMAIL
    assert evidence.kind == "interaction"
    assert evidence.summary == "Inbound email received"
    assert evidence.source_record_id == record.source_record_id
    assert provenance.collection_method == EvidenceCollectionMethod.INBOUND_EMAIL
    assert provenance.lawful_basis == EvidenceLawfulBasis.UNASSESSED
    assert provenance.confirmation_status == EvidenceConfirmationStatus.PENDING
    persisted_entities = json.dumps(
        {
            "person": {
                "display_name": person.display_name,
                "first_name": person.first_name,
                "last_name": person.last_name,
            },
            "evidence": {
                "summary": evidence.summary,
                "source_record_id": evidence.source_record_id,
            },
        },
        default=str,
        sort_keys=True,
    )
    assert raw_message_id not in persisted_entities
    assert raw_subject not in persisted_entities
    assert raw_body not in persisted_entities
    assert not LeadIntake.objects.filter(org=org_a, source="email").exists()
    assert not Lead.objects.filter(org=org_a).exists()
    assert not PersonContactIntent.objects.filter(org=org_a).exists()
    assert not AutomationJob.objects.filter(
        name__in=("sdr.send_acknowledgement", "sdr.send_nurture_email")
    ).exists()
    assert Case.objects.count() == 0


@pytest.mark.django_db
def test_email_address_in_sender_display_name_cannot_bypass_preview_masking(org_a):
    mailbox = sdr_mailbox(org_a)
    ingest(
        parse_raw_email(
            raw_email(
                message_id="display-name-address@example.com",
                sender="private-person@example.net <private-person@example.net>",
            )
        ),
        mailbox,
    )

    run_job(AutomationJob.objects.get(name="sdr.process_inbound_email"), org_a)

    record = PersonImportRecord.objects.select_related("batch").get(org=org_a)
    projection = PersonImportRecordSerializer(record).data
    assert projection["display_name"] == "Email sender"
    assert "private-person@example.net" not in json.dumps(projection)
    assert projection["masked_identities"] == [{"kind": "email", "present": True}]


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
    assert re.fullmatch(r"[0-9a-f]{64}", delivery.reply_message_id)
    assert delivery.reply_message_id != "reply-positive@example.com"
    assert delivery.replied_at is not None
    assert LeadIntake.objects.filter(source="email").count() == 0
    assert Notification.objects.filter(
        recipient=admin_profile,
        verb="sdr_nurture_reply",
        data__email_message_id=str(inbound.email_message.id),
    ).exists()

    delivery.delete()
    replay = process_inbound_email_job(job.payload)
    assert replay == {
        "email_message_id": str(inbound.email_message.id),
        "status": "reply_already_recorded",
        "enrollment_id": str(enrollment.id),
    }
    assert not PersonImportBatch.objects.filter(org=org_a).exists()


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


@pytest.mark.django_db
def test_html_only_opt_out_is_classified_in_memory_and_never_persisted(org_a):
    mailbox = sdr_mailbox(org_a)
    parsed = parse_raw_email(
        raw_email(
            message_id="html-only-stop@example.com",
            sender="HTML Sender <html-sender@prospect.example>",
            subject="Request",
            body="",
        )
    )
    parsed.body_text = ""
    parsed.body_html = "<p>HTML-RAW-SENTINEL Please unsubscribe me.</p>"

    result = ingest(parsed, mailbox)
    job = AutomationJob.objects.get(name="sdr.process_inbound_email")

    assert job.payload["opted_out"] is True
    assert "HTML-RAW-SENTINEL" not in json.dumps(job.payload)
    assert result.email_message.body_text == ""
    assert result.email_message.body_html == ""
    run_job(job, org_a)
    assert SDREmailSuppression.objects.filter(
        org=org_a,
        email="html-sender@prospect.example",
        is_active=True,
    ).exists()
    assert not PersonImportBatch.objects.filter(org=org_a).exists()


def test_reply_classifier_handles_positive_negative_and_opt_out():
    assert classify_reply("Can we schedule a demo?") == ("positive", False)
    assert classify_reply("No thanks, not interested.") == ("negative", False)
    assert classify_reply("Please unsubscribe me.") == ("negative", True)
