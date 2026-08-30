"""Fail-closed execution binding for one outbound email delivery.

The provider safety ledger lives in ``integrations``.  This module keeps SDR
decoupled from that concrete app through ``provider_ports`` and binds a request
to a delivery with the delivery UUID as the execution idempotency key.  No
provider call is made here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from django.conf import settings

from sdr.provider_ports import (
    ExecutionChannel,
    ExecutionSafetyError,
    ExternalExecutionRequestPort,
    ExternalRequestStatus,
    assert_provider_io_authorized,
    external_execution_request,
    hash_target_identifier,
    mark_execution_delivered,
    mark_execution_sending,
    mark_provider_accepted,
    release_execution,
    reserve_execution,
)

EMAIL_SEND_ACTION = "send_email"


@dataclass(frozen=True, slots=True)
class EmailExecutionIntent:
    target_hash: str
    payload_hash: str
    units: int = 1


def _error(code: str, detail: str, status_code: int = 409) -> ExecutionSafetyError:
    return ExecutionSafetyError(code=code, detail=detail, status_code=status_code)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def email_execution_request_id(payload: Mapping[str, Any]) -> UUID | None:
    """Return the approved request id or fail closed outside explicit test mode."""

    raw = str(payload.get("execution_request_id") or "").strip()
    if not raw:
        if getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
            return None
        raise _error(
            "execution_approval_required",
            "The email job has no approved execution request.",
            403,
        )
    try:
        return UUID(raw)
    except ValueError as exc:
        raise _error(
            "invalid_email_execution_request",
            "The email execution request id is invalid.",
        ) from exc


def email_send_intent(
    *,
    org,
    recipient: str,
    subject: str,
    text_body: str,
    from_email: str,
    html_body: str = "",
    headers: Mapping[str, str] | None = None,
) -> EmailExecutionIntent:
    """Fingerprint the semantic provider payload without persisting its PII."""

    normalized_recipient = str(recipient or "").strip().casefold()
    target_hash = hash_target_identifier(
        org=org,
        channel=ExecutionChannel.EMAIL,
        identifier=normalized_recipient,
    )
    payload_hash = _sha256(
        _canonical(
            {
                "schema_version": 1,
                "from_email": str(from_email or "").strip(),
                "to": [normalized_recipient],
                "subject": str(subject),
                "text_body": str(text_body),
                "html_body": str(html_body),
                "headers": {
                    str(key): str(value)
                    for key, value in sorted((headers or {}).items())
                },
            }
        )
    )
    return EmailExecutionIntent(target_hash=target_hash, payload_hash=payload_hash)


def reserve_email_send(
    *,
    org,
    delivery_id: UUID,
    approval_id: UUID,
    intent: EmailExecutionIntent,
):
    """Consume an exact approval and bind it to one immutable delivery UUID."""

    return reserve_execution(
        org=org,
        channel=ExecutionChannel.EMAIL,
        action=EMAIL_SEND_ACTION,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=intent.units,
        approval_id=approval_id,
        idempotency_key=delivery_id,
    )


def bound_email_execution(
    *,
    org,
    delivery_id: UUID,
    request_id: UUID | None,
) -> ExternalExecutionRequestPort | None:
    """Load and verify the non-PII durable binding before inspecting its state."""

    if request_id is None:
        return None
    request = external_execution_request(org=org, request_id=request_id)
    if request is None:
        raise _error(
            "email_execution_request_not_found",
            "The email execution request was not found.",
            404,
        )
    if (
        request.org_id != org.id
        or request.channel != ExecutionChannel.EMAIL
        or request.action != EMAIL_SEND_ACTION
        or request.idempotency_key != delivery_id
    ):
        # Never release a request that may belong to a different delivery.
        raise _error(
            "email_execution_scope_mismatch",
            "The email execution request does not belong to this delivery.",
        )
    return request


def release_reserved_email_execution(
    *,
    org,
    request: ExternalExecutionRequestPort | None,
    error_code: str,
) -> ExternalExecutionRequestPort | None:
    if request is None:
        return None
    current = external_execution_request(org=org, request_id=request.id)
    if current is None or current.status != ExternalRequestStatus.RESERVED:
        return current
    return release_execution(
        org=org,
        request_id=current.id,
        error_code=error_code,
        expected_status=ExternalRequestStatus.RESERVED,
    )


def claim_email_execution(
    *,
    org,
    request: ExternalExecutionRequestPort | None,
    intent: EmailExecutionIntent,
) -> ExternalExecutionRequestPort | None:
    """Claim RESERVED exactly once and revalidate the last-mile provider gate."""

    if request is None:
        # Explicit test-only compatibility. The concrete guard still decides.
        assert_provider_io_authorized(
            channel=ExecutionChannel.EMAIL,
            action=EMAIL_SEND_ACTION,
        )
        return None
    if request.status != ExternalRequestStatus.RESERVED:
        raise _error(
            "email_execution_not_replayable",
            "The email execution was already attempted and cannot be replayed.",
        )
    if (
        request.target_hash != intent.target_hash
        or request.payload_hash != intent.payload_hash
        or request.units != intent.units
    ):
        release_reserved_email_execution(
            org=org,
            request=request,
            error_code="email_execution_snapshot_changed",
        )
        raise _error(
            "email_execution_snapshot_changed",
            "The approved email snapshot changed before delivery.",
        )

    sending = mark_execution_sending(
        org=org,
        request_id=request.id,
        expected_status=ExternalRequestStatus.RESERVED,
    )
    try:
        assert_provider_io_authorized(
            org=org,
            channel=ExecutionChannel.EMAIL,
            action=EMAIL_SEND_ACTION,
            execution_request_id=sending.id,
        )
    except Exception:
        # No provider call has begun, so this reservation is safe to refund.
        try:
            release_execution(
                org=org,
                request_id=sending.id,
                error_code="email_execution_revalidation_failed",
                expected_status=ExternalRequestStatus.SENDING,
            )
        except Exception:
            pass
        raise
    return sending


def settle_email_provider_accepted(
    *,
    org,
    request: ExternalExecutionRequestPort | None,
) -> ExternalExecutionRequestPort | None:
    if request is None:
        return None
    accepted = mark_provider_accepted(org=org, request_id=request.id)
    if accepted.status != ExternalRequestStatus.ACCEPTED:
        raise _error(
            "email_execution_acceptance_failed",
            "The provider acceptance could not be recorded safely.",
        )
    return accepted


def settle_email_provider_unknown(
    *,
    org,
    request: ExternalExecutionRequestPort | None,
) -> ExternalExecutionRequestPort | None:
    """Conservatively consume a call whose provider outcome is ambiguous."""

    if request is None:
        return None
    current = external_execution_request(org=org, request_id=request.id)
    if current is None or current.status in {
        ExternalRequestStatus.UNKNOWN,
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.DELIVERED,
    }:
        return current
    if current.status != ExternalRequestStatus.SENDING:
        return current
    return mark_provider_accepted(
        org=org,
        request_id=current.id,
        local_state_uncertain=True,
    )


def settle_email_delivered(
    *,
    org,
    request: ExternalExecutionRequestPort | None,
) -> ExternalExecutionRequestPort | None:
    if request is None:
        return None
    current = external_execution_request(org=org, request_id=request.id)
    if current is None:
        raise _error(
            "email_execution_request_not_found",
            "The email execution request was not found.",
            404,
        )
    if current.status == ExternalRequestStatus.DELIVERED:
        return current
    if current.status != ExternalRequestStatus.ACCEPTED:
        raise _error(
            "email_execution_not_deliverable",
            "The email execution cannot be completed from its current state.",
        )
    return mark_execution_delivered(org=org, request_id=current.id)
