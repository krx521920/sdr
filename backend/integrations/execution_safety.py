"""Fail-closed local guard around real-channel side effects.

No function in this module calls a provider.  Callers reserve authorization and
quota before I/O, then durably report the outcome after the provider call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from integrations.models import (
    ChannelExecutionApproval,
    ChannelExecutionControl,
    ChannelTestTarget,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    OrganizationExecutionControl,
)

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
UNIMPLEMENTED_CHANNELS = {ExecutionChannel.WECHAT, ExecutionChannel.WECOM}


class ExecutionSafetyError(ValueError):
    def __init__(self, *, code: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ExecutionReservation:
    request: ExternalExecutionRequest
    replayed: bool


@dataclass(frozen=True)
class ApprovalIssueResult:
    approval: ChannelExecutionApproval
    replayed: bool

    def __getattr__(self, name):
        # Backwards-compatible read access for internal callers that used the
        # pre-idempotency service return value directly.
        return getattr(self.approval, name)


def assert_provider_io_authorized(
    *,
    org=None,
    channel: str,
    action: str,
    execution_request_id: UUID | None = None,
    target_hash: str | None = None,
    payload_hash: str | None = None,
    units: int | None = None,
    idempotency_key: UUID | None = None,
) -> None:
    """Last-mile network guard used immediately before provider I/O."""

    if getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
        return
    _require_channel(channel)
    if org is None or execution_request_id is None:
        raise _error(
            "execution_approval_required",
            "A reserved and revalidated execution request is required before provider I/O.",
            403,
        )
    if not getattr(settings, "REAL_CHANNEL_EXECUTION_ENABLED", False):
        raise _error("environment_execution_disabled", "Provider I/O is disabled.", 403)
    request = (
        ExternalExecutionRequest.objects.select_related("approval")
        .filter(
            org=org,
            id=execution_request_id,
            channel=channel,
            action=action,
            status=ExternalRequestStatus.SENDING,
        )
        .first()
    )
    if request is None:
        raise _error(
            "execution_approval_required",
            "Execution request is absent, mismatched, or not in SENDING state.",
            403,
        )
    approval = request.approval
    if (
        approval.org_id != request.org_id
        or approval.channel != request.channel
        or approval.action != request.action
        or approval.target_hash != request.target_hash
        or approval.payload_hash != request.payload_hash
        or approval.units != request.units
        or approval.consumed_at is None
        or approval.consumed_by_request_id != request.id
    ):
        raise _error(
            "execution_scope_mismatch",
            "The execution request no longer matches its consumed approval.",
            403,
        )
    exact_scope = (target_hash, payload_hash, units, idempotency_key)
    if any(value is not None for value in exact_scope):
        try:
            exact_scope_matches = (
                all(value is not None for value in exact_scope)
                and request.target_hash == str(target_hash)
                and request.payload_hash == str(payload_hash)
                and request.units == units
                and request.idempotency_key == UUID(str(idempotency_key))
            )
        except (TypeError, ValueError):
            exact_scope_matches = False
        if not exact_scope_matches:
            raise _error(
                "execution_scope_mismatch",
                "The provider call no longer matches its approved execution scope.",
                403,
            )
    if not OrganizationExecutionControl.objects.filter(org=org, enabled=True).exists():
        raise _error("organization_execution_disabled", "Organization execution is disabled.", 403)
    control = ChannelExecutionControl.objects.filter(
        org=org, channel=channel, enabled=True
    ).first()
    if control is None:
        raise _error("channel_disabled", "Channel execution is disabled.", 403)
    if control.test_mode and not ChannelTestTarget.objects.filter(
        org=org,
        channel=channel,
        identifier_hash=request.target_hash,
        is_active=True,
    ).exists():
        raise _error("target_not_allowlisted", "Target is no longer allowlisted.", 403)


def _error(code, detail, status_code=400):
    return ExecutionSafetyError(code=code, detail=detail, status_code=status_code)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_hash(value: str, field: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not HASH_RE.fullmatch(cleaned):
        raise _error("invalid_hash", f"{field} must be a lowercase SHA-256 digest.")
    return cleaned


def _require_channel(channel: str) -> None:
    if channel not in ExecutionChannel.values:
        raise _error("invalid_channel", "Unsupported execution channel.")
    if channel in UNIMPLEMENTED_CHANNELS:
        raise _error(
            "channel_not_implemented",
            "This channel cannot be enabled or authorized.",
            403,
        )


def _require_admin(*, org, actor) -> None:
    if (
        actor is None
        or actor.org_id != org.id
        or not actor.is_active
        or not (actor.role == "ADMIN" or actor.is_organization_admin)
    ):
        raise _error(
            "approval_permission_denied",
            "An active organization administrator is required.",
            403,
        )


def normalize_target_identifier(*, channel: str, identifier: str) -> str:
    value = str(identifier or "").strip()
    if not value or len(value) > 1000 or any(ord(char) < 32 for char in value):
        raise _error("invalid_target", "Target identifier is invalid.")
    if channel == ExecutionChannel.EMAIL:
        value = value.casefold()
    elif channel in {ExecutionChannel.WHATSAPP, ExecutionChannel.WECHAT, ExecutionChannel.WECOM}:
        value = re.sub(r"[\s()+.\-]", "", value).casefold()
    else:
        value = value.casefold()
    return value


def hash_target_identifier(*, org, channel: str, identifier: str) -> str:
    normalized = normalize_target_identifier(channel=channel, identifier=identifier)
    secret = str(settings.SECRET_KEY).encode("utf-8")
    message = f"channel-target:v1:{org.id}:{channel}:{normalized}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


@transaction.atomic
def configure_channel(
    *, org, actor, channel: str, enabled: bool, test_mode: bool, daily_limit: int,
    per_execution_limit: int, expected_revision: int | None = None
) -> ChannelExecutionControl:
    _require_admin(org=org, actor=actor)
    _require_channel(channel)
    if not isinstance(daily_limit, int) or not isinstance(per_execution_limit, int):
        raise _error("invalid_limit", "Execution limits must be integers.")
    if not 0 <= daily_limit <= 1_000_000 or not 0 <= per_execution_limit <= 1_000_000:
        raise _error("invalid_limit", "Execution limits are outside the supported range.")
    if enabled and (daily_limit < 1 or per_execution_limit < 1):
        raise _error("invalid_limit", "Enabled channels require positive execution limits.")
    control = ChannelExecutionControl.objects.select_for_update().filter(
        org=org, channel=channel
    ).first()
    if control is None:
        if expected_revision not in (None, 0):
            raise _error("revision_conflict", "Channel configuration changed.", 409)
        control = ChannelExecutionControl(org=org, channel=channel)
    elif expected_revision is not None and control.revision != expected_revision:
        raise _error("revision_conflict", "Channel configuration changed.", 409)
    control.enabled = enabled
    control.test_mode = test_mode
    control.daily_limit = daily_limit
    control.per_execution_limit = per_execution_limit
    control.revision += 1
    control.save()
    return control


@transaction.atomic
def configure_organization_execution(
    *, org, actor, enabled: bool, daily_limit: int, expected_revision: int | None = None
) -> OrganizationExecutionControl:
    _require_admin(org=org, actor=actor)
    if not isinstance(daily_limit, int) or not 0 <= daily_limit <= 10_000_000:
        raise _error("invalid_limit", "Organization daily limit is invalid.")
    if enabled and daily_limit < 1:
        raise _error("invalid_limit", "Enabled execution requires a positive daily limit.")
    control = OrganizationExecutionControl.objects.select_for_update().filter(org=org).first()
    if control is None:
        if expected_revision not in (None, 0):
            raise _error("revision_conflict", "Organization configuration changed.", 409)
        control = OrganizationExecutionControl(org=org)
    elif expected_revision is not None and control.revision != expected_revision:
        raise _error("revision_conflict", "Organization configuration changed.", 409)
    control.enabled = enabled
    control.daily_limit = daily_limit
    control.revision += 1
    control.save()
    return control


def add_test_target(
    *, org, actor, channel: str, identifier: str, safe_label: str
) -> ChannelTestTarget:
    _require_admin(org=org, actor=actor)
    _require_channel(channel)
    label = str(safe_label or "").strip()
    if (
        not label
        or len(label) > 120
        or "@" in label
        or re.search(r"https?://|\+?\d{7,}", label, re.IGNORECASE)
    ):
        raise _error("invalid_safe_label", "A bounded safe label is required.")
    identifier_hash = hash_target_identifier(org=org, channel=channel, identifier=identifier)
    target, _ = ChannelTestTarget.objects.update_or_create(
        org=org,
        channel=channel,
        identifier_hash=identifier_hash,
        defaults={"safe_label": label, "is_active": True},
    )
    return target


def disable_test_target(*, org, actor, target_id: UUID) -> ChannelTestTarget:
    _require_admin(org=org, actor=actor)
    with transaction.atomic():
        target = ChannelTestTarget.objects.select_for_update().filter(
            org=org, id=target_id
        ).first()
        if target is None:
            raise _error("test_target_not_found", "Test target was not found.", 404)
        target.is_active = False
        target.save(update_fields=["is_active", "updated_at"])
    return target


def _approval_request_hash(*, channel, action, target_hash, payload_hash, units, expires_in):
    return _sha256(
        _canonical(
            {
                "channel": channel,
                "action": action,
                "target_hash": target_hash,
                "payload_hash": payload_hash,
                "units": units,
                "expires_seconds": int(expires_in.total_seconds()),
            }
        )
    )


def issue_execution_approval(
    *,
    org,
    approved_by,
    channel: str,
    action: str,
    target_hash: str,
    payload_hash: str,
    units: int,
    idempotency_key: UUID | None = None,
    expires_in: timedelta = timedelta(minutes=15),
) -> ApprovalIssueResult:
    _require_admin(org=org, actor=approved_by)
    idempotency_key = idempotency_key or uuid4()
    _require_channel(channel)
    if not ACTION_RE.fullmatch(str(action or "")):
        raise _error("invalid_execution_scope", "Channel or action is invalid.")
    target_hash = _require_hash(target_hash, "target_hash")
    payload_hash = _require_hash(payload_hash, "payload_hash")
    if not isinstance(units, int) or units < 1 or units > 1_000_000:
        raise _error("invalid_units", "units must be between 1 and 1000000.")
    if expires_in <= timedelta(0) or expires_in > timedelta(hours=24):
        raise _error("invalid_expiry", "Approval expiry must be within 24 hours.")
    request_hash = _approval_request_hash(
        channel=channel,
        action=action,
        target_hash=target_hash,
        payload_hash=payload_hash,
        units=units,
        expires_in=expires_in,
    )
    existing = ChannelExecutionApproval.objects.filter(
        org=org, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise _error("approval_idempotency_conflict", "Idempotency-Key was reused.", 409)
        return ApprovalIssueResult(existing, True)
    try:
        with transaction.atomic():
            approval = ChannelExecutionApproval.objects.create(
                org=org,
                approved_by=approved_by,
                channel=channel,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                action=action,
                target_hash=target_hash,
                payload_hash=payload_hash,
                units=units,
                expires_at=timezone.now() + expires_in,
            )
    except IntegrityError:
        existing = ChannelExecutionApproval.objects.get(
            org=org, idempotency_key=idempotency_key
        )
        if existing.request_hash != request_hash:
            raise _error("approval_idempotency_conflict", "Idempotency-Key was reused.", 409)
        return ApprovalIssueResult(existing, True)
    return ApprovalIssueResult(approval, False)


def _reservation_hash(*, channel, action, target_hash, payload_hash, units, approval_id):
    return _sha256(
        _canonical(
            {
                "schema_version": 1,
                "channel": channel,
                "action": action,
                "target_hash": target_hash,
                "payload_hash": payload_hash,
                "units": units,
                "approval_id": str(approval_id),
            }
        )
    )


@transaction.atomic
def reserve_execution(
    *, org, channel: str, action: str, target_hash: str, payload_hash: str,
    units: int, approval_id: UUID, idempotency_key: UUID
) -> ExecutionReservation:
    _require_channel(channel)
    if not getattr(settings, "REAL_CHANNEL_EXECUTION_ENABLED", False):
        raise _error(
            "environment_execution_disabled",
            "Real-channel execution is disabled by the environment.",
            403,
        )
    if not ACTION_RE.fullmatch(str(action or "")):
        raise _error("invalid_execution_scope", "Channel or action is invalid.")
    target_hash = _require_hash(target_hash, "target_hash")
    payload_hash = _require_hash(payload_hash, "payload_hash")
    if not isinstance(units, int) or units < 1:
        raise _error("invalid_units", "units must be positive.")
    request_hash = _reservation_hash(
        channel=channel,
        action=action,
        target_hash=target_hash,
        payload_hash=payload_hash,
        units=units,
        approval_id=approval_id,
    )
    org_control = OrganizationExecutionControl.objects.select_for_update().filter(
        org=org
    ).first()
    if org_control is None or not org_control.enabled:
        raise _error("organization_execution_disabled", "Organization execution is disabled.", 403)
    control = ChannelExecutionControl.objects.select_for_update().filter(
        org=org, channel=channel
    ).first()
    if control is None or not control.enabled:
        raise _error("channel_disabled", "Channel execution is disabled.", 403)
    existing = ExternalExecutionRequest.objects.select_for_update().filter(
        org=org, channel=channel, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise _error("execution_idempotency_conflict", "Idempotency-Key was reused.", 409)
        return ExecutionReservation(request=existing, replayed=True)
    today = timezone.localdate()
    if org_control.usage_date != today:
        org_control.usage_date = today
        org_control.reserved_units = 0
        org_control.consumed_units = 0
    if control.usage_date != today:
        control.usage_date = today
        control.reserved_units = 0
        control.consumed_units = 0
    if units > control.per_execution_limit:
        raise _error("execution_limit_exceeded", "Per-execution limit exceeded.", 409)
    if control.reserved_units + control.consumed_units + units > control.daily_limit:
        raise _error("daily_limit_exceeded", "Daily channel limit exceeded.", 409)
    if org_control.reserved_units + org_control.consumed_units + units > org_control.daily_limit:
        raise _error("organization_daily_limit_exceeded", "Organization daily limit exceeded.", 409)
    if control.test_mode and not ChannelTestTarget.objects.filter(
        org=org, channel=channel, identifier_hash=target_hash, is_active=True
    ).exists():
        raise _error("target_not_allowlisted", "Target is not approved for test mode.", 403)
    approval = ChannelExecutionApproval.objects.select_for_update().filter(
        org=org, id=approval_id
    ).first()
    if approval is None:
        raise _error("approval_not_found", "Execution approval was not found.", 404)
    if approval.consumed_at is not None:
        raise _error("approval_consumed", "Execution approval was already consumed.", 409)
    if approval.expires_at <= timezone.now():
        raise _error("approval_expired", "Execution approval has expired.", 409)
    if (
        approval.channel != channel
        or approval.action != action
        or approval.target_hash != target_hash
        or approval.payload_hash != payload_hash
        or approval.units != units
    ):
        raise _error("approval_scope_mismatch", "Approval does not match this execution.", 409)
    request = ExternalExecutionRequest.objects.create(
        org=org,
        channel=channel,
        action=action,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        target_hash=target_hash,
        payload_hash=payload_hash,
        units=units,
        approval=approval,
        reserved_at=timezone.now(),
    )
    approval.consumed_at = timezone.now()
    approval.consumed_by_request = request
    approval.save(update_fields=["consumed_at", "consumed_by_request", "updated_at"])
    control.reserved_units += units
    control.revision += 1
    control.save(
        update_fields=["usage_date", "reserved_units", "consumed_units", "revision", "updated_at"]
    )
    org_control.reserved_units += units
    org_control.revision += 1
    org_control.save(
        update_fields=["usage_date", "reserved_units", "consumed_units", "revision", "updated_at"]
    )
    return ExecutionReservation(request=request, replayed=False)


def mark_execution_sending(
    *,
    org,
    request_id: UUID,
    expected_status: str | None = None,
) -> ExternalExecutionRequest:
    blocked_code = ""
    channel = ExternalExecutionRequest.objects.only("channel").get(org=org, id=request_id).channel
    with transaction.atomic():
        org_control = OrganizationExecutionControl.objects.select_for_update().filter(org=org).first()
        control = ChannelExecutionControl.objects.select_for_update().filter(
            org=org, channel=channel
        ).first()
        request = ExternalExecutionRequest.objects.select_for_update().get(org=org, id=request_id)
        if expected_status is not None and request.status != expected_status:
            raise _error(
                "invalid_execution_transition",
                "Request changed before it could enter SENDING.",
                409,
            )
        if request.status == ExternalRequestStatus.SENDING:
            return request
        if request.status != ExternalRequestStatus.RESERVED:
            raise _error("invalid_execution_transition", "Request cannot enter SENDING.", 409)
        if not getattr(settings, "REAL_CHANNEL_EXECUTION_ENABLED", False):
            blocked_code = "environment_execution_disabled"
        elif org_control is None or not org_control.enabled:
            blocked_code = "organization_execution_disabled"
        elif control is None or not control.enabled:
            blocked_code = "channel_disabled"
        elif control.test_mode and not ChannelTestTarget.objects.filter(
            org=org,
            channel=request.channel,
            identifier_hash=request.target_hash,
            is_active=True,
        ).exists():
            blocked_code = "target_not_allowlisted"
        if blocked_code:
            if control is not None:
                control.reserved_units = max(0, control.reserved_units - request.units)
                control.revision += 1
                control.save(update_fields=["reserved_units", "revision", "updated_at"])
            if org_control is not None:
                org_control.reserved_units = max(0, org_control.reserved_units - request.units)
                org_control.revision += 1
                org_control.save(update_fields=["reserved_units", "revision", "updated_at"])
            request.status = ExternalRequestStatus.FAILED
            request.error_code = blocked_code
            request.failed_at = timezone.now()
            request.save(update_fields=["status", "error_code", "failed_at", "updated_at"])
        else:
            request.status = ExternalRequestStatus.SENDING
            request.sending_at = timezone.now()
            request.save(update_fields=["status", "sending_at", "updated_at"])
    if blocked_code:
        raise _error(blocked_code, "Execution was stopped by a safety control.", 403)
    return request


@transaction.atomic
def mark_provider_accepted(
    *, org, request_id: UUID, provider_reference: str = "", local_state_uncertain: bool = False
) -> ExternalExecutionRequest:
    channel = ExternalExecutionRequest.objects.only("channel").get(org=org, id=request_id).channel
    org_control = OrganizationExecutionControl.objects.select_for_update().get(org=org)
    control = ChannelExecutionControl.objects.select_for_update().get(org=org, channel=channel)
    request = ExternalExecutionRequest.objects.select_for_update().get(org=org, id=request_id)
    if request.status in {ExternalRequestStatus.ACCEPTED, ExternalRequestStatus.UNKNOWN}:
        return request
    if request.status != ExternalRequestStatus.SENDING:
        raise _error("invalid_execution_transition", "Provider acceptance is not allowed.", 409)
    today = timezone.localdate()
    if control.usage_date != today:
        control.usage_date = today
        control.reserved_units = 0
        control.consumed_units = 0
    if org_control.usage_date != today:
        org_control.usage_date = today
        org_control.reserved_units = 0
        org_control.consumed_units = 0
    control.reserved_units = max(0, control.reserved_units - request.units)
    control.consumed_units += request.units
    control.revision += 1
    control.save(update_fields=["usage_date", "reserved_units", "consumed_units", "revision", "updated_at"])
    org_control.reserved_units = max(0, org_control.reserved_units - request.units)
    org_control.consumed_units += request.units
    org_control.revision += 1
    org_control.save(update_fields=["usage_date", "reserved_units", "consumed_units", "revision", "updated_at"])
    now = timezone.now()
    request.provider_reference_hash = _sha256(str(provider_reference)) if provider_reference else ""
    if local_state_uncertain:
        request.status = ExternalRequestStatus.UNKNOWN
        request.unknown_at = now
        fields = ["status", "unknown_at", "provider_reference_hash", "updated_at"]
    else:
        request.status = ExternalRequestStatus.ACCEPTED
        request.accepted_at = now
        fields = ["status", "accepted_at", "provider_reference_hash", "updated_at"]
    request.save(update_fields=fields)
    return request


@transaction.atomic
def release_execution(
    *,
    org,
    request_id: UUID,
    error_code: str,
    expected_status: str | None = None,
) -> ExternalExecutionRequest:
    channel = ExternalExecutionRequest.objects.only("channel").get(org=org, id=request_id).channel
    org_control = OrganizationExecutionControl.objects.select_for_update().get(org=org)
    control = ChannelExecutionControl.objects.select_for_update().get(org=org, channel=channel)
    request = ExternalExecutionRequest.objects.select_for_update().get(org=org, id=request_id)
    if request.status == ExternalRequestStatus.FAILED:
        return request
    if expected_status is not None and request.status != expected_status:
        raise _error(
            "invalid_execution_transition",
            "Request changed before its reservation could be released.",
            409,
        )
    if request.status not in {ExternalRequestStatus.RESERVED, ExternalRequestStatus.SENDING}:
        raise _error("invalid_execution_transition", "Accepted usage cannot be released.", 409)
    today = timezone.localdate()
    if control.usage_date != today:
        control.usage_date = today
        control.reserved_units = 0
        control.consumed_units = 0
    if org_control.usage_date != today:
        org_control.usage_date = today
        org_control.reserved_units = 0
        org_control.consumed_units = 0
    control.reserved_units = max(0, control.reserved_units - request.units)
    control.revision += 1
    control.save(update_fields=["usage_date", "reserved_units", "consumed_units", "revision", "updated_at"])
    org_control.reserved_units = max(0, org_control.reserved_units - request.units)
    org_control.revision += 1
    org_control.save(update_fields=["usage_date", "reserved_units", "consumed_units", "revision", "updated_at"])
    request.status = ExternalRequestStatus.FAILED
    request.error_code = str(error_code or "provider_failed")[:80]
    request.failed_at = timezone.now()
    request.save(update_fields=["status", "error_code", "failed_at", "updated_at"])
    return request


@transaction.atomic
def mark_execution_delivered(*, org, request_id: UUID) -> ExternalExecutionRequest:
    request = ExternalExecutionRequest.objects.select_for_update().get(org=org, id=request_id)
    if request.status == ExternalRequestStatus.DELIVERED:
        return request
    if request.status not in {ExternalRequestStatus.ACCEPTED, ExternalRequestStatus.UNKNOWN}:
        raise _error("invalid_execution_transition", "Request cannot enter DELIVERED.", 409)
    request.status = ExternalRequestStatus.DELIVERED
    request.delivered_at = timezone.now()
    request.save(update_fields=["status", "delivered_at", "updated_at"])
    return request


@transaction.atomic
def mark_execution_local_state_uncertain(
    *, org, request_id: UUID
) -> ExternalExecutionRequest:
    """Project an already-consumed execution to UNKNOWN without changing quota."""

    request = ExternalExecutionRequest.objects.select_for_update().get(
        org=org,
        id=request_id,
    )
    if request.status == ExternalRequestStatus.UNKNOWN:
        return request
    if request.status != ExternalRequestStatus.ACCEPTED:
        raise _error(
            "invalid_execution_transition",
            "Only an accepted execution can become locally uncertain.",
            409,
        )
    request.status = ExternalRequestStatus.UNKNOWN
    request.unknown_at = timezone.now()
    request.save(update_fields=["status", "unknown_at", "updated_at"])
    return request


@transaction.atomic
def resolve_unknown_execution(
    *, org, actor, request_id: UUID, outcome: str
) -> ExternalExecutionRequest:
    _require_admin(org=org, actor=actor)
    request = ExternalExecutionRequest.objects.select_for_update().filter(
        org=org, id=request_id
    ).first()
    if request is None:
        raise _error("execution_request_not_found", "Execution request was not found.", 404)
    if request.status != ExternalRequestStatus.UNKNOWN:
        raise _error("invalid_execution_transition", "Only UNKNOWN requests can be resolved.", 409)
    now = timezone.now()
    if outcome == "delivered":
        request.status = ExternalRequestStatus.DELIVERED
        request.delivered_at = now
        fields = ["status", "delivered_at", "updated_at"]
    elif outcome == "failed_consumed":
        request.status = ExternalRequestStatus.FAILED
        request.error_code = "failed_consumed"
        request.failed_at = now
        fields = ["status", "error_code", "failed_at", "updated_at"]
    else:
        raise _error("invalid_resolution", "Unsupported UNKNOWN resolution.")
    request.save(update_fields=fields)
    _sync_apollo_candidate_resolution(request=request, outcome=outcome)
    _sync_whatsapp_resolution(request=request, outcome=outcome)
    _sync_feishu_base_resolution(request=request, outcome=outcome)
    return request


def _sync_apollo_candidate_resolution(*, request, outcome: str) -> None:
    """Keep Apollo's local candidate state aligned with an admin decision.

    A confirmed delivery is terminal: the response body was not durably
    captured, so it must never be replayed automatically. A confirmed failure
    returns the candidate to the explicit-approval queue; any later attempt is
    a new, separately approved request with a new idempotency key.
    """

    if (
        request.channel != ExecutionChannel.APOLLO
        or request.action != "enrich_person"
    ):
        return
    from django.apps import apps

    candidate_model = apps.get_model("sdr", "SDRApolloCandidate")
    next_status = (
        "skipped" if outcome == "delivered" else "pending_enrichment_approval"
    )
    candidate_model.objects.filter(
        org_id=request.org_id,
        enrichment_request_id=request.id,
        status="unknown",
    ).update(status=next_status, updated_at=timezone.now())


def _whatsapp_message_for_request(*, request):
    """Lock the bound message, recovering only the exact UUID binding if needed."""

    from django.apps import apps

    message_model = apps.get_model("integrations", "WhatsAppMessage")
    message = (
        message_model.objects.select_for_update()
        .filter(
            org_id=request.org_id,
            execution_request_id=request.id,
        )
        .first()
    )
    if message is not None:
        return message
    message = (
        message_model.objects.select_for_update()
        .filter(
            org_id=request.org_id,
            id=request.idempotency_key,
            execution_request_id__isnull=True,
        )
        .first()
    )
    if message is not None:
        message.execution_request_id = request.id
        message.save(update_fields=["execution_request", "updated_at"])
    return message


def _sync_whatsapp_resolution(*, request, outcome: str) -> None:
    """Project an operator's consumed UNKNOWN decision without provider replay."""

    if (
        request.channel != ExecutionChannel.WHATSAPP
        or request.action != "send_message"
    ):
        return
    message = _whatsapp_message_for_request(request=request)
    if message is None:
        return
    now = timezone.now()
    if outcome == "delivered":
        if message.status == "read":
            return
        message.status = "delivered"
        message.delivered_at = message.delivered_at or now
        message.error_code = ""
        message.error_message = ""
        message.save(
            update_fields=[
                "status",
                "delivered_at",
                "error_code",
                "error_message",
                "updated_at",
            ]
        )
        return
    if outcome == "failed_consumed":
        if message.status in {"delivered", "read"}:
            return
        message.status = "failed"
        message.failed_at = now
        message.error_code = "confirmed_provider_failure_consumed"
        message.error_message = "Provider failure was confirmed after quota consumption."
        message.save(
            update_fields=[
                "status",
                "failed_at",
                "error_code",
                "error_message",
                "updated_at",
            ]
        )


