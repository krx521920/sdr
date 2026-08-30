"""Signed WhatsApp delivery-status webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from automation.tenant_context import database_org_context
from integrations.execution_safety import (
    mark_execution_delivered,
    mark_provider_accepted,
)
from integrations.models import (
    ExternalExecutionRequest,
    ExternalRequestStatus,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)

logger = logging.getLogger(__name__)

STATUS_ORDER = {
    WhatsAppMessageStatus.SENT: 1,
    # A later delivery receipt is stronger evidence than a provider failure.
    # This makes FAILED versus DELIVERED/READ deterministic regardless of
    # webhook arrival order while never allowing SENT to revive a failure.
    WhatsAppMessageStatus.FAILED: 2,
    WhatsAppMessageStatus.DELIVERED: 3,
    WhatsAppMessageStatus.READ: 4,
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
    candidate = WhatsAppMessage.objects.select_related("org").filter(
        org_id=org_id,
        provider_message_id=provider_message_id,
    ).first()
    if candidate is None:
        return False

    occurred_at = _timestamp(payload.get("timestamp"))
    snapshot = {
        "status": provider_status,
        "timestamp": occurred_at.isoformat(),
    }
    with transaction.atomic():
        # Every path which touches both ledgers locks the execution request
        # before the message. The inner savepoint preserves ACK-safe local
        # projection if an execution-ledger write fails.
        try:
            with transaction.atomic():
                _converge_execution_ledger(
                    org=candidate.org,
                    request_id=candidate.execution_request_id,
                    provider_message_id=provider_message_id,
                    provider_status=provider_status,
                )
        except Exception:
            logger.exception(
                "Could not converge WhatsApp execution ledger for message %s",
                candidate.id,
            )

        message = (
            WhatsAppMessage.objects.select_for_update()
            .filter(
                id=candidate.id,
                org_id=org_id,
                provider_message_id=provider_message_id,
            )
            .first()
        )
        if message is None:
            return False
        _apply_monotonic_message_status(
            message=message,
            payload=payload,
            provider_status=provider_status,
            occurred_at=occurred_at,
            snapshot=snapshot,
        )
    return True


def _apply_monotonic_message_status(
    *,
    message: WhatsAppMessage,
    payload: Mapping[str, Any],
    provider_status: str,
    occurred_at: datetime,
    snapshot: Mapping[str, Any],
) -> None:
    """Apply only a strictly stronger provider state to a locked message."""

    if message.status == WhatsAppMessageStatus.SKIPPED:
        return
    current_rank = STATUS_ORDER.get(message.status, 0)
    next_status = WhatsAppMessageStatus(provider_status)
    next_rank = STATUS_ORDER[next_status]
    if next_rank <= current_rank:
        return

    message.status = next_status
    message.provider_status_snapshot = dict(snapshot)
    fields = ["status", "provider_status_snapshot", "updated_at"]
    if next_status == WhatsAppMessageStatus.FAILED:
        errors = _mapping_list(payload.get("errors"))
        error = errors[0] if errors else {}
        code = str(error.get("code", "")).strip()
        if not code.isdigit() or len(code) > 16:
            code = ""
        message.failed_at = occurred_at
        message.error_code = (
            f"whatsapp_provider_{code}" if code else "whatsapp_failed"
        )
        message.error_message = "WhatsApp reported a delivery failure."
        fields.extend(["failed_at", "error_code", "error_message"])
    else:
        message.failed_at = None
        message.error_code = ""
        message.error_message = ""
        fields.extend(["failed_at", "error_code", "error_message"])
        if next_status == WhatsAppMessageStatus.SENT and not message.sent_at:
            message.sent_at = occurred_at
            fields.append("sent_at")
        elif next_status == WhatsAppMessageStatus.DELIVERED:
            message.delivered_at = occurred_at
            fields.append("delivered_at")
        elif next_status == WhatsAppMessageStatus.READ:
            message.read_at = occurred_at
            fields.append("read_at")
    message.save(update_fields=fields)


def _converge_execution_ledger(
    *,
    org,
    request_id,
    provider_message_id: str,
    provider_status: str,
) -> None:
    """Lock/converge execution state before the caller locks its message."""

    if not request_id:
        return
    request = ExternalExecutionRequest.objects.filter(
        org=org,
        id=request_id,
    ).first()
    if request is None:
        return
    if request.status == ExternalRequestStatus.SENDING:
        request = mark_provider_accepted(
            org=org,
            request_id=request.id,
            provider_reference=provider_message_id,
        )
    else:
        request = ExternalExecutionRequest.objects.select_for_update().get(
            org=org,
            id=request.id,
        )
    if provider_status in {"delivered", "read"} and request.status in {
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.UNKNOWN,
        ExternalRequestStatus.DELIVERED,
    }:
        mark_execution_delivered(org=org, request_id=request.id)
    # A provider-side delivery failure occurs after API acceptance. Its units
    # remain consumed; the message retains only sanitized failure evidence.


def _timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return timezone.now()


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
