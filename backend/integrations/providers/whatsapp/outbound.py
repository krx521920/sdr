"""Durable WhatsApp template delivery for SDR outbound campaigns."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from integrations.models import (
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
)
from integrations.providers.whatsapp.client import (
    WhatsAppCloudAPIError,
    WhatsAppCloudClient,
)
from sdr.compliance import evaluate_contact
from sdr.models import (
    OutboundCampaignStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
)

logger = logging.getLogger(__name__)

WHATSAPP_MESSAGE_JOB = "whatsapp.send_campaign_message"
PHONE_SEPARATORS = re.compile(r"[\s()+.\-]")


class WhatsAppCampaignUnavailable(ValueError):
    pass


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
        .select_related("connection__route", "campaign", "prospect")
        .first()
    )
    if message is None:
        raise PermanentJobError(
            "The WhatsApp message no longer exists.",
            code="whatsapp_message_not_found",
        )
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
        _mark_failed(message, code=exc.error_code, error=str(exc))
        error_type = RetryableJobError if exc.retryable else PermanentJobError
        raise error_type(str(exc), code=exc.error_code) from exc

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


def _client() -> WhatsAppCloudClient:
    return WhatsAppCloudClient(
        api_version=settings.META_GRAPH_API_VERSION,
        base_url=settings.META_GRAPH_API_BASE_URL,
        timeout=settings.META_GRAPH_API_TIMEOUT,
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


def _result(message: WhatsAppMessage, *, replayed: bool) -> dict[str, Any]:
    return {
        "message_id": str(message.id),
        "campaign_id": str(message.campaign_id),
        "prospect_id": str(message.prospect_id),
        "status": message.status,
        "provider_message_id": message.provider_message_id,
        "replayed": replayed,
    }


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch WhatsApp message job %s", job.id)