def _sync_feishu_base_resolution(*, request, outcome: str) -> None:
    """Project an administrator's UNKNOWN decision without replaying provider I/O."""

    if request.channel != ExecutionChannel.FEISHU or request.action not in {
        "sync_research_result",
        "delete_research_record",
        "import_person_records",
    }:
        return
    from django.apps import apps

    if request.action == "import_person_records":
        import_model = apps.get_model("integrations", "FeishuBasePersonImport")
        person_import = (
            import_model.objects.select_for_update()
            .filter(org_id=request.org_id, execution_request_id=request.id)
            .first()
        )
        if person_import is None:
            return
        person_import.status = "failed"
        person_import.error_code = (
            "confirmed_read_completed_without_preview"
            if outcome == "delivered"
            else "confirmed_read_failed_consumed"
        )
        person_import.completed_at = timezone.now()
        person_import.save(
            update_fields=["status", "error_code", "completed_at", "updated_at"]
        )
        return

    sync_model = apps.get_model("integrations", "FeishuBaseSync")
    sync = (
        sync_model.objects.select_for_update()
        .filter(
            org_id=request.org_id,
            execution_request_id=request.id,
        )
        .first()
    )
    if sync is None:
        return
    if request.action == "delete_research_record":
        if outcome == "delivered":
            sync.clear_record_id()
            sync.status = "external_erasure_completed"
            sync.synced_field_names = []
            sync.error_code = ""
            sync.error_message = ""
            fields = [
                "record_id_ciphertext",
                "record_id_hash",
                "record_safe_label",
                "status",
                "synced_field_names",
                "error_code",
                "error_message",
                "updated_at",
            ]
        else:
            sync.status = "external_erasure_pending"
            sync.error_code = "confirmed_remote_delete_failed"
            sync.error_message = "Remote deletion was confirmed unsuccessful."
            fields = ["status", "error_code", "error_message", "updated_at"]
        sync.save(update_fields=fields)
        return
    if outcome == "failed_consumed":
        sync.status = "failed"
        sync.error_code = "confirmed_remote_sync_failed"
        sync.error_message = "Remote synchronization was confirmed unsuccessful."
        sync.failed_at = timezone.now()
        sync.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "failed_at",
                "updated_at",
            ]
        )
    elif sync.has_remote_record:
        # An update can be confirmed delivered because the encrypted remote id
        # was already known. A create with a lost response must remain UNKNOWN.
        sync.status = "succeeded"
        sync.error_code = ""
        sync.error_message = ""
        sync.save(update_fields=["status", "error_code", "error_message", "updated_at"])
    else:
        sync.error_code = "manual_reconciliation_required"
        sync.error_message = "Delivered create lacks a recoverable remote record id."
        sync.save(update_fields=["error_code", "error_message", "updated_at"])


