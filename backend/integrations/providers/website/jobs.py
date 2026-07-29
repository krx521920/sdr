"""Durable job entry point for authenticated website lead submissions."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from integrations.providers.website.adapter import WebsiteFormNormalizer
from integrations.providers.website.service import process_website_intake
from sdr.models import LeadIntake, LeadLifecycleEventType
from sdr.response import record_lifecycle_event, schedule_acknowledgement_job
from sdr.services import (
    IntakeAlreadyProcessing,
    IntakeProcessingFailed,
    accept_candidate_intake,
)

logger = logging.getLogger(__name__)

WEBSITE_INTAKE_JOB = "sdr.process_intake"


@dataclass(frozen=True, slots=True)
class WebsiteIntakeAcceptance:
    intake_id: UUID
    job_id: UUID
    status: str
    lead_id: UUID | None
    replayed: bool


def enqueue_website_intake(
    *, org_id: UUID, payload: Mapping[str, Any]
) -> WebsiteIntakeAcceptance:
    candidate = WebsiteFormNormalizer().normalize(org_id=org_id, payload=payload)
    acceptance = accept_candidate_intake(candidate=candidate, raw_payload=payload)
    enqueued = enqueue_job(
        JobRequest(
            org_id=org_id,
            name=WEBSITE_INTAKE_JOB,
            idempotency_key=f"intake:{acceptance.intake_id}",
            payload={
                "org_id": str(org_id),
                "source": candidate.source.value,
                "payload": dict(payload),
            },
            max_attempts=6,
        )
    )
    intake = LeadIntake.objects.get(id=acceptance.intake_id, org_id=org_id)
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.QUEUED,
        event_key="queued:intake_processing",
        data={"job_id": str(enqueued.job.id)},
    )
    schedule_acknowledgement_job(intake)
    if not enqueued.terminal_replay:
        try:
            dispatch_job(enqueued.job)
        except Exception:
            logger.exception(
                "Website intake %s was persisted but broker dispatch failed",
                acceptance.intake_id,
            )
    return WebsiteIntakeAcceptance(
        intake_id=acceptance.intake_id,
        job_id=enqueued.job.id,
        status=acceptance.status,
        lead_id=acceptance.lead_id,
        replayed=acceptance.replayed,
    )


def process_website_intake_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        source = str(payload["source"])
        lead_payload = payload["payload"]
        if source != "website_form" or not isinstance(lead_payload, Mapping):
            raise TypeError
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The website intake job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    try:
        result = process_website_intake(org_id=org_id, payload=lead_payload)
    except IntakeAlreadyProcessing as exc:
        raise RetryableJobError(
            "The website lead is already being processed.",
            code="intake_already_processing",
        ) from exc
    except IntakeProcessingFailed as exc:
        raise RetryableJobError(
            "The SDR intake pipeline failed and retained the lead for retry.",
            code="intake_processing_failed",
        ) from exc
    return {
        "intake_id": str(result.intake_id),
        "lead_id": str(result.lead_id) if result.lead_id else None,
        "replayed": result.replayed,
    }
