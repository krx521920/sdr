"""Signed WhatsApp delivery-status webhook handling."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from django.utils import timezone

from automation.tenant_context import database_org_context
from integrations.models import (
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)

STATUS_ORDER = {
    WhatsAppMessageStatus.SENT: 1,
    WhatsAppMessageStatus.DELIVERED: 2,
    WhatsAppMessageStatus.READ: 3,
}


def verify_whatsapp_signature(*, body: bytes, signature: str, app_secret: str) -> bool:
    if not app_secret or not signature.startswith("sha256="):
        return False
    expected = (
        "sha256="
        + hmac.new(
            app_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(signature, expected)


def process_whatsapp_status_webhook(payload: Mapping[str, Any]) -> dict[str, int]:
    processed = 0
    ignored = 0
    for entry in _mapping_list(payload.get("entry")):
        for change in _mapping_list(entry.get("changes")):
            value = change.get("value", {})
            if not isinstance(value, Mapping):
                ignored += 1
                continue
            metadata = value.get("metadata", {})
            phone_number_id = (
                str(metadata.get("phone_number_id", "")).strip()
                if isinstance(metadata, Mapping)
                else ""
            )
            route = WhatsAppPhoneRoute.objects.filter(
                phone_number_id=phone_number_id
            ).first()
            if route is None:
                ignored += len(_mapping_list(value.get("statuses"))) or 1
                continue
            with database_org_context(route.org_id):
                WhatsAppBusinessConnection.objects.filter(
                    route=route,
                    org_id=route.org_id,
                ).update(last_webhook_at=timezone.now())
                for status_payload in _mapping_list(value.get("statuses")):
                    if _apply_status(route.org_id, status_payload):
                        processed += 1
                    else:
                        ignored += 1
    return {"processed": processed, "ignored": ignored}


def _apply_status(org_id, payload: Mapping[str, Any]) -> bool:
    provider_message_id = str(payload.get("id", "")).strip()
    provider_status = str(payload.get("status", "")).strip().lower()
    if not provider_message_id or provider_status not in {
        "sent",
        "delivered",
        "read",
        "failed",
    }:
        return False
    message = WhatsAppMessage.objects.filter(
        org_id=org_id,
        provider_message_id=provider_message_id,
    ).first()
    if message is None:
        return False

    occurred_at = _timestamp(payload.get("timestamp"))
    snapshot = {
        "status": provider_status,
        "timestamp": occurred_at.isoformat(),
    }
    updates: dict[str, Any] = {"provider_status_snapshot": snapshot}
    if provider_status == "failed":
        if message.status in {
            WhatsAppMessageStatus.DELIVERED,
            WhatsAppMessageStatus.READ,
        }:
            WhatsAppMessage.objects.filter(id=message.id, org_id=org_id).update(
                provider_status_snapshot=snapshot
            )
            return True
        errors = _mapping_list(payload.get("errors"))
        error = errors[0] if errors else {}
        code = str(error.get("code", "")).strip()
        title = str(error.get("title", "")).strip()
        updates.update(
            status=WhatsAppMessageStatus.FAILED,
            failed_at=occurred_at,
            error_code=(f"whatsapp_provider_{code}" if code else "whatsapp_failed"),
            error_message=title[:1000] or "WhatsApp reported delivery failure.",
        )
    else:
        next_status = WhatsAppMessageStatus(provider_status)
        current_rank = STATUS_ORDER.get(message.status, 0)
        next_rank = STATUS_ORDER[next_status]
        if next_rank >= current_rank:
            updates["status"] = next_status
            if next_status == WhatsAppMessageStatus.SENT and not message.sent_at:
                updates["sent_at"] = occurred_at
            elif next_status == WhatsAppMessageStatus.DELIVERED:
                updates["delivered_at"] = occurred_at
            elif next_status == WhatsAppMessageStatus.READ:
                updates["read_at"] = occurred_at
            updates.update(error_code="", error_message="")
    WhatsAppMessage.objects.filter(id=message.id, org_id=org_id).update(**updates)
    return True


def _timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return timezone.now()


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