def reconcile_stale_reserved(*, org, older_than, limit: int = 100) -> list[UUID]:
    """Release quota held by jobs that never reached provider I/O.

    The expected-state check closes the race with a worker claiming the same
    request: once a worker changes RESERVED to SENDING, this reconciler can no
    longer refund or relabel that request as a pre-I/O failure.
    """

    if limit < 1 or limit > 500:
        raise _error("invalid_limit", "Reconciliation limit must be between 1 and 500.")
    request_ids = list(
        ExternalExecutionRequest.objects.filter(
            org=org,
            status=ExternalRequestStatus.RESERVED,
            reserved_at__lte=older_than,
        )
        .order_by("reserved_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    reconciled = []
    for request_id in request_ids:
        try:
            with transaction.atomic():
                request = release_execution(
                    org=org,
                    request_id=request_id,
                    error_code="stale_reservation_released",
                    expected_status=ExternalRequestStatus.RESERVED,
                )
                if (
                    request.status == ExternalRequestStatus.FAILED
                    and request.error_code == "stale_reservation_released"
                ):
                    _project_whatsapp_stale_failed(request=request)
                    _project_feishu_stale_failed(request=request)
        except (ExecutionSafetyError, ExternalExecutionRequest.DoesNotExist):
            continue
        if (
            request.status == ExternalRequestStatus.FAILED
            and request.error_code == "stale_reservation_released"
        ):
            reconciled.append(request.id)
    return reconciled


def _project_whatsapp_stale_failed(*, request) -> None:
    """Atomically fail a never-started WhatsApp message without state regression."""

    if (
        request.channel != ExecutionChannel.WHATSAPP
        or request.action != "send_message"
    ):
        return
    message = _whatsapp_message_for_request(request=request)
    if message is None:
        return
    if message.status in {"sent", "delivered", "read"}:
        raise _error(
            "whatsapp_terminal_state_conflict",
            "A terminal WhatsApp message cannot be projected as stale RESERVED.",
            409,
        )
    message.status = "failed"
    message.failed_at = timezone.now()
    message.error_code = "stale_reservation_released"
    message.error_message = "The approved provider call did not begin before expiry."
    message.save(
        update_fields=[
            "status",
            "failed_at",
            "error_code",
            "error_message",
            "updated_at",
        ]
    )


def _project_feishu_stale_failed(*, request) -> None:
    """Atomically align a never-started Feishu import with its refunded request."""

    if (
        request.channel != ExecutionChannel.FEISHU
        or request.action != "import_person_records"
    ):
        return
    from django.apps import apps

    import_model = apps.get_model("integrations", "FeishuBasePersonImport")
    person_import = (
        import_model.objects.select_for_update()
        .filter(
            org_id=request.org_id,
            execution_request_id=request.id,
        )
        .first()
    )
    if person_import is None:
        raise _error(
            "feishu_import_ledger_missing",
            "The Feishu import ledger is missing; stale quota release was stopped.",
            409,
        )
    if person_import.status != "queued":
        raise _error(
            "feishu_import_state_mismatch",
            "The Feishu import state is not queued; stale quota release was stopped.",
            409,
        )
    person_import.status = "failed"
    person_import.error_code = "stale_reservation_released"
    person_import.completed_at = timezone.now()
    person_import.save(
        update_fields=["status", "error_code", "completed_at", "updated_at"]
    )


def reconcile_stale_sending(*, org, older_than, limit: int = 100) -> list[UUID]:
    """Conservatively account stale in-flight calls as UNKNOWN for later reconciliation."""

    if limit < 1 or limit > 500:
        raise _error("invalid_limit", "Reconciliation limit must be between 1 and 500.")
    request_ids = list(
        ExternalExecutionRequest.objects.filter(
            org=org,
            status=ExternalRequestStatus.SENDING,
            sending_at__lte=older_than,
        )
        .order_by("sending_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    reconciled = []
    for request_id in request_ids:
        try:
            with transaction.atomic():
                request = mark_provider_accepted(
                    org=org,
                    request_id=request_id,
                    local_state_uncertain=True,
                )
                # A live worker can finish its provider call while this
                # reconciler waits for the execution-control/request locks.
                # mark_provider_accepted then returns the already ACCEPTED
                # request unchanged. Only a transition that actually resulted
                # in UNKNOWN may project provider-specific ledgers; otherwise
                # an active Feishu import would be relabelled underneath its
                # successful preview persistence.
                if request.status == ExternalRequestStatus.UNKNOWN:
                    _project_whatsapp_stale_unknown(request=request)
                    _project_feishu_stale_unknown(request=request)
        except (ExecutionSafetyError, ExternalExecutionRequest.DoesNotExist):
            # The candidate list is intentionally unlocked. A live worker may
            # reach DELIVERED/FAILED before this reconciler acquires the row;
            # that is a benign race, not a reason to abort the reconciliation
            # batch or alter the provider-specific ledger.
            continue
        if request.status == ExternalRequestStatus.UNKNOWN:
            reconciled.append(request.id)
    return reconciled


def _project_whatsapp_stale_unknown(*, request) -> None:
    """Align an in-flight WhatsApp message while preserving provider evidence."""

    if (
        request.channel != ExecutionChannel.WHATSAPP
        or request.action != "send_message"
    ):
        return
    message = _whatsapp_message_for_request(request=request)
    if message is None or message.status in {"sent", "delivered", "read"}:
        return
    message.status = "unknown"
    message.failed_at = None
    message.error_code = "stale_whatsapp_outcome_unknown"
    message.error_message = "Provider outcome requires manual reconciliation."
    message.save(
        update_fields=[
            "status",
            "failed_at",
            "error_code",
            "error_message",
            "updated_at",
        ]
    )


def _project_feishu_stale_unknown(*, request) -> None:
    """Keep Feishu's local ledger aligned with a stale in-flight request."""

    if request.channel != ExecutionChannel.FEISHU or request.action not in {
        "sync_research_result",
        "delete_research_record",
        "import_person_records",
    }:
        return
    from django.apps import apps

    if request.action == "import_person_records":
        import_model = apps.get_model("integrations", "FeishuBasePersonImport")
        person_import = (
            import_model.objects.select_for_update()
            .filter(org_id=request.org_id, execution_request_id=request.id)
            .first()
        )
        if person_import is None:
            return
        person_import.status = "unknown"
        person_import.error_code = "stale_feishu_import_outcome_unknown"
        person_import.completed_at = timezone.now()
        person_import.save(
            update_fields=["status", "error_code", "completed_at", "updated_at"]
        )
        return

    sync_model = apps.get_model("integrations", "FeishuBaseSync")
    sync = (
        sync_model.objects.select_for_update()
        .filter(
            org_id=request.org_id,
            execution_request_id=request.id,
        )
        .first()
    )
    if sync is None:
        return
    sync.status = "unknown"
    sync.error_code = "stale_feishu_outcome_unknown"
    sync.error_message = "Provider outcome requires manual reconciliation."
    sync.failed_at = timezone.now()
    sync.save(
        update_fields=[
            "status",
            "error_code",
            "error_message",
            "failed_at",
            "updated_at",
        ]
    )
