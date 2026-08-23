"""Parse and apply signed AWS SES delivery feedback to nurture deliveries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as datetime_timezone
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from sdr.email_safety import evaluate_campaign_safety
from sdr.models import (
    EmailProviderEventType,
    EmailSuppressionReason,
    EmailSuppressionSource,
    LeadLifecycleEventType,
    LeadNurtureDelivery,
    SDREmailProviderEvent,
)
from sdr.response import record_lifecycle_event
from sdr.suppression import suppress_email


@dataclass(frozen=True)
class SESFeedback:
    org_id: UUID
    delivery_id: UUID | None
    provider_message_id: str
    provider_event_id: str
    event_type: str
    event_at: datetime
    recipients: tuple[str, ...]
    details: dict[str, Any]


def parse_ses_feedback(message: str, *, sns_message_id: str) -> SESFeedback | None:
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    raw_type = str(payload.get("eventType") or payload.get("notificationType") or "")
    event_type = raw_type.strip().lower()
    if event_type not in EmailProviderEventType.values:
        return None
    mail = payload.get("mail")
    if not isinstance(mail, dict):
        return None
    tags = mail.get("tags") if isinstance(mail.get("tags"), dict) else {}
    try:
        org_id = UUID(_first_tag(tags, "sdr_org"))
    except (TypeError, ValueError):
        return None
    try:
        delivery_id = UUID(_first_tag(tags, "sdr_delivery"))
    except (TypeError, ValueError):
        delivery_id = None

    section = payload.get(event_type)
    if not isinstance(section, dict):
        section = payload.get(raw_type)
    if not isinstance(section, dict):
        section = {}
    provider_message_id = str(mail.get("messageId") or "")[:255]
    provider_event_id = str(section.get("feedbackId") or sns_message_id or "")[:255]
    if not provider_event_id:
        return None
    recipients = _recipients(event_type, section)
    details = _details(event_type, section, provider_message_id)
    event_at = _event_at(section.get("timestamp") or mail.get("timestamp"))
    return SESFeedback(
        org_id=org_id,
        delivery_id=delivery_id,
        provider_message_id=provider_message_id,
        provider_event_id=provider_event_id,
        event_type=event_type,
        event_at=event_at,
        recipients=recipients,
        details=details,
    )


@transaction.atomic
def process_ses_feedback(feedback: SESFeedback) -> dict[str, Any]:
    deliveries = LeadNurtureDelivery.objects.select_for_update().select_related(
        "enrollment__intake",
        "enrollment__sequence",
    )
    if feedback.delivery_id:
        delivery = deliveries.filter(
            id=feedback.delivery_id,
            org_id=feedback.org_id,
        ).first()
    elif feedback.provider_message_id:
        delivery = deliveries.filter(
            org_id=feedback.org_id,
            provider_message_id=feedback.provider_message_id,
        ).first()
    else:
        delivery = None
    if delivery is None:
        return {"status": "ignored", "reason": "delivery_not_found"}
    if feedback.recipients and delivery.recipient.lower() not in feedback.recipients:
        return {"status": "ignored", "reason": "recipient_mismatch"}
    if (
        delivery.provider_message_id
        and feedback.provider_message_id
        and delivery.provider_message_id != feedback.provider_message_id
    ):
        return {"status": "ignored", "reason": "message_id_mismatch"}

    provider_event, created = SDREmailProviderEvent.objects.get_or_create(
        org_id=feedback.org_id,
        provider="ses",
        provider_event_id=feedback.provider_event_id,
        defaults={
            "delivery": delivery,
            "event_type": feedback.event_type,
            "event_at": feedback.event_at,
            "details": feedback.details,
        },
    )
    if not created:
        return {
            "status": "duplicate",
            "event_id": str(provider_event.id),
            "delivery_id": str(delivery.id),
        }

    update_fields = ["updated_at"]
    if feedback.provider_message_id and not delivery.provider_message_id:
        delivery.provider_message_id = feedback.provider_message_id
        update_fields.append("provider_message_id")
    if feedback.event_type == EmailProviderEventType.DELIVERY:
        delivery.delivered_at = delivery.delivered_at or feedback.event_at
        update_fields.append("delivered_at")
        lifecycle_type = LeadLifecycleEventType.NURTURE_DELIVERED
    elif feedback.event_type == EmailProviderEventType.BOUNCE:
        delivery.bounced_at = delivery.bounced_at or feedback.event_at
        delivery.bounce_type = str(feedback.details.get("bounce_type") or "")[:32]
        delivery.bounce_subtype = str(feedback.details.get("bounce_subtype") or "")[:64]
        update_fields.extend(["bounced_at", "bounce_type", "bounce_subtype"])
        lifecycle_type = LeadLifecycleEventType.NURTURE_BOUNCED
    else:
        delivery.complained_at = delivery.complained_at or feedback.event_at
        update_fields.append("complained_at")
        lifecycle_type = LeadLifecycleEventType.NURTURE_COMPLAINED
    delivery.save(update_fields=update_fields)

    event_key_hash = hashlib.sha256(feedback.provider_event_id.encode()).hexdigest()[
        :24
    ]
    record_lifecycle_event(
        intake=delivery.enrollment.intake,
        event_type=lifecycle_type,
        event_key=f"nurture:ses:{event_key_hash}",
        data={
            "delivery_id": str(delivery.id),
            "provider_event_id": feedback.provider_event_id,
            "event_type": feedback.event_type,
            **feedback.details,
        },
    )

    suppression_id = None
    if (
        feedback.event_type == EmailProviderEventType.BOUNCE
        and delivery.bounce_type.lower() == "permanent"
    ):
        suppression, _ = suppress_email(
            org_id=feedback.org_id,
            email=delivery.recipient,
            reason=EmailSuppressionReason.HARD_BOUNCE,
            source=EmailSuppressionSource.PROVIDER,
            source_delivery=delivery,
            details={"provider_event_id": feedback.provider_event_id},
        )
        suppression_id = str(suppression.id)
    elif feedback.event_type == EmailProviderEventType.COMPLAINT:
        suppression, _ = suppress_email(
            org_id=feedback.org_id,
            email=delivery.recipient,
            reason=EmailSuppressionReason.COMPLAINT,
            source=EmailSuppressionSource.PROVIDER,
            source_delivery=delivery,
            details={"provider_event_id": feedback.provider_event_id},
        )
        suppression_id = str(suppression.id)

    campaign_safety = evaluate_campaign_safety(delivery)
    return {
        "status": "processed",
        "event_id": str(provider_event.id),
        "delivery_id": str(delivery.id),
        "event_type": feedback.event_type,
        "suppression_id": suppression_id,
        "campaign_safety": campaign_safety,
    }


def _first_tag(tags: dict[str, Any], name: str) -> str:
    value = tags.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "")


def _recipients(event_type: str, section: dict[str, Any]) -> tuple[str, ...]:
    if event_type == EmailProviderEventType.BOUNCE:
        values = section.get("bouncedRecipients") or []
    elif event_type == EmailProviderEventType.COMPLAINT:
        values = section.get("complainedRecipients") or []
    else:
        values = section.get("recipients") or []
    recipients: list[str] = []
    for item in values if isinstance(values, list) else []:
        value = item.get("emailAddress") if isinstance(item, dict) else item
        email = str(value or "").strip().lower()
        if email:
            recipients.append(email)
    return tuple(recipients)


def _details(
    event_type: str,
    section: dict[str, Any],
    provider_message_id: str,
) -> dict[str, Any]:
    details: dict[str, Any] = {"provider_message_id": provider_message_id}
    if event_type == EmailProviderEventType.BOUNCE:
        details.update(
            {
                "bounce_type": str(section.get("bounceType") or "")[:32],
                "bounce_subtype": str(section.get("bounceSubType") or "")[:64],
            }
        )
        recipients = section.get("bouncedRecipients") or []
        first = recipients[0] if isinstance(recipients, list) and recipients else {}
        if isinstance(first, dict):
            details.update(
                {
                    "action": str(first.get("action") or "")[:32],
                    "status": str(first.get("status") or "")[:32],
                    "diagnostic_code": str(first.get("diagnosticCode") or "")[:500],
                }
            )
    elif event_type == EmailProviderEventType.COMPLAINT:
        details["complaint_feedback_type"] = str(
            section.get("complaintFeedbackType") or ""
        )[:64]
    elif event_type == EmailProviderEventType.DELIVERY:
        details["processing_time_millis"] = section.get("processingTimeMillis")
    return details


def _event_at(value: Any) -> datetime:
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return timezone.now()
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed
