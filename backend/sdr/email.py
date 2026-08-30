"""Inbound email adapter for new SDR leads and nurture replies."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from cases.inbound.parser import ParsedEmail
from cases.models import EmailMessage
from common import notifications
from matching.models import EvidenceKind, EvidenceSource
from matching.provider_import import (
    ProviderPersonRecord,
    preview_provider_person_import,
)
from sdr.models import (
    EmailSuppressionReason,
    EmailSuppressionSource,
    LeadLifecycleEvent,
    LeadLifecycleEventType,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    NurtureReplySentiment,
)
from sdr.nurture import stop_enrollment
from sdr.response import record_lifecycle_event
from sdr.suppression import suppress_email

logger = logging.getLogger(__name__)

INBOUND_EMAIL_JOB = "sdr.process_inbound_email"
EMAIL_IMPORT_SUMMARY = "Inbound email received"
EMAIL_IMPORT_NAMESPACE_PREFIX = "email:inbound:"
MAX_SAFE_COUNT = 1_000_000_000
REPLY_MATCH_STATUSES = (
    NurtureEnrollmentStatus.ACTIVE,
    NurtureEnrollmentStatus.PAUSED,
    NurtureEnrollmentStatus.COMPLETED,
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


def enqueue_inbound_email(
    email_message: EmailMessage,
    *,
    parsed_email: ParsedEmail,
) -> None:
    """Persist a content-free SDR job while the parsed message is still in memory."""

    if (
        parsed_email.message_id != email_message.message_id
        or parsed_email.from_address != email_message.from_address
    ):
        raise ValueError("Parsed inbound email does not match its durable receipt.")
    # Classification happens while the MIME payload is request-local.  Include
    # HTML-only mail here because none of either body representation survives
    # in EmailMessage or the durable automation payload.
    classification_body = "\n".join(
        value
        for value in (parsed_email.body_text, parsed_email.body_html)
        if value
    )
    sentiment, opted_out = classify_reply(classification_body)
    message_key_hmac = _message_key_hmac(email_message)

    enqueued = enqueue_job(
        JobRequest(
            org_id=email_message.org_id,
            name=INBOUND_EMAIL_JOB,
            idempotency_key=f"inbound-email:{message_key_hmac}",
            payload={
                "org_id": str(email_message.org_id),
                "email_message_id": str(email_message.id),
                "message_key_hmac": message_key_hmac,
                "sentiment": sentiment,
                "opted_out": opted_out,
                "safe_counts": {
                    "body_characters": _safe_count(len(parsed_email.body_text or "")),
                    "subject_characters": _safe_count(len(parsed_email.subject or "")),
                    "attachments": _safe_count(len(parsed_email.attachments)),
                    "has_html": bool(parsed_email.body_html),
                },
            },
            max_attempts=5,
        )
    )
    transaction.on_commit(lambda: _safe_dispatch(enqueued.job))


@transaction.atomic
def process_inbound_email_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    email_message = _load_email_message(payload)
    safe_projection = _safe_job_projection(
        payload=payload,
        email_message=email_message,
    )
    if email_message.direction != "inbound" or email_message.drop_reason:
        return {
            "email_message_id": str(email_message.id),
            "status": "skipped",
        }

    prior_reply = _prior_reply_result(email_message)
    if prior_reply is not None:
        return prior_reply

    enrollment = _find_reply_enrollment(
        email_message,
        message_key_hmac=safe_projection["message_key_hmac"],
    )
    if enrollment:
        return _record_reply(
            email_message=email_message,
            enrollment=enrollment,
            sentiment=safe_projection["sentiment"],
            opted_out=safe_projection["opted_out"],
            message_key_hmac=safe_projection["message_key_hmac"],
        )

    if safe_projection["opted_out"]:
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

    preview = _preview_email_person_import(
        email_message=email_message,
        message_key_hmac=safe_projection["message_key_hmac"],
    )
    return {
        "email_message_id": str(email_message.id),
        "status": "person_import_previewed",
        "batch_id": str(preview.batch.id),
        "replayed": preview.replayed,
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
    sentiment: str,
    opted_out: bool,
    message_key_hmac: str,
) -> Mapping[str, Any]:
    prior_delivery = enrollment.deliveries.filter(
        reply_message_id=message_key_hmac
    ).first()
    if prior_delivery is not None:
        return {
            "email_message_id": str(email_message.id),
            "status": "reply_already_recorded",
            "enrollment_id": str(enrollment.id),
        }

    latest = (
        enrollment.deliveries.filter(status=NurtureDeliveryStatus.SENT)
        .order_by("-sent_at", "-created_at")
        .first()
    )
    now = timezone.now()
    if latest:
        latest.replied_at = now
        latest.reply_message_id = message_key_hmac
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
        event_key=_reply_event_key(email_message),
        data={
            "email_message_id": str(email_message.id),
            "enrollment_id": str(enrollment.id),
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
    *,
    message_key_hmac: str,
) -> LeadNurtureEnrollment | None:
    email = email_message.from_address.strip().lower()
    if not email:
        return None
    base = (
        LeadNurtureEnrollment.objects.filter(
            org_id=email_message.org_id,
            lead__email__iexact=email,
        )
        .select_related(
            "sequence",
            "intake__crm_lead",
            "intake__assigned_profile",
            "lead",
        )
        .prefetch_related("deliveries")
    )
    replay = (
        base.filter(deliveries__reply_message_id=message_key_hmac)
        .order_by("-enrolled_at")
        .first()
    )
    if replay is not None:
        return replay
    return (
        base.filter(status__in=REPLY_MATCH_STATUSES)
        .order_by("-enrolled_at")
        .first()
    )


def _reply_event_key(email_message: EmailMessage) -> str:
    return f"nurture:email-reply:{email_message.id}"


def _prior_reply_result(email_message: EmailMessage) -> Mapping[str, Any] | None:
    event = (
        LeadLifecycleEvent.objects.filter(
            org_id=email_message.org_id,
            event_type=LeadLifecycleEventType.NURTURE_STOPPED,
            event_key=_reply_event_key(email_message),
        )
        .select_related("intake")
        .first()
    )
    if event is None:
        return None
    enrollment_id = str((event.data or {}).get("enrollment_id") or "")
    if not enrollment_id:
        enrollment_id = str(
            LeadNurtureEnrollment.objects.filter(
                org_id=email_message.org_id,
                intake_id=event.intake_id,
            )
            .values_list("id", flat=True)
            .order_by("-enrolled_at")
            .first()
            or ""
        )
    return {
        "email_message_id": str(email_message.id),
        "status": "reply_already_recorded",
        "enrollment_id": enrollment_id,
    }


def _preview_email_person_import(*, email_message, message_key_hmac):
    display_name = _safe_sender_display_name(
        email_message.from_display_name,
        email=email_message.from_address,
    )
    first_name, last_name = _split_display_name(display_name)
    return preview_provider_person_import(
        org=email_message.org,
        requested_by=None,
        idempotency_key=uuid5(
            NAMESPACE_URL,
            f"sdr:inbound-email-import:v1:{email_message.org_id}:{message_key_hmac}",
        ),
        source=EvidenceSource.EMAIL,
        source_namespace=_source_namespace(email_message),
        records=[
            ProviderPersonRecord(
                source_record_id=message_key_hmac,
                display_name=display_name,
                first_name=first_name,
                last_name=last_name,
                email=email_message.from_address,
                evidence_kind=EvidenceKind.INTERACTION,
                evidence_summary=EMAIL_IMPORT_SUMMARY,
                observed_at=email_message.received_at,
            )
        ],
    )


def _safe_job_projection(*, payload, email_message) -> dict[str, Any]:
    expected_keys = {
        "org_id",
        "email_message_id",
        "message_key_hmac",
        "sentiment",
        "opted_out",
        "safe_counts",
    }
    counts = payload.get("safe_counts")
    if set(payload) != expected_keys or not isinstance(counts, Mapping):
        raise PermanentJobError(
            "The inbound SDR email payload is invalid.",
            code="invalid_inbound_email_payload",
        )
    expected_count_keys = {
        "body_characters",
        "subject_characters",
        "attachments",
        "has_html",
    }
    numeric_counts = (
        counts.get("body_characters"),
        counts.get("subject_characters"),
        counts.get("attachments"),
    )
    if (
        set(counts) != expected_count_keys
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= MAX_SAFE_COUNT
            for value in numeric_counts
        )
        or not isinstance(counts.get("has_html"), bool)
    ):
        raise PermanentJobError(
            "The inbound SDR email payload is invalid.",
            code="invalid_inbound_email_payload",
        )
    sentiment = str(payload.get("sentiment") or "")
    opted_out = payload.get("opted_out")
    message_key_hmac = str(payload.get("message_key_hmac") or "")
    if (
        sentiment
        not in {
            NurtureReplySentiment.POSITIVE,
            NurtureReplySentiment.NEGATIVE,
            NurtureReplySentiment.NEUTRAL,
        }
        or not isinstance(opted_out, bool)
        or not hmac.compare_digest(message_key_hmac, _message_key_hmac(email_message))
    ):
        raise PermanentJobError(
            "The inbound SDR email payload is invalid.",
            code="invalid_inbound_email_payload",
        )
    return {
        "message_key_hmac": message_key_hmac,
        "sentiment": sentiment,
        "opted_out": opted_out,
    }


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
        EmailMessage.objects.select_for_update()
        .filter(id=email_message_id, org_id=org_id)
        .select_related("org")
        .first()
    )
    if email_message is None:
        raise PermanentJobError(
            "The inbound email no longer exists.",
            code="inbound_email_not_found",
        )
    return email_message


def _message_key_hmac(email_message: EmailMessage) -> str:
    if not email_message.mailbox_id or not email_message.message_id:
        raise PermanentJobError(
            "The inbound email does not have a safe source identity.",
            code="inbound_email_source_identity_missing",
        )
    return _tenant_hmac(
        email_message=email_message,
        purpose="message",
        value=email_message.message_id,
    )


def _source_namespace(email_message: EmailMessage) -> str:
    return EMAIL_IMPORT_NAMESPACE_PREFIX + _tenant_hmac(
        email_message=email_message,
        purpose="mailbox",
        value=str(email_message.mailbox_id),
    )[:32]


def _tenant_hmac(*, email_message: EmailMessage, purpose: str, value: str) -> str:
    secret = str(settings.SECRET_KEY).encode("utf-8")
    message = (
        f"sdr-inbound-email:{purpose}:v1:{email_message.org_id}:"
        f"{email_message.mailbox_id}:{value}"
    ).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _safe_count(value: int) -> int:
    return min(MAX_SAFE_COUNT, max(0, int(value)))


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


def _safe_sender_display_name(value: str, *, email: str) -> str:
    """Keep an attacker-controlled display name from bypassing email masking."""

    candidate = re.sub(r"\s+", " ", (value or "").strip())[:255]
    normalized_email = (email or "").strip().casefold()
    if (
        not candidate
        or "@" in candidate
        or (normalized_email and normalized_email in candidate.casefold())
    ):
        return "Email sender"
    return candidate
