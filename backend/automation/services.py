"""Transactional lifecycle services for durable background jobs."""

from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from typing import Any, Mapping
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.jobs import JobRequest
from automation.models import (
    AutomationAttemptStatus,
    AutomationJob,
    AutomationJobAttempt,
    AutomationJobStatus,
)


class AutomationJobStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    job: AutomationJob
    created: bool
    terminal_replay: bool


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job: AutomationJob
    attempt: AutomationJobAttempt


@dataclass(frozen=True, slots=True)
class FailedJob:
    job: AutomationJob
    retry_delay_seconds: int | None


TERMINAL_STATUSES = (
    AutomationJobStatus.SUCCEEDED,
    AutomationJobStatus.DEAD_LETTER,
    AutomationJobStatus.CANCELLED,
)
DISPATCHABLE_STATUSES = (
    AutomationJobStatus.PENDING,
    AutomationJobStatus.RETRY_SCHEDULED,
)


def enqueue_job(request: JobRequest) -> EnqueuedJob:
    """Persist an idempotent job before any broker publication is attempted."""

    scheduled_for = request.scheduled_for or timezone.now()
    with transaction.atomic():
        job, created = AutomationJob.objects.get_or_create(
            org_id=request.org_id,
            name=request.name,
            idempotency_key=request.idempotency_key,
            defaults={
                "payload": dict(request.payload),
                "queue": request.queue,
                "max_attempts": request.max_attempts,
                "scheduled_for": scheduled_for,
            },
        )
    return EnqueuedJob(
        job=job,
        created=created,
        terminal_replay=not created and job.status in TERMINAL_STATUSES,
    )


def dispatch_job(job: AutomationJob) -> bool:
    """Publish a persisted job, reverting to recoverable state if the broker fails."""

    now = timezone.now()
    countdown = max(0, ceil((job.scheduled_for - now).total_seconds()))
    previous_status = job.status
    updated = AutomationJob.objects.filter(
        id=job.id,
        org_id=job.org_id,
        status__in=DISPATCHABLE_STATUSES,
    ).update(status=AutomationJobStatus.QUEUED, queued_at=now)
    if not updated:
        return False

    from automation.tasks import run_automation_job

    try:
        run_automation_job.apply_async(
            args=[str(job.id), str(job.org_id)],
            queue=job.queue,
            countdown=countdown,
        )
    except Exception:
        AutomationJob.objects.filter(
            id=job.id,
            org_id=job.org_id,
            status=AutomationJobStatus.QUEUED,
        ).update(
            status=previous_status,
            last_error_code="broker_unavailable",
            last_error_message="The job broker was unavailable during dispatch.",
        )
        raise
    job.status = AutomationJobStatus.QUEUED
    job.queued_at = now
    return True


def claim_job(*, job_id: UUID, org_id: UUID) -> ClaimedJob | None:
    now = timezone.now()
    with transaction.atomic():
        try:
            job = AutomationJob.objects.select_for_update().get(
                id=job_id,
                org_id=org_id,
            )
        except AutomationJob.DoesNotExist:
            return None
        if job.status not in (
            AutomationJobStatus.PENDING,
            AutomationJobStatus.QUEUED,
            AutomationJobStatus.RETRY_SCHEDULED,
        ):
            return None
        if job.scheduled_for > now:
            return None
        if job.attempt_count >= job.max_attempts:
            job.status = AutomationJobStatus.DEAD_LETTER
            job.completed_at = now
            job.last_error_code = "attempts_exhausted"
            job.last_error_message = "The job exhausted its configured attempts."
            job.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "last_error_code",
                    "last_error_message",
                    "updated_at",
                ]
            )
            return None

        job.attempt_count += 1
        job.status = AutomationJobStatus.RUNNING
        job.started_at = now
        job.completed_at = None
        job.save(
            update_fields=[
                "attempt_count",
                "status",
                "started_at",
                "completed_at",
                "updated_at",
            ]
        )
        attempt = AutomationJobAttempt.objects.create(
            org_id=org_id,
            job=job,
            attempt_number=job.attempt_count,
            started_at=now,
        )
        return ClaimedJob(job=job, attempt=attempt)


