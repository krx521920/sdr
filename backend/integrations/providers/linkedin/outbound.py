"""Durable LinkedIn connection invitations for SDR campaigns."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from integrations.models import (
    LinkedInConnection,
    LinkedInInvitation,
    LinkedInInvitationStatus,
)
from integrations.providers.linkedin.client import (
    LinkedInInvitationsAPIError,
    LinkedInInvitationsClient,
)
from sdr.compliance import evaluate_contact
from sdr.linkedin_copy import (
    LinkedInInvitationTemplateError,
    render_invitation_message,
)
from sdr.models import (
    OutboundCampaignStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
)

logger = logging.getLogger(__name__)

LINKEDIN_INVITATION_JOB = "linkedin.send_campaign_invitation"


class LinkedInCampaignUnavailable(ValueError):
    pass


def active_linkedin_connection(*, org_id: UUID) -> LinkedInConnection | None:
    return LinkedInConnection.objects.filter(
        org_id=org_id,
        is_active=True,
        partner_access_confirmed=True,
    ).exclude(access_token_ciphertext="").first()


def enqueue_linkedin_campaign_invitation(
    *,
    prospect: SDROutboundProspect,
    campaign: SDROutboundCampaign,
    campaign_run: int,
    force_retry: bool = False,
) -> LinkedInInvitation:
    if campaign_run < 1 or campaign.run_count != campaign_run:
        raise LinkedInCampaignUnavailable("The LinkedIn campaign run is stale.")
    connection = active_linkedin_connection(org_id=campaign.org_id)
    if connection is None:
        raise LinkedInCampaignUnavailable(
            "Configure an enabled LinkedIn connection with approved partner API access first."
        )
    recipient = prospect.email.strip().lower()
    try:
        validate_email(recipient)
    except ValidationError as exc:
        raise LinkedInCampaignUnavailable(
            "A valid prospect email is required for an official LinkedIn invitation."
        ) from exc
    try:
        message_body = render_invitation_message(
            campaign.linkedin_invitation_message,
            prospect,
        )
    except LinkedInInvitationTemplateError as exc:
        raise LinkedInCampaignUnavailable(str(exc)) from exc
    compliance = evaluate_contact(
        org_id=campaign.org_id,
        channel="linkedin",
        identifier=recipient,
        country_code=prospect.country,
        prospect=prospect,
        event_key=f"linkedin:{campaign.id}:{campaign_run}:{prospect.id}",
    )
    if compliance.allowed and prospect.linkedin_url:
        compliance = evaluate_contact(
            org_id=campaign.org_id,
            channel="linkedin",
            identifier=prospect.linkedin_url,
            country_code=prospect.country,
            prospect=prospect,
            event_key=f"linkedin:{campaign.id}:{campaign_run}:{prospect.id}:profile",
        )

    with transaction.atomic():
        invitation, _ = LinkedInInvitation.objects.get_or_create(
            org_id=campaign.org_id,
            campaign=campaign,
            prospect=prospect,
            campaign_run=campaign_run,
            defaults={
                "connection": connection,
                "recipient": recipient,
                "message_body": message_body,
            },
        )
        if invitation.status == LinkedInInvitationStatus.SENT:
            return invitation
        if not compliance.allowed:
            _mark_skipped(
                invitation,
                compliance.code,
                error=compliance.reason,
            )
            return invitation
        retry_suffix = (
            f":retry:{invitation.attempt_count + 1}"
            if force_retry and invitation.status == LinkedInInvitationStatus.FAILED
            else ""
        )
        enqueued = enqueue_job(
            JobRequest(
                org_id=campaign.org_id,
                name=LINKEDIN_INVITATION_JOB,
                idempotency_key=f"linkedin-invitation:{invitation.id}{retry_suffix}",
                payload={
                    "org_id": str(campaign.org_id),
                    "invitation_id": str(invitation.id),
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
            raise LinkedInCampaignUnavailable(
                "The LinkedIn invitation job was cancelled and cannot be replayed."
            )
        if not terminal_replay:
            LinkedInInvitation.objects.filter(
                id=invitation.id,
                org_id=campaign.org_id,
            ).update(status=LinkedInInvitationStatus.QUEUED)
            invitation.status = LinkedInInvitationStatus.QUEUED
            transaction.on_commit(lambda: _safe_dispatch(job))
    return invitation


def retry_failed_linkedin_invitations(campaign: SDROutboundCampaign) -> int:
    queued = 0
    invitations = LinkedInInvitation.objects.filter(
        org_id=campaign.org_id,
        campaign=campaign,
        campaign_run=campaign.run_count,
        status=LinkedInInvitationStatus.FAILED,
    ).select_related("prospect")
    for invitation in invitations.iterator(chunk_size=100):
        enqueue_linkedin_campaign_invitation(
            prospect=invitation.prospect,
            campaign=campaign,
            campaign_run=campaign.run_count,
            force_retry=True,
        )
        queued += 1
    return queued


def process_linkedin_invitation_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        invitation_id = UUID(str(payload["invitation_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The LinkedIn invitation job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    invitation = (
        LinkedInInvitation.objects.filter(id=invitation_id, org_id=org_id)
        .select_related("connection", "campaign", "prospect")
        .first()
    )
    if invitation is None:
        raise PermanentJobError(
            "The LinkedIn invitation no longer exists.",
            code="linkedin_invitation_not_found",
        )
    if invitation.status in {
        LinkedInInvitationStatus.SENT,
        LinkedInInvitationStatus.SKIPPED,
    }:
        return _result(invitation, replayed=True)
    campaign = invitation.campaign
    if (
        campaign.status != OutboundCampaignStatus.ACTIVE
        or campaign.run_count != invitation.campaign_run
        or "linkedin" not in campaign.channels
    ):
        _mark_skipped(invitation, "campaign_not_active")
        return _result(invitation, replayed=False)
    connection = invitation.connection
    if not connection.is_active or not connection.partner_access_confirmed:
        _mark_failed(
            invitation,
            code="linkedin_connection_inactive",
            error="The LinkedIn partner connection is inactive.",
        )
        raise PermanentJobError(
            "The LinkedIn partner connection is inactive.",
            code="linkedin_connection_inactive",
        )
    compliance = evaluate_contact(
        org_id=org_id,
        channel="linkedin",
        identifier=invitation.recipient,
        country_code=invitation.prospect.country,
        prospect=invitation.prospect,
        event_key=f"linkedin:send:{invitation.id}",
    )
    if compliance.allowed and invitation.prospect.linkedin_url:
        compliance = evaluate_contact(
            org_id=org_id,
            channel="linkedin",
            identifier=invitation.prospect.linkedin_url,
            country_code=invitation.prospect.country,
            prospect=invitation.prospect,
            event_key=f"linkedin:send:{invitation.id}:profile",
        )
    if not compliance.allowed:
        _mark_skipped(invitation, compliance.code, error=compliance.reason)
        return _result(invitation, replayed=False)

    LinkedInInvitation.objects.filter(id=invitation.id, org_id=org_id).update(
        status=LinkedInInvitationStatus.SENDING,
        attempt_count=invitation.attempt_count + 1,
        error_code="",
        error_message="",
        failed_at=None,
    )
    invitation.status = LinkedInInvitationStatus.SENDING
    invitation.attempt_count += 1
    try:
        response = _client().send_email_invitation(
            access_token=connection.get_access_token(),
            recipient_email=invitation.recipient,
            message_body=invitation.message_body,
        )
    except LinkedInInvitationsAPIError as exc:
        _mark_failed(invitation, code=exc.error_code, error=str(exc))
        error_type = RetryableJobError if exc.retryable else PermanentJobError
        raise error_type(str(exc), code=exc.error_code) from exc

    sent_at = timezone.now()
    LinkedInInvitation.objects.filter(id=invitation.id, org_id=org_id).update(
        status=LinkedInInvitationStatus.SENT,
        provider_invitation_id=response.invitation_id,
        sent_at=sent_at,
        error_code="",
        error_message="",
        provider_status_snapshot=dict(response.snapshot),
    )
    LinkedInConnection.objects.filter(id=connection.id, org_id=org_id).update(
        last_invitation_sent_at=sent_at
    )
    invitation.status = LinkedInInvitationStatus.SENT
    invitation.provider_invitation_id = response.invitation_id
    invitation.sent_at = sent_at
    return _result(invitation, replayed=False)


def _client() -> LinkedInInvitationsClient:
    return LinkedInInvitationsClient(
        base_url=settings.LINKEDIN_API_BASE_URL,
        timeout=settings.LINKEDIN_API_TIMEOUT,
    )


def _mark_skipped(
    invitation: LinkedInInvitation,
    code: str,
    *,
    error: str = "The campaign run is no longer active.",
) -> None:
    LinkedInInvitation.objects.filter(
        id=invitation.id,
        org_id=invitation.org_id,
    ).update(
        status=LinkedInInvitationStatus.SKIPPED,
        error_code=code,
        error_message=error[:1000],
    )
    invitation.status = LinkedInInvitationStatus.SKIPPED
    invitation.error_code = code
    invitation.error_message = error[:1000]


def _mark_failed(invitation: LinkedInInvitation, *, code: str, error: str) -> None:
    failed_at = timezone.now()
    LinkedInInvitation.objects.filter(
        id=invitation.id,
        org_id=invitation.org_id,
    ).update(
        status=LinkedInInvitationStatus.FAILED,
        error_code=code[:80],
        error_message=error[:1000],
        failed_at=failed_at,
    )
    invitation.status = LinkedInInvitationStatus.FAILED
    invitation.error_code = code[:80]
    invitation.error_message = error[:1000]
    invitation.failed_at = failed_at


def _result(invitation: LinkedInInvitation, *, replayed: bool) -> dict[str, Any]:
    return {
        "invitation_id": str(invitation.id),
        "campaign_id": str(invitation.campaign_id),
        "prospect_id": str(invitation.prospect_id),
        "status": invitation.status,
        "provider_invitation_id": invitation.provider_invitation_id,
        "replayed": replayed,
    }


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch LinkedIn invitation job %s", job.id)
