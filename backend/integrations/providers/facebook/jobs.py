"""Durable job bridge between Meta webhooks and the shared SDR pipeline."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJob
from automation.services import dispatch_job, enqueue_job
from automation.tenant_context import database_org_context
from integrations.models import FacebookPageRoute
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.service import (
    FacebookConnectionUnavailable,
    process_facebook_lead_event,
)
from sdr.services import IntakeProcessingFailed

FACEBOOK_LEAD_JOB = "facebook.process_lead"


def enqueue_facebook_lead_event(
    event_payload: Mapping[str, Any],
) -> AutomationJob:
    page_id = str(event_payload.get("page_id", "")).strip()
    leadgen_id = str(event_payload.get("leadgen_id", "")).strip()
    if not page_id or not leadgen_id:
        raise FacebookConnectionUnavailable("Facebook lead event is incomplete")
    route = FacebookPageRoute.objects.filter(page_id=page_id).only("org_id").first()
    if route is None:
        raise FacebookConnectionUnavailable(
            "No organization is connected to this Facebook Page"
        )

    with database_org_context(route.org_id):
        enqueued = enqueue_job(
            JobRequest(
                org_id=route.org_id,
                name=FACEBOOK_LEAD_JOB,
                idempotency_key=f"leadgen:{leadgen_id}",
                payload={
                    "org_id": str(route.org_id),
                    "event": dict(event_payload),
                },
                max_attempts=6,
            )
        )
        if not enqueued.terminal_replay:
            dispatch_job(enqueued.job)
        return enqueued.job


def process_facebook_lead_job(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        expected_org_id = UUID(str(payload["org_id"]))
        event_payload = payload["event"]
        if not isinstance(event_payload, Mapping):
            raise TypeError
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "Facebook job payload is invalid",
            code="invalid_job_payload",
        ) from exc
    try:
        result = process_facebook_lead_event(
            event_payload=event_payload,
            expected_org_id=expected_org_id,
        )
    except FacebookGraphAPIError as exc:
        error_class = RetryableJobError if exc.retryable else PermanentJobError
        raise error_class(str(exc), code="meta_graph_error") from exc
    except IntakeProcessingFailed as exc:
        raise RetryableJobError(
            "The SDR intake pipeline failed and retained the lead for retry.",
            code="intake_processing_failed",
        ) from exc
    except FacebookConnectionUnavailable as exc:
        raise PermanentJobError(
            str(exc), code="facebook_connection_unavailable"
        ) from exc
    return {
        "intake_id": str(result.intake_id),
        "lead_id": str(result.lead_id) if result.lead_id else None,
        "replayed": result.replayed,
    }
