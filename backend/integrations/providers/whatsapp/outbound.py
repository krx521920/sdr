"""Durable WhatsApp template delivery for SDR outbound campaigns."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJob, AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from integrations.execution_safety import (
    ExecutionSafetyError,
    mark_execution_local_state_uncertain,
    mark_execution_sending,
    mark_provider_accepted,
    release_execution,
    reserve_execution,
)
from integrations.models import (
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
)
from integrations.providers.whatsapp.client import (
    WhatsAppCloudAPIError,
    WhatsAppCloudClient,
    whatsapp_provider_execution_hashes,
)
from sdr.compliance import evaluate_contact
from sdr.models import (
    OutboundCampaignStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
)

logger = logging.getLogger(__name__)

WHATSAPP_MESSAGE_JOB = "whatsapp.send_campaign_message"
WHATSAPP_SEND_ACTION = "send_message"
PHONE_SEPARATORS = re.compile(r"[\s()+.\-]")
SAFE_PROVIDER_ERROR_CODE = re.compile(r"^whatsapp_[a-z0-9_:-]{1,68}$")
PROVIDER_REJECTION_DETAIL = "WhatsApp provider rejected the message before acceptance."
PROVIDER_FAILURE_DETAIL = "The WhatsApp provider request could not be completed safely."


class WhatsAppCampaignUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WhatsAppExecutionIntent:
    """Non-PII approval scope for one immutable WhatsApp provider call."""

    target_hash: str
    payload_hash: str
    units: int = 1


@dataclass(frozen=True, slots=True)
class WhatsAppExecutionSubmission:
    request: ExternalExecutionRequest
    job: Any | None
    replayed: bool


def _approved_job_key(message_id: UUID) -> str:
    return f"whatsapp-approved-message:{message_id}"


def normalize_whatsapp_recipient(value: str) -> str:
    cleaned = PHONE_SEPARATORS.sub("", (value or "").strip())
    if not cleaned.isdigit() or not 8 <= len(cleaned) <= 15:
        raise WhatsAppCampaignUnavailable(
            "A WhatsApp recipient must contain 8-15 international phone digits."
        )
    return cleaned


def active_whatsapp_connection(*, org_id: UUID) -> WhatsAppBusinessConnection | None:
    return (
        WhatsAppBusinessConnection.objects.filter(org_id=org_id, is_active=True)
        .select_related("route")
        .first()
    )


def whatsapp_message_execution_intent(
    message: WhatsAppMessage,
) -> WhatsAppExecutionIntent:
    """Fingerprint the exact Meta call without returning or persisting its PII."""

    recipient = normalize_whatsapp_recipient(message.recipient)
    target_hash, payload_hash = whatsapp_provider_execution_hashes(
        org=message.org,
        message_id=message.id,
        campaign_id=message.campaign_id,
        campaign_run=message.campaign_run,
        phone_number_id=message.connection.phone_number_id,
        recipient=recipient,
        template_name=message.template_name,
        language_code=message.template_language,
    )
    return WhatsAppExecutionIntent(
        target_hash=target_hash,
        payload_hash=payload_hash,
    )


def reserve_whatsapp_message_send(
    *,
    org,
    message_id: UUID,
    approval_id: UUID,
    intent: WhatsAppExecutionIntent,
):
    """Consume an exact approval using the message UUID as the replay barrier."""

    return reserve_execution(
        org=org,
        channel=ExecutionChannel.WHATSAPP,
        action=WHATSAPP_SEND_ACTION,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=intent.units,
        approval_id=approval_id,
        idempotency_key=message_id,
    )


def reserve_and_enqueue_whatsapp_message(
    message: WhatsAppMessage,
    *,
    approval_id: UUID,
) -> WhatsAppExecutionSubmission:
    """Reserve quota and enqueue one exact approved message, idempotently."""

    failure = None
    submission = None
    with transaction.atomic():
        intent = whatsapp_message_execution_intent(message)
        reservation = reserve_whatsapp_message_send(
            org=message.org,
            message_id=message.id,
            approval_id=approval_id,
            intent=intent,
        )
        if reservation.request.status != ExternalRequestStatus.RESERVED:
            submission = WhatsAppExecutionSubmission(
                request=reservation.request,
                job=None,
                replayed=True,
            )
        else:
            try:
                job = enqueue_approved_whatsapp_message(
                    message,
                    execution_request_id=reservation.request.id,
                )
            except Exception as exc:
                active_job_exists = AutomationJob.objects.filter(
                    org_id=message.org_id,
                    name=WHATSAPP_MESSAGE_JOB,
                    idempotency_key=_approved_job_key(message.id),
                    status__in={
                        AutomationJobStatus.PENDING,
                        AutomationJobStatus.QUEUED,
                        AutomationJobStatus.RUNNING,
                        AutomationJobStatus.RETRY_SCHEDULED,
                    },
                ).exists()
                current = ExternalExecutionRequest.objects.select_for_update().get(
                    id=reservation.request.id,
                    org_id=message.org_id,
                )
                if (
                    current.status == ExternalRequestStatus.RESERVED
                    and not active_job_exists
                ):
                    error_code = getattr(
                        exc,
                        "code",
                        "whatsapp_enqueue_failed",
                    )
                    release_execution(
                        org=message.org,
                        request_id=current.id,
                        error_code=error_code,
                        expected_status=ExternalRequestStatus.RESERVED,
                    )
                failure = exc
            else:
                submission = WhatsAppExecutionSubmission(
                    request=reservation.request,
                    job=job,
                    replayed=reservation.replayed,
                )
    if failure is not None:
        raise failure
    return submission


def enqueue_approved_whatsapp_message(
    message: WhatsAppMessage,
    *,
    execution_request_id: UUID,
):
    """Enqueue exactly one approved, single-attempt WhatsApp provider job."""

    with transaction.atomic():
        # Keep the global WhatsApp lock order execution request -> message.
        # Webhooks and reconciliation use the same order.
        request = ExternalExecutionRequest.objects.select_for_update().filter(
            id=execution_request_id,
            org_id=message.org_id,
        ).first()
        locked = (
            WhatsAppMessage.objects.select_for_update()
            .select_related("org", "connection__route")
            .filter(id=message.id, org_id=message.org_id)
            .first()
        )
        if locked is None:
            raise _execution_error(
                "whatsapp_message_not_found",
                "The WhatsApp message no longer exists.",
                404,
            )
        _validate_execution_binding(message=locked, request=request)
        linked_request_id = getattr(locked, "execution_request_id", None)
        if linked_request_id is None and hasattr(locked, "execution_request_id"):
            WhatsAppMessage.objects.filter(
                id=locked.id,
                org_id=locked.org_id,
            ).update(execution_request_id=request.id)
            locked.execution_request_id = request.id
        elif linked_request_id not in {None, request.id}:
            raise _execution_error(
                "whatsapp_execution_scope_mismatch",
                "The WhatsApp message is already bound to another execution.",
            )
        if request.status != ExternalRequestStatus.RESERVED:
            raise _execution_error(
                "whatsapp_execution_not_replayable",
                "The WhatsApp execution has already been attempted and cannot be replayed.",
            )
        if locked.status not in {
            WhatsAppMessageStatus.PENDING,
            WhatsAppMessageStatus.QUEUED,
        }:
            raise _execution_error(
                "whatsapp_message_not_approvable",
                "Only a pending WhatsApp message can be approved.",
            )
        intent = whatsapp_message_execution_intent(locked)
        if (
            request.target_hash != intent.target_hash
            or request.payload_hash != intent.payload_hash
            or request.units != intent.units
        ):
            raise _execution_error(
                "whatsapp_execution_snapshot_changed",
                "The approved WhatsApp message snapshot changed before enqueue.",
            )
        enqueued = enqueue_job(
            JobRequest(
                org_id=locked.org_id,
                name=WHATSAPP_MESSAGE_JOB,
                idempotency_key=_approved_job_key(locked.id),
                payload={
                    "org_id": str(locked.org_id),
                    "message_id": str(locked.id),
                    "execution_request_id": str(request.id),
                },
                max_attempts=1,
            )
        )
        job = enqueued.job
        if job.status in {
            AutomationJobStatus.DEAD_LETTER,
            AutomationJobStatus.CANCELLED,
        }:
            raise _execution_error(
                "whatsapp_job_not_replayable",
                "The approved WhatsApp job is terminal and cannot be replayed.",
            )
        if enqueued.created:
            WhatsAppMessage.objects.filter(
                id=locked.id,
                org_id=locked.org_id,
            ).update(status=WhatsAppMessageStatus.QUEUED)
            transaction.on_commit(lambda: _safe_dispatch(job))
    return job


def enqueue_whatsapp_campaign_message(
    *,
    prospect: SDROutboundProspect,
    campaign: SDROutboundCampaign,
    campaign_run: int,
    force_retry: bool = False,
) -> WhatsAppMessage:
    if campaign_run < 1 or campaign.run_count != campaign_run:
        raise WhatsAppCampaignUnavailable("The WhatsApp campaign run is stale.")
    connection = active_whatsapp_connection(org_id=campaign.org_id)
    if connection is None:
        raise WhatsAppCampaignUnavailable(
            "Configure and enable a WhatsApp Business connection first."
        )
    recipient = normalize_whatsapp_recipient(prospect.phone)
    if not campaign.whatsapp_template_name.strip():
        raise WhatsAppCampaignUnavailable(
            "Configure an approved WhatsApp template name for the campaign."
        )
    compliance = evaluate_contact(
        org_id=campaign.org_id,
        channel="whatsapp",
        identifier=recipient,
        country_code=prospect.country,
        prospect=prospect,
        event_key=f"whatsapp:{campaign.id}:{campaign_run}:{prospect.id}",
    )

    with transaction.atomic():
        message, _ = WhatsAppMessage.objects.get_or_create(
            org_id=campaign.org_id,
            campaign=campaign,
            prospect=prospect,
            campaign_run=campaign_run,
            defaults={
                "connection": connection,
                "recipient": recipient,
                "template_name": campaign.whatsapp_template_name.strip(),
                "template_language": campaign.whatsapp_template_language.strip(),
            },
        )
        if message.status in {
            WhatsAppMessageStatus.SENT,
            WhatsAppMessageStatus.DELIVERED,
            WhatsAppMessageStatus.READ,
        }:
            return message
        if not compliance.allowed:
            _mark_skipped(
                message,
                compliance.code,
                error=compliance.reason,
            )
            return message
        if not getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
            # Production-safe mode deliberately stops at a reviewable message.
            # An administrator must reserve an exact execution request before a
            # single-attempt job is created.
            return message
        retry_suffix = (
            f":retry:{message.attempt_count + 1}"
            if force_retry and message.status == WhatsAppMessageStatus.FAILED
            else ""
        )
        enqueued = enqueue_job(
            JobRequest(
                org_id=campaign.org_id,
                name=WHATSAPP_MESSAGE_JOB,
                idempotency_key=f"whatsapp-message:{message.id}{retry_suffix}",
                payload={
                    "org_id": str(campaign.org_id),
                    "message_id": str(message.id),
                },
                max_attempts=5,
            )
        )
        job = enqueued.job
        terminal_replay = enqueued.terminal_replay
        if job.status == AutomationJobStatus.DEAD_LETTER:
            job = replay_dead_letter(job_id=job.id, org_id=campaign.org_id)
            terminal_replay = False
        elif job.status == AutomationJobStatus.CANCELLED:
            raise WhatsAppCampaignUnavailable(
                "The WhatsApp message job was cancelled and cannot be replayed."
            )
        if not terminal_replay:
            WhatsAppMessage.objects.filter(
                id=message.id,
                org_id=campaign.org_id,
            ).update(status=WhatsAppMessageStatus.QUEUED)
            message.status = WhatsAppMessageStatus.QUEUED
            transaction.on_commit(lambda: _safe_dispatch(job))
    return message


def retry_failed_whatsapp_messages(campaign: SDROutboundCampaign) -> int:
    if not getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
        # A failed/unknown real-channel call needs a fresh operator decision;
        # campaign-level retry must never replay provider I/O.
        return 0
    queued = 0
    messages = WhatsAppMessage.objects.filter(
        org_id=campaign.org_id,
        campaign=campaign,
        campaign_run=campaign.run_count,
        status=WhatsAppMessageStatus.FAILED,
    ).select_related("prospect")
    for message in messages.iterator(chunk_size=100):
        enqueue_whatsapp_campaign_message(
            prospect=message.prospect,
            campaign=campaign,
            campaign_run=campaign.run_count,
            force_retry=True,
        )
        queued += 1
    return queued


def process_whatsapp_message_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        message_id = UUID(str(payload["message_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The WhatsApp message job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    message = (
        WhatsAppMessage.objects.filter(id=message_id, org_id=org_id)
        .select_related("org", "connection__route", "campaign", "prospect")
        .first()
    )
    if message is None:
        raise PermanentJobError(
            "The WhatsApp message no longer exists.",
            code="whatsapp_message_not_found",
        )
    if getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
        return _process_legacy_whatsapp_message(message)
    return _process_approved_whatsapp_message(message, payload=payload)


def _process_legacy_whatsapp_message(message: WhatsAppMessage) -> Mapping[str, Any]:
    """Compatibility path used only by the explicit test settings module."""

    org_id = message.org_id
    if message.status in {
        WhatsAppMessageStatus.SENT,
        WhatsAppMessageStatus.DELIVERED,
        WhatsAppMessageStatus.READ,
        WhatsAppMessageStatus.SKIPPED,
    }:
        return _result(message, replayed=True)
    campaign = message.campaign
    if (
        campaign.status != OutboundCampaignStatus.ACTIVE
        or campaign.run_count != message.campaign_run
        or "whatsapp" not in campaign.channels
    ):
        _mark_skipped(message, "campaign_not_active")
        return _result(message, replayed=False)
    if not message.connection.is_active:
        _mark_failed(
            message,
            code="whatsapp_connection_inactive",
            error="The WhatsApp Business connection is inactive.",
        )
        raise PermanentJobError(
            "The WhatsApp Business connection is inactive.",
            code="whatsapp_connection_inactive",
        )
    compliance = evaluate_contact(
        org_id=org_id,
        channel="whatsapp",
        identifier=message.recipient,
        country_code=message.prospect.country,
        prospect=message.prospect,
        event_key=f"whatsapp:send:{message.id}",
    )
    if not compliance.allowed:
        _mark_skipped(message, compliance.code, error=compliance.reason)
        return _result(message, replayed=False)

    WhatsAppMessage.objects.filter(id=message.id, org_id=org_id).update(
        status=WhatsAppMessageStatus.SENDING,
        attempt_count=message.attempt_count + 1,
        error_code="",
        error_message="",
        failed_at=None,
    )
    message.status = WhatsAppMessageStatus.SENDING
    message.attempt_count += 1
    try:
        response = _client().send_template(
            phone_number_id=message.connection.phone_number_id,
            access_token=message.connection.get_access_token(),
            recipient=message.recipient,
            template_name=message.template_name,
            language_code=message.template_language,
        )
    except WhatsAppCloudAPIError as exc:
        error_code = _safe_provider_error_code(exc.error_code)
        safe_detail = (
            PROVIDER_REJECTION_DETAIL if exc.outcome_known else PROVIDER_FAILURE_DETAIL
        )
        _mark_failed(message, code=error_code, error=safe_detail)
        error_type = RetryableJobError if exc.retryable else PermanentJobError
        raise error_type(safe_detail, code=error_code) from exc

    provider_message_id = str(response["messages"][0]["id"]).strip()[:255]
    sent_at = timezone.now()
    WhatsAppMessage.objects.filter(id=message.id, org_id=org_id).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id=provider_message_id,
        sent_at=sent_at,
        error_code="",
        error_message="",
        provider_status_snapshot={"accepted": True},
    )
    WhatsAppBusinessConnection.objects.filter(
        id=message.connection_id,
        org_id=org_id,
    ).update(last_message_sent_at=sent_at)
    message.status = WhatsAppMessageStatus.SENT
    message.provider_message_id = provider_message_id
    message.sent_at = sent_at
    return _result(message, replayed=False)


def _process_approved_whatsapp_message(
    message: WhatsAppMessage,
    *,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Execute one exact reservation without any automatic provider replay."""

    try:
        execution_request_id = UUID(str(payload["execution_request_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The WhatsApp job has no valid approved execution request.",
            code="execution_approval_required",
        ) from exc

    request = ExternalExecutionRequest.objects.filter(
        id=execution_request_id,
        org_id=message.org_id,
    ).first()
    try:
        _validate_execution_binding(message=message, request=request)
    except ExecutionSafetyError as exc:
        raise PermanentJobError(exc.detail, code=exc.code) from exc

    if request.status in {
        ExternalRequestStatus.SENDING,
        ExternalRequestStatus.UNKNOWN,
        ExternalRequestStatus.FAILED,
    }:
        raise PermanentJobError(
            "The WhatsApp execution was already attempted and cannot be replayed.",
            code="whatsapp_execution_not_replayable",
        )

    if request.status in {
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.DELIVERED,
    }:
        if message.status not in {
            WhatsAppMessageStatus.SENT,
            WhatsAppMessageStatus.DELIVERED,
            WhatsAppMessageStatus.READ,
        }:
            raise PermanentJobError(
                "Provider acceptance exists but the local WhatsApp message is incomplete.",
                code="whatsapp_local_state_incomplete",
            )
        return _secure_result(message, replayed=True)

    if request.status != ExternalRequestStatus.RESERVED:
        raise PermanentJobError(
            "The WhatsApp execution cannot be processed in its current state.",
            code="whatsapp_execution_not_replayable",
        )
    if message.status == WhatsAppMessageStatus.SKIPPED:
        release_execution(
            org=message.org,
            request_id=request.id,
            error_code="whatsapp_message_skipped",
            expected_status=ExternalRequestStatus.RESERVED,
        )
        return _secure_result(message, replayed=False)
    if message.status in {
        WhatsAppMessageStatus.SENT,
        WhatsAppMessageStatus.DELIVERED,
        WhatsAppMessageStatus.READ,
        WhatsAppMessageStatus.UNKNOWN,
    }:
        # A terminal local receipt is evidence that provider I/O may already
        # have happened. Never refund or downgrade it merely because the local
        # execution ledger is stale.
        raise PermanentJobError(
            "The WhatsApp message and execution request need reconciliation.",
            code="whatsapp_execution_state_conflict",
        )
    if message.status not in {
        WhatsAppMessageStatus.PENDING,
        WhatsAppMessageStatus.QUEUED,
    }:
        _release_approved_execution(
            message=message,
            request=request,
            code="whatsapp_message_not_sendable",
            detail="The WhatsApp message is not pending approval execution.",
        )

    intent = whatsapp_message_execution_intent(message)
    if (
        request.target_hash != intent.target_hash
        or request.payload_hash != intent.payload_hash
        or request.units != intent.units
    ):
        _release_approved_execution(
            message=message,
            request=request,
            code="whatsapp_execution_snapshot_changed",
            detail="The approved WhatsApp message snapshot changed before delivery.",
        )

    campaign = message.campaign
    if (
        campaign.status != OutboundCampaignStatus.ACTIVE
        or campaign.run_count != message.campaign_run
        or "whatsapp" not in campaign.channels
    ):
        release_execution(
            org=message.org,
            request_id=request.id,
            error_code="campaign_not_active",
            expected_status=ExternalRequestStatus.RESERVED,
        )
        _mark_skipped(message, "campaign_not_active")
        return _secure_result(message, replayed=False)
    if not message.connection.is_active:
        _release_approved_execution(
            message=message,
            request=request,
            code="whatsapp_connection_inactive",
            detail="The WhatsApp Business connection is inactive.",
        )
    compliance = evaluate_contact(
        org_id=message.org_id,
        channel="whatsapp",
        identifier=message.recipient,
        country_code=message.prospect.country,
        prospect=message.prospect,
        event_key=f"whatsapp:send:{message.id}",
    )
    if not compliance.allowed:
        release_execution(
            org=message.org,
            request_id=request.id,
            error_code=compliance.code,
            expected_status=ExternalRequestStatus.RESERVED,
        )
        _mark_skipped(message, compliance.code, error=compliance.reason)
        return _secure_result(message, replayed=False)

    try:
        access_token = message.connection.get_access_token()
        client = _client(
            org=message.org,
            execution_request_id=request.id,
        )
    except Exception as exc:
        _release_approved_execution(
            message=message,
            request=request,
            code="whatsapp_local_preflight_failed",
            detail="The WhatsApp provider configuration could not be loaded.",
            cause=exc,
        )

    try:
        mark_execution_sending(
            org=message.org,
            request_id=request.id,
            expected_status=ExternalRequestStatus.RESERVED,
        )
    except ExecutionSafetyError as exc:
        _mark_failed(message, code=exc.code, error=exc.detail)
        raise PermanentJobError(exc.detail, code=exc.code) from exc

    WhatsAppMessage.objects.filter(
        id=message.id,
        org_id=message.org_id,
    ).update(
        status=WhatsAppMessageStatus.SENDING,
        attempt_count=message.attempt_count + 1,
        error_code="",
        error_message="",
        failed_at=None,
    )
    message.status = WhatsAppMessageStatus.SENDING
    message.attempt_count += 1
    try:
        response = client.send_template(
            phone_number_id=message.connection.phone_number_id,
            access_token=access_token,
            recipient=message.recipient,
            template_name=message.template_name,
            language_code=message.template_language,
            message_id=message.id,
            campaign_id=message.campaign_id,
            campaign_run=message.campaign_run,
        )
    except WhatsAppCloudAPIError as exc:
        error_code = _safe_provider_error_code(exc.error_code)
        if exc.outcome_known:
            release_execution(
                org=message.org,
                request_id=request.id,
                error_code=error_code,
                expected_status=ExternalRequestStatus.SENDING,
            )
            _mark_failed(
                message,
                code=error_code,
                error=PROVIDER_REJECTION_DETAIL,
            )
            raise PermanentJobError(
                PROVIDER_REJECTION_DETAIL,
                code=error_code,
            ) from exc
        _settle_unknown(message=message, request=request, error=exc)
    except Exception as exc:
        _settle_unknown(message=message, request=request, error=exc)

    try:
        provider_message_id = str(response["messages"][0]["id"]).strip()[:255]
        if not provider_message_id:
            raise ValueError("WhatsApp provider message id is empty")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _settle_unknown(message=message, request=request, error=exc)
    current = ExternalExecutionRequest.objects.get(
        id=request.id,
        org_id=message.org_id,
    )
    if current.status == ExternalRequestStatus.DELIVERED:
        accepted = current
    else:
        try:
            accepted = mark_provider_accepted(
                org=message.org,
                request_id=request.id,
                provider_reference=provider_message_id,
            )
        except Exception as exc:
            try:
                mark_provider_accepted(
                    org=message.org,
                    request_id=request.id,
                    provider_reference=provider_message_id,
                    local_state_uncertain=True,
                )
            except Exception:
                pass
            _mark_unknown(
                message,
                code="whatsapp_local_state_uncertain",
                error="Provider accepted the message but its local state is uncertain.",
            )
            raise PermanentJobError(
                "Provider accepted the WhatsApp message but its local state is uncertain.",
                code="whatsapp_local_state_uncertain",
            ) from exc
        if accepted.status == ExternalRequestStatus.UNKNOWN:
            _mark_unknown(
                message,
                code="whatsapp_execution_outcome_unknown",
                error="The WhatsApp provider outcome requires manual reconciliation.",
            )
            raise PermanentJobError(
                "The WhatsApp provider outcome requires manual reconciliation.",
                code="whatsapp_execution_outcome_unknown",
            )

    sent_at = timezone.now()
    try:
        _mark_sent(
            message,
            provider_message_id=provider_message_id,
            sent_at=sent_at,
        )
    except Exception as exc:
        try:
            mark_execution_local_state_uncertain(
                org=message.org,
                request_id=request.id,
            )
        except Exception:
            pass
        raise PermanentJobError(
            "Provider accepted the WhatsApp message but local persistence failed.",
            code="whatsapp_local_state_incomplete",
        ) from exc

    current = ExternalExecutionRequest.objects.get(
        id=request.id,
        org_id=message.org_id,
    )
    if current.status not in {
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.DELIVERED,
    }:
        raise PermanentJobError(
            "The WhatsApp execution needs manual reconciliation.",
            code="whatsapp_execution_outcome_unknown",
        )
    return _secure_result(message, replayed=False)


def _execution_error(
    code: str,
    detail: str,
    status_code: int = 409,
) -> ExecutionSafetyError:
    return ExecutionSafetyError(code=code, detail=detail, status_code=status_code)


def _safe_provider_error_code(value: str) -> str:
    code = str(value or "").strip().lower()
    if SAFE_PROVIDER_ERROR_CODE.fullmatch(code):
        return code
    return "whatsapp_provider_error"


def _validate_execution_binding(
    *,
    message: WhatsAppMessage,
    request: ExternalExecutionRequest | None,
) -> None:
    if request is None:
        raise _execution_error(
            "whatsapp_execution_request_not_found",
            "The approved WhatsApp execution request was not found.",
            404,
        )
    if (
        request.org_id != message.org_id
        or request.channel != ExecutionChannel.WHATSAPP
        or request.action != WHATSAPP_SEND_ACTION
        or request.idempotency_key != message.id
    ):
        raise _execution_error(
            "whatsapp_execution_scope_mismatch",
            "The execution request does not belong to this WhatsApp message.",
        )
    linked_request_id = getattr(message, "execution_request_id", None)
    if linked_request_id is not None and linked_request_id != request.id:
        raise _execution_error(
            "whatsapp_execution_scope_mismatch",
            "The WhatsApp message is bound to another execution request.",
        )


def _release_approved_execution(
    *,
    message: WhatsAppMessage,
    request: ExternalExecutionRequest,
    code: str,
    detail: str,
    cause: Exception | None = None,
) -> None:
    release_error = None
    try:
        release_execution(
            org=message.org,
            request_id=request.id,
            error_code=code,
            expected_status=ExternalRequestStatus.RESERVED,
        )
    except Exception as exc:
        release_error = exc
    _mark_failed(message, code=code, error=detail)
    error = PermanentJobError(detail, code=code)
    if cause is not None or release_error is not None:
        raise error from (cause or release_error)
    raise error


def _settle_unknown(
    *,
    message: WhatsAppMessage,
    request: ExternalExecutionRequest,
    error: Exception,
) -> None:
    current = ExternalExecutionRequest.objects.filter(
        id=request.id,
        org_id=message.org_id,
    ).first()
    if current is not None and current.status == ExternalRequestStatus.SENDING:
        try:
            mark_provider_accepted(
                org=message.org,
                request_id=request.id,
                local_state_uncertain=True,
            )
        except Exception:
            # The stale-SENDING reconciler will conservatively charge this call.
            pass
    current = ExternalExecutionRequest.objects.filter(
        id=request.id,
        org_id=message.org_id,
    ).first()
    if current is None or current.status not in {
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.DELIVERED,
    }:
        _mark_unknown(
            message,
            code="whatsapp_execution_outcome_unknown",
            error="The WhatsApp provider outcome requires manual reconciliation.",
        )
    raise PermanentJobError(
        "The WhatsApp provider outcome requires manual reconciliation.",
        code="whatsapp_execution_outcome_unknown",
    ) from error


def _mark_sent(
    message: WhatsAppMessage,
    *,
    provider_message_id: str,
    sent_at,
) -> None:
    updated = WhatsAppMessage.objects.filter(
        id=message.id,
        org_id=message.org_id,
        status__in={
            WhatsAppMessageStatus.PENDING,
            WhatsAppMessageStatus.QUEUED,
            WhatsAppMessageStatus.SENDING,
        },
    ).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id=provider_message_id,
        sent_at=sent_at,
        error_code="",
        error_message="",
        provider_status_snapshot={"accepted": True},
    )
    if not updated:
        current = WhatsAppMessage.objects.get(id=message.id, org_id=message.org_id)
        if current.status not in {
            WhatsAppMessageStatus.SENT,
            WhatsAppMessageStatus.DELIVERED,
            WhatsAppMessageStatus.READ,
        }:
            raise RuntimeError("The local WhatsApp message could not be finalized.")
        message.status = current.status
        message.provider_message_id = current.provider_message_id
        message.sent_at = current.sent_at
    else:
        message.status = WhatsAppMessageStatus.SENT
        message.provider_message_id = provider_message_id
        message.sent_at = sent_at
    WhatsAppBusinessConnection.objects.filter(
        id=message.connection_id,
        org_id=message.org_id,
    ).update(last_message_sent_at=sent_at)


def _client(
    *,
    org=None,
    execution_request_id: UUID | None = None,
) -> WhatsAppCloudClient:
    return WhatsAppCloudClient(
        api_version=settings.META_GRAPH_API_VERSION,
        base_url=settings.META_GRAPH_API_BASE_URL,
        timeout=settings.META_GRAPH_API_TIMEOUT,
        org=org,
        execution_request_id=execution_request_id,
    )


def _mark_skipped(
    message: WhatsAppMessage,
    code: str,
    *,
    error: str = "The campaign run is no longer active.",
) -> None:
    WhatsAppMessage.objects.filter(id=message.id, org_id=message.org_id).update(
        status=WhatsAppMessageStatus.SKIPPED,
        error_code=code,
        error_message=error[:1000],
    )
    message.status = WhatsAppMessageStatus.SKIPPED
    message.error_code = code
    message.error_message = error[:1000]


def _mark_failed(message: WhatsAppMessage, *, code: str, error: str) -> None:
    failed_at = timezone.now()
    WhatsAppMessage.objects.filter(id=message.id, org_id=message.org_id).update(
        status=WhatsAppMessageStatus.FAILED,
        error_code=code[:80],
        error_message=error[:1000],
        failed_at=failed_at,
    )
    message.status = WhatsAppMessageStatus.FAILED
    message.error_code = code[:80]
    message.error_message = error[:1000]
    message.failed_at = failed_at


def _mark_unknown(message: WhatsAppMessage, *, code: str, error: str) -> None:
    failed_at = timezone.now()
    unknown_status = getattr(
        WhatsAppMessageStatus,
        "UNKNOWN",
        WhatsAppMessageStatus.FAILED,
    )
    updated = WhatsAppMessage.objects.filter(
        id=message.id,
        org_id=message.org_id,
        status__in={
            WhatsAppMessageStatus.PENDING,
            WhatsAppMessageStatus.QUEUED,
            WhatsAppMessageStatus.SENDING,
        },
    ).update(
        status=unknown_status,
        error_code=code[:80],
        error_message=error[:1000],
        failed_at=failed_at,
    )
    if updated:
        message.status = unknown_status
        message.error_code = code[:80]
        message.error_message = error[:1000]
        message.failed_at = failed_at
    else:
        current = WhatsAppMessage.objects.get(
            id=message.id,
            org_id=message.org_id,
        )
        message.status = current.status
        message.error_code = current.error_code
        message.error_message = current.error_message
        message.failed_at = current.failed_at


def _result(message: WhatsAppMessage, *, replayed: bool) -> dict[str, Any]:
    return {
        "message_id": str(message.id),
        "campaign_id": str(message.campaign_id),
        "prospect_id": str(message.prospect_id),
        "status": message.status,
        "provider_message_id": message.provider_message_id,
        "replayed": replayed,
    }


def _secure_result(message: WhatsAppMessage, *, replayed: bool) -> dict[str, Any]:
    """Return only non-PII identifiers safe for the shared automation ledger."""

    return {
        "message_id": str(message.id),
        "campaign_id": str(message.campaign_id),
        "prospect_id": str(message.prospect_id),
        "status": message.status,
        "replayed": replayed,
    }


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch WhatsApp message job %s", job.id)
