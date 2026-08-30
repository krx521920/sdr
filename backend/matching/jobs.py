"""Durable automation handlers for asynchronous matching runs."""

from collections.abc import Mapping
from uuid import UUID

from django.db import OperationalError
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from matching.import_pipeline import PersonImportServiceError, execute_person_import
from matching.models import (
    MatchRun,
    PersonImportBatch,
    PersonImportBatchStatus,
)
from matching.services import (
    RecomputeSnapshotChanged,
    RecomputeTargetNotFound,
    execute_opportunity_recompute,
)


def _uuid(payload: Mapping, key: str) -> UUID:
    try:
        return UUID(str(payload[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The matching recompute payload is invalid.",
            code="invalid_job_payload",
        ) from exc


def process_recompute_opportunity_job(payload: Mapping):
    """Validate a persisted payload and execute one tenant-scoped ranking run."""

    if payload.get("schema_version") != 1:
        raise PermanentJobError(
            "The matching recompute payload version is not supported.",
            code="unsupported_job_payload",
        )
    org_id = _uuid(payload, "org_id")
    run_id = _uuid(payload, "run_id")
    opportunity_id = _uuid(payload, "opportunity_id")
    request_hash = str(payload.get("request_hash") or "").strip()
    if len(request_hash) != 64:
        raise PermanentJobError(
            "The matching recompute payload is invalid.",
            code="invalid_job_payload",
        )
    MatchRun.objects.filter(
        org_id=org_id,
        id=run_id,
        opportunity_id=opportunity_id,
        request_hash=request_hash,
        started_at__isnull=True,
        completed_at__isnull=True,
    ).update(started_at=timezone.now())
    try:
        return execute_opportunity_recompute(
            org_id=org_id,
            opportunity_id=opportunity_id,
            run_id=run_id,
            request_hash=request_hash,
        )
    except RecomputeTargetNotFound as exc:
        raise PermanentJobError(str(exc), code="matching_target_not_found") from exc
    except RecomputeSnapshotChanged as exc:
        raise PermanentJobError(
            str(exc),
            code="matching_candidate_snapshot_changed",
        ) from exc
    except OperationalError as exc:
        raise RetryableJobError(
            "The matching database is temporarily unavailable.",
            code="matching_database_unavailable",
        ) from exc


def process_person_import_job(payload: Mapping):
    """Validate and execute one durable, tenant-scoped Person import batch."""

    if payload.get("schema_version") != 1:
        raise PermanentJobError(
            "The matching import payload version is not supported.",
            code="unsupported_job_payload",
        )
    org_id = _uuid(payload, "org_id")
    batch_id = _uuid(payload, "batch_id")
    request_hash = str(payload.get("request_hash") or "").strip()
    if len(request_hash) != 64:
        raise PermanentJobError(
            "The matching import payload is invalid.",
            code="invalid_job_payload",
        )
    if not PersonImportBatch.objects.filter(
        org_id=org_id,
        id=batch_id,
        request_hash=request_hash,
    ).exists():
        raise PermanentJobError(
            "The matching import batch no longer exists.",
            code="matching_import_not_found",
        )
    try:
        return execute_person_import(
            org_id=org_id,
            batch_id=batch_id,
            request_hash=request_hash,
        )
    except PersonImportServiceError as exc:
        PersonImportBatch.objects.filter(org_id=org_id, id=batch_id).update(
            status=PersonImportBatchStatus.FAILED,
            error_code=exc.code[:80],
            completed_at=timezone.now(),
        )
        raise PermanentJobError(str(exc), code=exc.code) from exc
    except OperationalError as exc:
        raise RetryableJobError(
            "The matching database is temporarily unavailable.",
            code="matching_database_unavailable",
        ) from exc
