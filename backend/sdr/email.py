"""Inbound email adapter for new SDR leads and nurture replies."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from cases.models import EmailMessage
from common import notifications
from sdr.domain import CompanySnapshot, LeadCandidate, LeadIdentity, LeadSource
from sdr.models import (
    EmailSuppressionReason,
    EmailSuppressionSource,
    LeadLifecycleEventType,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    NurtureReplySentiment,
)
from sdr.nurture import stop_enrollment
from sdr.response import record_lifecycle_event
from sdr.services import process_candidate_intake
from sdr.suppression import suppress_email

logger = logging.getLogger(__name__)

INBOUND_EMAIL_JOB = "sdr.process_inbound_email"
REPLY_MATCH_STATUSES = (
    NurtureEnrollmentStatus.ACTIVE,
    NurtureEnrollmentStatus.PAUSED,
    NurtureEnrollmentStatus.COMPLETED,
)
FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "qq.com",
        "163.com",
        "126.com",
    }
)
OPT_OUT_PHRASES = (
    "unsubscribe",
    "remove me",
    "stop emailing",
    "do not contact",
    "退订",
    "不要再发",
)
NEGATIVE_PHRASES = (
    "not interested",
    "no thanks",
    "not a priority",
    "not now",
    "不感兴趣",
    "不需要",
    "暂不考虑",
)
POSITIVE_PHRASES = (
    "interested",
    "book a meeting",
    "schedule a call",
    "schedule a demo",
    "send pricing",
    "send a quote",
    "let's talk",
    "lets talk",
    "感兴趣",
    "预约",
    "演示",
    "报价",
    "聊一下",
)


def enqueue_inbound_email(email_message: EmailMessage) -> None:
    """Persist the SDR job inside the mailbox transaction, dispatch after commit."""

    enqueued = enqueue_job(
        JobRequest(
            org_id=email_message.org_id,
            name=INBOUND_EMAIL_JOB,
            idempotency_key=f"inbound-email:{email_message.id}",
            payload={
                "org_id": str(email_message.org_id),
                "email_message_id": str(email_message.id),
            },
            max_attempts=5,
        )
    )
    transaction.on_commit(lambda: _safe_dispatch(enqueued.job))


def process_inbound_email_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    email_message = _load_email_message(payload)
    if email_message.direction != "inbound" or email_message.drop_reason:
        return {
            "email_message_id": str(email_message.id),
            "status": "skipped",
        }

    enrollment = _find_reply_enrollment(email_message)
    if enrollment:
        return _record_reply(email_message=email_message, enrollment=enrollment)

    _, opted_out = classify_reply(email_message.body_text)
    if opted_out:
        suppression, _ = suppress_email(
            org_id=email_message.org_id,
            email=email_message.from_address,
            reason=EmailSuppressionReason.UNSUBSCRIBED,
            source=EmailSuppressionSource.INBOUND_REPLY,
            details={"email_message_id": str(email_message.id)},
        )
        return {
            "email_message_id": str(email_message.id),
            "status": "email_suppressed",
            "suppression_id": str(suppression.id),
        }

    candidate = _candidate_from_email(email_message)
    result = process_candidate_intake(
        candidate=candidate,
        raw_payload={
            "email_message_id": str(email_message.id),
            "provider_message_id": email_message.message_id,
            "mailbox_id": str(email_message.mailbox_id or ""),
            "email": email_message.from_address,
            "first_name": candidate.identity.first_name or "",
            "last_name": candidate.identity.last_name or "",
            "company_name": candidate.company.name or "",
            "website": candidate.company.website or "",
            "subject": email_message.subject,
            "message": email_message.body_text[:20000],
        },
    )
    return {
        "email_message_id": str(email_message.id),
        "status": "lead_processed",
        "intake_id": str(result.intake_id),
        "lead_id": str(result.lead_id) if result.lead_id else None,
        "replayed": result.replayed,
    }


def classify_reply(body: str) -> tuple[str, bool]:
    """Return deterministic sentiment and whether the sender requested opt-out."""

    normalized = re.sub(r"\s+", " ", (body or "").lower())[:10000]
    opted_out = any(phrase in normalized for phrase in OPT_OUT_PHRASES)
    if opted_out or any(phrase in normalized for phrase in NEGATIVE_PHRASES):
        return NurtureReplySentiment.NEGATIVE, opted_out
    if any(phrase in normalized for phrase in POSITIVE_PHRASES):
        return NurtureReplySentiment.POSITIVE, False
    return NurtureReplySentiment.NEUTRAL, False


def _record_reply(
    *,
    email_message: EmailMessage,
    enrollment: LeadNurtureEnrollment,
) -> Mapping[str, Any]:
    latest = enrollment.deliveries.filter(
        status=NurtureDeliveryStatus.SENT
    ).order_by("-sent_at", "-created_at").first()
    if latest and latest.reply_message_id == email_message.message_id:
        return {
            "email_message_id": str(email_message.id),
            "status": "reply_already_recorded",
            "enrollment_id": str(enrollment.id),
        }

    sentiment, opted_out = classify_reply(email_message.body_text)
    now = timezone.now()
    if latest:
        latest.replied_at = now
        latest.reply_message_id = email_message.message_id
        latest.reply_sentiment = sentiment
        latest.save(
            update_fields=[
                "replied_at",
                "reply_message_id",
                "reply_sentiment",
                "updated_at",
            ]
        )

    target_status = (
        NurtureEnrollmentStatus.CANCELLED
        if opted_out
        else NurtureEnrollmentStatus.REPLIED
    )
    reason = (
        "The contact requested no further email."
        if opted_out
        else "An inbound email reply stopped the nurture sequence."
    )
    stop_enrollment(
        enrollment,
        status=target_status,
        reason=reason,
        reply_sentiment=sentiment,
    )
    if opted_out:
        suppress_email(
            org_id=email_message.org_id,
            email=email_message.from_address,
            reason=EmailSuppressionReason.UNSUBSCRIBED,
            source=EmailSuppressionSource.INBOUND_REPLY,
            source_delivery=latest,
            details={"email_message_id": str(email_message.id)},
        )
    record_lifecycle_event(
        intake=enrollment.intake,
        event_type=LeadLifecycleEventType.NURTURE_STOPPED,
        event_key=f"nurture:email-reply:{email_message.id}",
        data={
            "email_message_id": str(email_message.id),
            "sentiment": sentiment,
            "opted_out": opted_out,
        },
    )
    lead = enrollment.lead or enrollment.intake.crm_lead
    if lead:
        lead.last_contacted = timezone.localdate()
        lead.save(update_fields=["last_contacted", "updated_at"])
    assignee = enrollment.intake.assigned_profile
    if assignee and assignee.is_active:
        notifications.create(
            assignee,
            "sdr_nurture_reply",
            entity=lead,
            entity_name=(
                getattr(lead, "company_name", "")
                or email_message.from_display_name
                or email_message.from_address
            ),
            link=f"/leads/{lead.id}" if lead else "/leads",
            data={
                "intake_id": str(enrollment.intake_id),
                "email_message_id": str(email_message.id),
                "sentiment": sentiment,
                "subject": email_message.subject[:255],
            },
        )
    return {
        "email_message_id": str(email_message.id),
        "status": "reply_recorded",
        "enrollment_id": str(enrollment.id),
        "sentiment": sentiment,
        "opted_out": opted_out,
    }


def _find_reply_enrollment(
    email_message: EmailMessage,
) -> LeadNurtureEnrollment | None:
    email = email_message.from_address.strip().lower()
    if not email:
        return None
    return (
        LeadNurtureEnrollment.objects.filter(
            org_id=email_message.org_id,
            lead__email__iexact=email,
            status__in=REPLY_MATCH_STATUSES,
        )
        .select_related(
            "sequence",
            "intake__crm_lead",
            "intake__assigned_profile",
            "lead",
        )
        .prefetch_related("deliveries")
        .order_by("-enrolled_at")
        .first()
    )


def _candidate_from_email(email_message: EmailMessage) -> LeadCandidate:
    first_name, last_name = _split_display_name(email_message.from_display_name)
    domain = _company_domain(email_message.from_address)
    return LeadCandidate(
        org_id=email_message.org_id,
        source=LeadSource.EMAIL,
        source_record_id=f"email:{email_message.id}",
        identity=LeadIdentity(
            first_name=first_name,
            last_name=last_name,
            email=email_message.from_address,
        ),
        company=CompanySnapshot(
            name=domain.split(".", 1)[0].replace("-", " ").title() if domain else None,
            website=f"https://{domain}" if domain else None,
        ),
        attributes={
            "subject": email_message.subject,
            "message": email_message.body_text[:20000],
            "mailbox_id": str(email_message.mailbox_id or ""),
            "in_reply_to": email_message.in_reply_to,
        },
        received_at=email_message.received_at,
    )


def _load_email_message(payload: Mapping[str, Any]) -> EmailMessage:
    try:
        org_id = UUID(str(payload["org_id"]))
        email_message_id = UUID(str(payload["email_message_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The inbound SDR email payload is invalid.",
            code="invalid_inbound_email_payload",
        ) from exc
    email_message = (
        EmailMessage.objects.filter(id=email_message_id, org_id=org_id)
        .select_related("mailbox")
        .first()
    )
    if email_message is None:
        raise PermanentJobError(
            "The inbound email no longer exists.",
            code="inbound_email_not_found",
        )
    return email_message


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch inbound SDR email job %s", job.id)


def _split_display_name(value: str) -> tuple[str | None, str | None]:
    parts = [part for part in re.split(r"\s+", (value or "").strip()) if part]
    if not parts:
        return None, None
    return parts[0][:255], " ".join(parts[1:])[:255] or None


def _company_domain(email: str) -> str:
    if "@" not in (email or ""):
        return ""
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    return "" if domain in FREE_EMAIL_DOMAINS else domain
