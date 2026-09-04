"""Human-reviewed AI copy drafts for SDR outbound campaigns."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from sdr.intelligence.contracts import ModelProviderError
from sdr.intelligence.gateway import (
    ModelGatewayError,
    UnifiedAIGateway,
    _configuration_routes,
)
from sdr.intelligence.outbound_copy_contracts import (
    OUTBOUND_COPY_PROMPT_VERSION,
    OUTBOUND_COPY_SCHEMA,
    OutboundCopyProviderError,
    OutboundCopyResult,
    validate_generated_steps,
)
from sdr.intelligence.safety import AISafetyError
from sdr.models import (
    LeadIntakeSource,
    OutboundCampaignStatus,
    OutboundCopyDraftStatus,
    SDRIntelligenceSettings,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCopyDraft,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OUTBOUND_COPY_PROMPT_VERSION",
    "OUTBOUND_COPY_SCHEMA",
    "OutboundCopyProviderError",
    "OutboundCopyResult",
    "validate_generated_steps",
]

OUTBOUND_COPY_JOB = "sdr.generate_outbound_copy"


class OutboundCopyUnavailable(ValueError):
    pass


class OutboundCopyGatewayError(ModelProviderError):
    def __init__(
        self,
        attempts: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        *,
        code: str = "",
        message: str = "",
    ):
        last = attempts[-1] if attempts else {}
        super().__init__(
            message or "Every configured model provider failed to generate copy.",
            code=code or str(last.get("error_code") or "outbound_copy_gateway_failed"),
            retryable=any(bool(item.get("retryable")) for item in attempts),
        )
        self.attempts = tuple(attempts)


class OutboundCopyGateway:
    def __init__(
        self,
        *,
        org_id: UUID,
        routes,
        configuration=None,
    ):
        self.org_id = org_id
        self.routes = tuple(routes)
        self.configuration = configuration

    @classmethod
    def for_configuration(cls, configuration):
        return cls(
            org_id=configuration.org_id,
            routes=_configuration_routes(configuration),
            configuration=configuration,
        )

    def generate(self, *, context: dict[str, Any]) -> OutboundCopyResult:
        try:
            execution = UnifiedAIGateway(
                org_id=self.org_id,
                routes=self.routes,
                configuration=self.configuration,
            ).execute(
                purpose="outbound_copy",
                prompt_version=OUTBOUND_COPY_PROMPT_VERSION,
                context=context,
            )
        except ModelGatewayError as exc:
            raise OutboundCopyGatewayError(exc.attempts) from exc
        except AISafetyError as exc:
            raise OutboundCopyGatewayError(
                code=exc.code,
                message=str(exc),
            ) from exc
        result = execution.result
        return OutboundCopyResult(
            steps=result.steps,
            response_id=result.response_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider=result.provider,
            model=result.model,
            attempts=execution.attempts,
        )


def enqueue_outbound_copy_generation(draft: SDROutboundCopyDraft):
    if draft.status not in {
        OutboundCopyDraftStatus.PENDING,
        OutboundCopyDraftStatus.FAILED,
    }:
        raise OutboundCopyUnavailable(
            "Only pending or failed copy drafts can be generated."
        )
    enqueued = enqueue_job(
        JobRequest(
            org_id=draft.org_id,
            name=OUTBOUND_COPY_JOB,
            idempotency_key=f"outbound-copy:{draft.id}",
            payload={"org_id": str(draft.org_id), "draft_id": str(draft.id)},
            max_attempts=3,
        )
    )
    job = enqueued.job
    terminal_replay = enqueued.terminal_replay
    if job.status == AutomationJobStatus.DEAD_LETTER:
        job = replay_dead_letter(job_id=job.id, org_id=draft.org_id)
        terminal_replay = False
    elif job.status == AutomationJobStatus.CANCELLED:
        raise OutboundCopyUnavailable("The copy generation job was cancelled.")
    SDROutboundCopyDraft.objects.filter(id=draft.id, org_id=draft.org_id).update(
        status=OutboundCopyDraftStatus.PENDING,
        last_job_id=job.id,
        error_code="",
        error_message="",
    )
    if not terminal_replay:
        transaction.on_commit(lambda: _safe_dispatch(job))
    return job


def process_outbound_copy_job(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        draft_id = UUID(str(payload["draft_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The outbound copy job payload is invalid.",
            code="invalid_job_payload",
        ) from exc
    draft = (
        SDROutboundCopyDraft.objects.filter(id=draft_id, org_id=org_id)
        .select_related("campaign")
        .first()
    )
    if draft is None:
        raise PermanentJobError(
            "The outbound copy draft no longer exists.",
            code="outbound_copy_draft_not_found",
        )
    if draft.status == OutboundCopyDraftStatus.APPLIED:
        return {"draft_id": str(draft.id), "status": "skipped", "reason": "applied"}
    configuration = SDRIntelligenceSettings.objects.filter(org_id=org_id).first()
    if configuration is None or not configuration.is_enabled:
        return _copy_failure(
            draft,
            code="ai_gateway_not_enabled",
            message="Enable SDR Intelligence before generating outbound copy.",
            retryable=False,
        )
    SDROutboundCopyDraft.objects.filter(id=draft.id, org_id=org_id).update(
        status=OutboundCopyDraftStatus.GENERATING,
        error_code="",
        error_message="",
    )
    gateway = OutboundCopyGateway.for_configuration(configuration)
    try:
        result = gateway.generate(context=_draft_context(draft))
    except OutboundCopyGatewayError as exc:
        SDROutboundCopyDraft.objects.filter(id=draft.id, org_id=org_id).update(
            status=OutboundCopyDraftStatus.FAILED,
            provider_attempts=list(exc.attempts),
            error_code=exc.code[:80],
            error_message=str(exc)[:1000],
        )
        error_type = RetryableJobError if exc.retryable else PermanentJobError
        raise error_type(str(exc), code=exc.code) from exc
    now = timezone.now()
    SDROutboundCopyDraft.objects.filter(id=draft.id, org_id=org_id).update(
        status=OutboundCopyDraftStatus.READY,
        generated_steps=list(result.steps),
        provider=result.provider,
        model=result.model,
        prompt_version=OUTBOUND_COPY_PROMPT_VERSION,
        provider_response_id=result.response_id,
        provider_attempts=list(result.attempts),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        generated_at=now,
        error_code="",
        error_message="",
    )
    return {
        "draft_id": str(draft.id),
        "status": OutboundCopyDraftStatus.READY,
        "provider": result.provider,
        "model": result.model,
        "step_count": len(result.steps),
    }


@transaction.atomic
def apply_outbound_copy_draft(
    draft: SDROutboundCopyDraft,
    *,
    reviewer,
) -> SDRNurtureSequence:
    draft = (
        SDROutboundCopyDraft.objects.select_for_update()
        .select_related("campaign__sequence")
        .get(id=draft.id, org_id=draft.org_id)
    )
    if draft.status != OutboundCopyDraftStatus.READY:
        raise OutboundCopyUnavailable("Only a ready copy draft can be applied.")
    campaign = draft.campaign
    if campaign.status not in {
        OutboundCampaignStatus.DRAFT,
        OutboundCampaignStatus.PAUSED,
    }:
        raise OutboundCopyUnavailable(
            "Pause the campaign before applying generated outbound copy."
        )
    steps = validate_generated_steps(
        draft.generated_steps, expected_count=draft.step_count
    )
    sequence = campaign.sequence
    reusable = bool(
        sequence
        and not sequence.is_active
        and not sequence.enrollments.exists()
        and not sequence.outbound_campaigns.exclude(id=campaign.id).exists()
    )
    if not reusable:
        sequence = SDRNurtureSequence.objects.create(
            org_id=draft.org_id,
            name=f"{campaign.name} AI sequence"[:160],
            description=(
                f"Human-reviewed AI draft {draft.id}; provider {draft.provider}:{draft.model}."
            ),
            is_active=False,
            auto_enroll=False,
            sources=[LeadIntakeSource.OUTBOUND],
        )
        campaign.sequence = sequence
        campaign.save(update_fields=["sequence", "updated_at"])
    else:
        sequence.steps.all().delete()
    SDRNurtureStep.objects.bulk_create(
        [
            SDRNurtureStep(
                org_id=draft.org_id,
                sequence=sequence,
                position=step["position"],
                delay_minutes=step["delay_days"] * 1440,
                subject_a=step["subject_a"],
                body_a=step["body_a"],
                subject_b=step["subject_b"],
                body_b=step["body_b"],
                variant_b_percent=50,
            )
            for step in steps
        ]
    )
    now = timezone.now()
    draft.status = OutboundCopyDraftStatus.APPLIED
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    draft.applied_at = now
    draft.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "applied_at",
            "updated_at",
        ]
    )
    return sequence


def _draft_context(draft: SDROutboundCopyDraft) -> dict[str, Any]:
    prospects = list(
        draft.campaign.prospects.order_by("created_at").values(
            "job_title",
            "company_name",
            "industry",
            "country",
        )[:10]
    )
    audience = {
        "job_titles": sorted(
            {str(item["job_title"]).strip() for item in prospects if item["job_title"]}
        ),
        "company_names": sorted(
            {
                str(item["company_name"]).strip()
                for item in prospects
                if item["company_name"]
            }
        ),
        "industries": sorted(
            {str(item["industry"]).strip() for item in prospects if item["industry"]}
        ),
        "countries": sorted(
            {str(item["country"]).strip() for item in prospects if item["country"]}
        ),
        "prospect_count": draft.campaign.prospects.count(),
    }
    return {
        "request": {
            "language": draft.language,
            "tone": draft.tone,
            "step_count": draft.step_count,
            "offering_summary": draft.offering_summary,
            "value_proposition": draft.value_proposition,
            "proof_points": draft.proof_points,
            "cta_goal": draft.cta_goal,
        },
        "campaign": {
            "name": draft.campaign.name,
            "description": draft.campaign.description,
            "icp_description": draft.campaign.icp_description,
            "channels": draft.campaign.channels,
        },
        "audience": audience,
        "allowed_template_variables": [
            "first_name",
            "last_name",
            "company_name",
            "organization_name",
            "qualification_band",
            "qualification_score",
        ],
    }


def _copy_failure(
    draft: SDROutboundCopyDraft,
    *,
    code: str,
    message: str,
    retryable: bool,
):
    SDROutboundCopyDraft.objects.filter(id=draft.id, org_id=draft.org_id).update(
        status=OutboundCopyDraftStatus.FAILED,
        error_code=code[:80],
        error_message=message[:1000],
    )
    error_type = RetryableJobError if retryable else PermanentJobError
    raise error_type(message, code=code)


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch outbound copy job %s", job.id)