def complete_job(
    *, claimed: ClaimedJob, result: Mapping[str, Any] | None
) -> AutomationJob:
    now = timezone.now()
    with transaction.atomic():
        job = AutomationJob.objects.select_for_update().get(
            id=claimed.job.id,
            org_id=claimed.job.org_id,
        )
        if job.status != AutomationJobStatus.RUNNING:
            raise AutomationJobStateError("Only a running job can be completed")
        AutomationJobAttempt.objects.filter(
            id=claimed.attempt.id,
            org_id=job.org_id,
            status=AutomationAttemptStatus.RUNNING,
        ).update(status=AutomationAttemptStatus.SUCCEEDED, finished_at=now)
        job.status = AutomationJobStatus.SUCCEEDED
        job.result = dict(result or {})
        job.completed_at = now
        job.last_error_code = ""
        job.last_error_message = ""
        job.save(
            update_fields=[
                "status",
                "result",
                "completed_at",
                "last_error_code",
                "last_error_message",
                "updated_at",
            ]
        )
        return job


def fail_job(
    *,
    claimed: ClaimedJob,
    error_code: str,
    error_message: str,
    retryable: bool,
) -> FailedJob:
    now = timezone.now()
    safe_code = error_code.strip()[:80] or "job_failed"
    safe_message = error_message.strip()[:2000] or "The job failed."
    with transaction.atomic():
        job = AutomationJob.objects.select_for_update().get(
            id=claimed.job.id,
            org_id=claimed.job.org_id,
        )
        if job.status != AutomationJobStatus.RUNNING:
            raise AutomationJobStateError("Only a running job can fail")
        AutomationJobAttempt.objects.filter(
            id=claimed.attempt.id,
            org_id=job.org_id,
            status=AutomationAttemptStatus.RUNNING,
        ).update(
            status=AutomationAttemptStatus.FAILED,
            finished_at=now,
            error_code=safe_code,
            error_message=safe_message,
        )

        should_retry = retryable and job.attempt_count < job.max_attempts
        delay = _retry_delay(job.attempt_count) if should_retry else None
        job.status = (
            AutomationJobStatus.RETRY_SCHEDULED
            if should_retry
            else AutomationJobStatus.DEAD_LETTER
        )
        job.scheduled_for = now + timedelta(seconds=delay or 0)
        job.completed_at = None if should_retry else now
        job.last_error_code = safe_code
        job.last_error_message = safe_message
        job.save(
            update_fields=[
                "status",
                "scheduled_for",
                "completed_at",
                "last_error_code",
                "last_error_message",
                "updated_at",
            ]
        )
        return FailedJob(job=job, retry_delay_seconds=delay)


def replay_dead_letter(*, job_id: UUID, org_id: UUID) -> AutomationJob:
    with transaction.atomic():
        job = AutomationJob.objects.select_for_update().get(
            id=job_id,
            org_id=org_id,
        )
        if job.status != AutomationJobStatus.DEAD_LETTER:
            raise AutomationJobStateError("Only a dead-letter job can be replayed")
        job.status = AutomationJobStatus.PENDING
        job.max_attempts += settings.AUTOMATION_MANUAL_RETRY_ATTEMPTS
        job.replay_count += 1
        job.scheduled_for = timezone.now()
        job.queued_at = None
        job.started_at = None
        job.completed_at = None
        job.save(
            update_fields=[
                "status",
                "max_attempts",
                "replay_count",
                "scheduled_for",
                "queued_at",
                "started_at",
                "completed_at",
                "updated_at",
            ]
        )
        return job


def recover_stale_jobs(*, org_id: UUID) -> int:
    """Return lost broker messages and abandoned leases to retryable state."""

    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.AUTOMATION_JOB_LEASE_SECONDS)
    recovered = AutomationJob.objects.filter(
        org_id=org_id,
        status=AutomationJobStatus.QUEUED,
        queued_at__lte=cutoff,
        scheduled_for__lte=now,
    ).update(
        status=AutomationJobStatus.PENDING,
        scheduled_for=now,
        last_error_code="dispatch_lease_expired",
        last_error_message="The queued job was not claimed before its lease expired.",
    )

    stale_running = list(
        AutomationJob.objects.filter(
            org_id=org_id,
            status=AutomationJobStatus.RUNNING,
            started_at__lte=cutoff,
        ).select_related("org")
    )
    for job in stale_running:
        attempt = job.attempts.filter(
            attempt_number=job.attempt_count,
            status=AutomationAttemptStatus.RUNNING,
        ).first()
        if attempt is None:
            continue
        try:
            fail_job(
                claimed=ClaimedJob(job=job, attempt=attempt),
                error_code="execution_lease_expired",
                error_message=(
                    "The worker did not finish before its execution lease expired."
                ),
                retryable=True,
            )
        except AutomationJobStateError:
            continue
        else:
            recovered += 1
    return recovered


def _retry_delay(attempt_count: int) -> int:
    base = settings.AUTOMATION_RETRY_BASE_SECONDS
    maximum = settings.AUTOMATION_RETRY_MAX_SECONDS
    return min(maximum, base * (2 ** max(0, attempt_count - 1)))
