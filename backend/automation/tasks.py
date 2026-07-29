"""Celery entry points backed by the durable automation job ledger."""

import logging
from collections.abc import Mapping
from uuid import UUID

from celery import shared_task
from django.utils import timezone

from automation.errors import AutomationJobError
from automation.models import AutomationJob, AutomationJobStatus
from automation.registry import get_job_handler
from automation.services import (
    claim_job,
    complete_job,
    dispatch_job,
    fail_job,
    recover_stale_jobs,
)
from automation.tenant_context import database_org_context
from common.models import Org

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="automation.run_job")
def run_automation_job(self, job_id: str, org_id: str):
    parsed_job_id = UUID(job_id)
    parsed_org_id = UUID(org_id)
    with database_org_context(parsed_org_id):
        claimed = claim_job(job_id=parsed_job_id, org_id=parsed_org_id)
        if claimed is None:
            return {"job_id": job_id, "status": "skipped"}

        try:
            handler = get_job_handler(claimed.job.name)
            result = handler(claimed.job.payload)
            if result is not None and not isinstance(result, Mapping):
                raise AutomationJobError(
                    "The job handler returned an invalid result.",
                    code="invalid_handler_result",
                    retryable=False,
                )
        except AutomationJobError as exc:
            error_code = exc.code
            retryable = exc.retryable
            error_message = str(exc)
        except Exception as exc:
            logger.exception("Unhandled automation job failure: %s", claimed.job.name)
            error_code = "unhandled_failure"
            retryable = True
            error_message = str(exc) or exc.__class__.__name__
        else:
            job = complete_job(claimed=claimed, result=result)
            return {"job_id": job_id, "status": job.status}

        failed = fail_job(
            claimed=claimed,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
        )
        if failed.retry_delay_seconds is not None:
            try:
                dispatch_job(failed.job)
            except Exception:
                logger.exception(
                    "Could not dispatch retry for automation job %s", job_id
                )
        return {"job_id": job_id, "status": failed.job.status}


@shared_task(name="automation.dispatch_due_jobs")
def dispatch_due_jobs():
    """Recover abandoned work and publish all tenant jobs that are due."""

    dispatched = 0
    recovered = 0
    now = timezone.now()
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            recovered += recover_stale_jobs(org_id=org_id)
            jobs = list(
                AutomationJob.objects.filter(
                    org_id=org_id,
                    status__in=(
                        AutomationJobStatus.PENDING,
                        AutomationJobStatus.RETRY_SCHEDULED,
                    ),
                    scheduled_for__lte=now,
                ).order_by("scheduled_for")[:100]
            )
            for job in jobs:
                try:
                    if dispatch_job(job):
                        dispatched += 1
                except Exception:
                    logger.exception("Could not dispatch due automation job %s", job.id)
    return {"dispatched": dispatched, "recovered": recovered}
