"""Recurring outbound prospect sources backed by durable automation jobs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid5

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from matching.import_pipeline import commit_person_import
from matching.models import PersonImportBatch, PersonImportBatchStatus
from matching.provider_import import (
    ProviderPersonRecord,
    preview_provider_person_import,
)
from sdr.models import (
    ApolloCandidateStatus,
    OutboundCampaignStatus,
    SDRApolloCandidate,
    SDROutboundSource,
)
from sdr.outbound import CSV_HEADERS, import_prospect_csv
from sdr.provider_ports import (
    APOLLO_ENRICH_ACTION,
    APOLLO_SEARCH_ACTION,
    ExecutionChannel,
    ExecutionSafetyError,
    ExternalExecutionRequestPort,
    ExternalRequestStatus,
    ProviderAdapterError,
    ProviderAdapterUnavailable,
    external_execution_request,
    hash_target_identifier,
    mark_execution_delivered,
    mark_execution_sending,
    mark_provider_accepted,
    prospect_source_adapter,
    reconcile_stale_reserved,
    reconcile_stale_sending,
    release_execution,
    reserve_execution,
)

logger = logging.getLogger(__name__)

OUTBOUND_SOURCE_SYNC_JOB = "sdr.sync_outbound_source"
APOLLO_CANDIDATE_ENRICH_JOB = "sdr.enrich_apollo_candidate"
APOLLO_PERSON_URL = "https://app.apollo.io/#/people/{person_id}"
APOLLO_IMPORT_COMMIT_NAMESPACE = UUID("cc6dbdf8-c4b0-4c72-8d7c-1f522d028082")
APOLLO_SEARCH_SAFE_ERROR = "Apollo search did not complete successfully."
APOLLO_ENRICH_SAFE_ERROR = "Apollo enrichment did not complete successfully."


def reconcile_apollo_candidate_states(
    *,
    org,
    reserved_before,
    sending_before,
    limit: int = 100,
) -> dict[str, int]:
    """Recover Apollo's durable state without making any provider call.

    RESERVED means provider I/O never began, so stale reservations are failed
    and refunded. SENDING may already have reached Apollo, so stale calls are
    charged and marked UNKNOWN for an administrator to resolve. Import state
    is projected separately from the canonical Person import batch ledger.
    """

    released_request_ids = reconcile_stale_reserved(
        org=org,
        older_than=reserved_before,
        limit=limit,
    )
    unknown_request_ids = reconcile_stale_sending(
        org=org,
        older_than=sending_before,
        limit=limit,
    )
    execution_counts = _reconcile_apollo_candidate_execution_states(
        org_id=org.id,
        limit=limit,
    )
    import_counts = _reconcile_apollo_candidate_import_states(
        org_id=org.id,
        limit=limit,
    )
    return {
        "released_requests": len(released_request_ids),
        "unknown_requests": len(unknown_request_ids),
        **execution_counts,
        **import_counts,
    }


def _reconcile_apollo_candidate_execution_states(
    *, org_id: UUID, limit: int
) -> dict[str, int]:
    """Repair candidate projections even after a prior process interruption."""

    pending_ids = list(
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            status=ApolloCandidateStatus.ENRICHMENT_RESERVED,
            enrichment_request__status=ExternalRequestStatus.FAILED,
        )
        .order_by("updated_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    pending = SDRApolloCandidate.objects.filter(
        org_id=org_id,
        id__in=pending_ids,
        status=ApolloCandidateStatus.ENRICHMENT_RESERVED,
        enrichment_request__status=ExternalRequestStatus.FAILED,
    ).update(
        status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL,
        updated_at=timezone.now(),
    )
    remaining = max(0, limit - pending)
    unknown_ids = list(
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            status=ApolloCandidateStatus.ENRICHMENT_RESERVED,
            enrichment_request__status=ExternalRequestStatus.UNKNOWN,
        )
        .order_by("updated_at", "id")
        .values_list("id", flat=True)[:remaining]
    )
    unknown = SDRApolloCandidate.objects.filter(
        org_id=org_id,
        id__in=unknown_ids,
        status=ApolloCandidateStatus.ENRICHMENT_RESERVED,
        enrichment_request__status=ExternalRequestStatus.UNKNOWN,
    ).update(
        status=ApolloCandidateStatus.UNKNOWN,
        updated_at=timezone.now(),
    )
    return {
        "candidates_released": pending,
        "candidates_unknown": unknown,
    }


def _reconcile_apollo_candidate_import_states(
    *, org_id: UUID, limit: int
) -> dict[str, int]:
    candidate_ids = list(
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            status=ApolloCandidateStatus.IMPORT_QUEUED,
        )
        .order_by("updated_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    counts = {
        "candidates_imported": 0,
        "candidates_import_review_required": 0,
        "candidates_import_failed": 0,
        "candidates_import_retry_required": 0,
    }
    for candidate_id in candidate_ids:
        next_status = _reconcile_one_apollo_candidate_import(
            org_id=org_id,
            candidate_id=candidate_id,
        )
        if next_status == ApolloCandidateStatus.IMPORTED:
            counts["candidates_imported"] += 1
        elif next_status == ApolloCandidateStatus.IMPORT_REVIEW_REQUIRED:
            counts["candidates_import_review_required"] += 1
        elif next_status == ApolloCandidateStatus.IMPORT_FAILED:
            counts["candidates_import_failed"] += 1
        elif next_status == ApolloCandidateStatus.IMPORT_RETRY_REQUIRED:
            counts["candidates_import_retry_required"] += 1
    return counts


@transaction.atomic
def _reconcile_one_apollo_candidate_import(
    *, org_id: UUID, candidate_id: UUID
) -> str | None:
    candidate = (
        SDRApolloCandidate.objects.select_for_update()
        .filter(
            org_id=org_id,
            id=candidate_id,
            status=ApolloCandidateStatus.IMPORT_QUEUED,
        )
        .first()
    )
    if candidate is None:
        return None
    if candidate.import_batch_id is None:
        next_status = ApolloCandidateStatus.IMPORT_RETRY_REQUIRED
    else:
        batch = (
            PersonImportBatch.objects.select_for_update()
            .filter(org_id=org_id, id=candidate.import_batch_id)
            .select_related("automation_job")
            .first()
        )
        if batch is None:
            next_status = ApolloCandidateStatus.IMPORT_RETRY_REQUIRED
        elif batch.status == PersonImportBatchStatus.COMPLETED:
            next_status = ApolloCandidateStatus.IMPORTED
        elif batch.status == PersonImportBatchStatus.PARTIAL:
            next_status = ApolloCandidateStatus.IMPORT_REVIEW_REQUIRED
        elif batch.status == PersonImportBatchStatus.FAILED:
            next_status = ApolloCandidateStatus.IMPORT_FAILED
        elif (
            batch.automation_job_id
            and batch.automation_job.status
            in {AutomationJobStatus.DEAD_LETTER, AutomationJobStatus.CANCELLED}
        ):
            next_status = ApolloCandidateStatus.IMPORT_RETRY_REQUIRED
        else:
            return None
    candidate.status = next_status
    candidate.save(update_fields=["status", "updated_at"])
    return next_status


class OutboundSourceUnavailable(ValueError):
    def __init__(self, message: str, *, code: str = "outbound_source_unavailable"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ApolloExecutionIntent:
    channel: str
    action: str
    target_hash: str
    payload_hash: str
    units: int = 1
    test_target_identifier: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "action": self.action,
            "target_hash": self.target_hash,
            "payload_hash": self.payload_hash,
            "units": self.units,
            **(
                {"test_target_identifier": self.test_target_identifier}
                if self.test_target_identifier
                else {}
            ),
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _apollo_search_payload(source: SDROutboundSource) -> dict[str, Any]:
    page = max(1, min(source.next_page, 500))
    per_page = min(100, max(25, source.max_results_per_sync * 4))
    return {
        "filters": source.search_filters,
        "page": page,
        "per_page": per_page,
    }


def apollo_search_execution_intent(source: SDROutboundSource) -> ApolloExecutionIntent:
    target_identifier = f"outbound-source:{source.id}"
    return ApolloExecutionIntent(
        channel=ExecutionChannel.APOLLO,
        action=APOLLO_SEARCH_ACTION,
        target_hash=hash_target_identifier(
            org=source.org,
            channel=ExecutionChannel.APOLLO,
            identifier=target_identifier,
        ),
        payload_hash=_payload_hash(_apollo_search_payload(source)),
        test_target_identifier=target_identifier,
    )


def apollo_enrichment_execution_intent(
    candidate: SDRApolloCandidate,
) -> ApolloExecutionIntent:
    provider_person_id = candidate.get_provider_person_id()
    # Approval and allowlisting use our non-sensitive local candidate id. The
    # exact provider person id remains encrypted and is bound only via payload_hash.
    target_identifier = f"apollo-candidate:{candidate.id}"
    return ApolloExecutionIntent(
        channel=ExecutionChannel.APOLLO,
        action=APOLLO_ENRICH_ACTION,
        target_hash=hash_target_identifier(
            org=candidate.org,
            channel=ExecutionChannel.APOLLO,
            identifier=target_identifier,
        ),
        payload_hash=_payload_hash({"person_id": provider_person_id}),
        test_target_identifier=target_identifier,
    )


def _execution_error(exc: ExecutionSafetyError) -> OutboundSourceUnavailable:
    return OutboundSourceUnavailable(exc.detail, code=exc.code)


def enqueue_outbound_source_sync(
    source: SDROutboundSource,
    *,
    manual: bool = False,
    approval_id: UUID | None = None,
    idempotency_key: UUID | None = None,
):
    if not source.enrichment_credits_acknowledged:
        raise OutboundSourceUnavailable(
            "Acknowledge Apollo enrichment credit usage before syncing."
        )
    if not manual and not source.is_active:
        raise OutboundSourceUnavailable("Enable the outbound source first.")
    try:
        adapter = prospect_source_adapter("apollo")
        adapter_ready = adapter.is_ready(org_id=source.org_id)
    except ProviderAdapterUnavailable:
        adapter_ready = False
    if not adapter_ready:
        raise OutboundSourceUnavailable(
            "Configure and enable the Apollo connection before syncing."
        )
    execution_request_id = None
    has_execution_grant = approval_id is not None or idempotency_key is not None
    # The quota reservation and its durable one-attempt job are committed as
    # one unit.  A process failure between those writes cannot strand RESERVED
    # quota without a job that owns it.
    with transaction.atomic():
        if has_execution_grant:
            if approval_id is None or idempotency_key is None:
                raise OutboundSourceUnavailable(
                    "approval_id and idempotency_key are both required.",
                    code="apollo_search_approval_required",
                )
            intent = apollo_search_execution_intent(source)
            try:
                reservation = reserve_execution(
                    org=source.org,
                    channel=intent.channel,
                    action=intent.action,
                    target_hash=intent.target_hash,
                    payload_hash=intent.payload_hash,
                    units=intent.units,
                    approval_id=approval_id,
                    idempotency_key=idempotency_key,
                )
            except ExecutionSafetyError as exc:
                raise _execution_error(exc) from exc
            request = reservation.request
            if (
                reservation.replayed
                and request.status != ExternalRequestStatus.RESERVED
            ):
                raise OutboundSourceUnavailable(
                    "This Apollo execution was already attempted and cannot be replayed.",
                    code="apollo_execution_not_replayable",
                )
            execution_request_id = request.id
        elif not getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
            raise OutboundSourceUnavailable(
                "An exact, single-use Apollo search approval is required.",
                code="apollo_search_approval_required",
            )
        enqueued = enqueue_job(
            JobRequest(
                org_id=source.org_id,
                name=OUTBOUND_SOURCE_SYNC_JOB,
                idempotency_key=(
                    f"outbound-source:{source.id}:apollo-search:"
                    f"{execution_request_id or source.sync_count + 1}"
                ),
                payload={
                    "org_id": str(source.org_id),
                    "source_id": str(source.id),
                    "manual": manual,
                    "execution_request_id": (
                        str(execution_request_id) if execution_request_id else ""
                    ),
                },
                # Real provider calls are never automatically retried. A new
                # exact approval and idempotency key are required instead.
                max_attempts=1 if execution_request_id else 5,
            )
        )
    job = enqueued.job
    terminal_replay = enqueued.terminal_replay
    if job.status == AutomationJobStatus.DEAD_LETTER:
        if execution_request_id is not None:
            _release_pending_apollo_execution(
                source=source,
                execution_request_id=execution_request_id,
                error_code="apollo_job_dead_letter",
            )
            raise OutboundSourceUnavailable(
                "A failed Apollo job cannot be replayed; issue a new approval.",
                code="apollo_execution_not_replayable",
            )
        job = replay_dead_letter(job_id=job.id, org_id=source.org_id)
        terminal_replay = False
    elif job.status == AutomationJobStatus.CANCELLED:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="apollo_job_cancelled",
        )
        raise OutboundSourceUnavailable(
            "The previous source sync was cancelled and cannot be replayed."
        )
    SDROutboundSource.objects.filter(id=source.id, org_id=source.org_id).update(
        last_job_id=job.id,
        last_error_code="",
        last_error_message="",
    )
    if not terminal_replay:
        transaction.on_commit(lambda: _safe_dispatch(job))
    return job


def enqueue_apollo_candidate_enrichment(
    candidate: SDRApolloCandidate,
    *,
    approval_id: UUID,
    idempotency_key: UUID,
):
    with transaction.atomic():
        candidate = (
            SDRApolloCandidate.objects.select_for_update()
            .filter(org=candidate.org, id=candidate.id)
            .select_related("source", "source__campaign")
            .first()
        )
        if candidate is None:
            raise OutboundSourceUnavailable(
                "Apollo candidate was not found.",
                code="apollo_candidate_not_found",
            )
        if candidate.status not in {
            ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL,
            ApolloCandidateStatus.ENRICHMENT_RESERVED,
        }:
            raise OutboundSourceUnavailable(
                "This Apollo candidate is not awaiting enrichment approval.",
                code="apollo_candidate_not_pending",
            )
        source = candidate.source
        if not source.enrichment_credits_acknowledged:
            raise OutboundSourceUnavailable(
                "Acknowledge Apollo enrichment credit usage before enriching."
            )
        if source.campaign.status == OutboundCampaignStatus.ARCHIVED:
            raise OutboundSourceUnavailable(
                "Reopen the campaign before enriching Apollo candidates.",
                code="campaign_archived",
            )
        try:
            adapter = prospect_source_adapter("apollo")
            adapter_ready = adapter.is_ready(org_id=candidate.org_id)
        except ProviderAdapterUnavailable:
            adapter_ready = False
        if not adapter_ready:
            raise OutboundSourceUnavailable(
                "Configure and enable the Apollo connection before enriching.",
                code="apollo_connection_unavailable",
            )
        try:
            intent = apollo_enrichment_execution_intent(candidate)
        except Exception as exc:
            raise OutboundSourceUnavailable(
                "Apollo candidate credentials are unavailable.",
                code="apollo_candidate_credentials_unavailable",
            ) from exc
        try:
            reservation = reserve_execution(
                org=candidate.org,
                channel=intent.channel,
                action=intent.action,
                target_hash=intent.target_hash,
                payload_hash=intent.payload_hash,
                units=intent.units,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )
        except ExecutionSafetyError as exc:
            raise _execution_error(exc) from exc
        request = reservation.request
        if reservation.replayed and request.status != ExternalRequestStatus.RESERVED:
            raise OutboundSourceUnavailable(
                "This Apollo enrichment was already attempted and cannot be replayed.",
                code="apollo_execution_not_replayable",
            )
        if reservation.replayed and candidate.enrichment_request_id not in {
            None,
            request.id,
        }:
            raise OutboundSourceUnavailable(
                "Apollo candidate is bound to another enrichment request.",
                code="apollo_candidate_request_conflict",
            )
        if candidate.status == ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL:
            candidate.status = ApolloCandidateStatus.ENRICHMENT_RESERVED
            candidate.enrichment_request = request
            candidate.save(update_fields=["status", "enrichment_request", "updated_at"])
        if candidate.enrichment_request_id != request.id:
            raise OutboundSourceUnavailable(
                "Apollo candidate changed before enrichment could be queued.",
                code="apollo_candidate_request_conflict",
            )
        enqueued = enqueue_job(
            JobRequest(
                org_id=candidate.org_id,
                name=APOLLO_CANDIDATE_ENRICH_JOB,
                idempotency_key=(
                    f"apollo-candidate:{candidate.id}:enrich:{request.id}"
                ),
                payload={
                    "org_id": str(candidate.org_id),
                    "source_id": str(source.id),
                    "candidate_id": str(candidate.id),
                    "execution_request_id": str(request.id),
                },
                max_attempts=1,
            )
        )
    job = enqueued.job
    if job.status in {AutomationJobStatus.DEAD_LETTER, AutomationJobStatus.CANCELLED}:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=request.id,
            error_code="apollo_enrichment_job_terminal",
        )
        SDRApolloCandidate.objects.filter(
            org=candidate.org,
            id=candidate.id,
            enrichment_request=request,
        ).update(status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL)
        raise OutboundSourceUnavailable(
            "A terminal Apollo enrichment job cannot be replayed; issue a new approval.",
            code="apollo_execution_not_replayable",
        )
    if not enqueued.terminal_replay:
        transaction.on_commit(lambda: _safe_dispatch(job))
    return job


def process_outbound_source_sync_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        source_id = UUID(str(payload["source_id"]))
        manual = bool(payload.get("manual", False))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The outbound source job payload is invalid.",
            code="invalid_job_payload",
        ) from exc
    execution_request_id = None
    raw_execution_request_id = str(payload.get("execution_request_id") or "").strip()
    if raw_execution_request_id:
        try:
            execution_request_id = UUID(raw_execution_request_id)
        except ValueError as exc:
            raise PermanentJobError(
                "The Apollo execution request id is invalid.",
                code="invalid_job_payload",
            ) from exc
    elif not getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
        raise PermanentJobError(
            "The Apollo job has no approved execution request.",
            code="apollo_search_approval_required",
        )

    source = (
        SDROutboundSource.objects.filter(id=source_id, org_id=org_id)
        .select_related("campaign")
        .first()
    )
    if source is None:
        _release_orphan_apollo_execution(
            org_id=org_id,
            execution_request_id=execution_request_id,
            error_code="outbound_source_not_found",
        )
        raise PermanentJobError(
            "The outbound source no longer exists.",
            code="outbound_source_not_found",
        )
    if not manual and not source.is_active:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="outbound_source_inactive",
        )
        return {"source_id": str(source.id), "status": "skipped", "reason": "inactive"}
    if source.campaign.status == OutboundCampaignStatus.ARCHIVED:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="campaign_archived",
        )
        return {
            "source_id": str(source.id),
            "status": "skipped",
            "reason": "campaign_archived",
        }
    try:
        adapter = prospect_source_adapter("apollo")
        client = adapter.client_for(org_id=org_id)
    except Exception:
        # Credential lookup/decryption happens before provider I/O.  Release
        # the exact reservation and persist only a fixed, non-sensitive error.
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="apollo_connection_unavailable",
        )
        _record_source_error(
            source,
            code="apollo_connection_unavailable",
            message=APOLLO_SEARCH_SAFE_ERROR,
        )
        raise PermanentJobError(
            APOLLO_SEARCH_SAFE_ERROR,
            code="apollo_connection_unavailable",
        ) from None
    if client is None:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="apollo_connection_unavailable",
        )
        return _permanent_failure(
            source,
            code="apollo_connection_unavailable",
            message="The Apollo connection is not configured or active.",
        )

    try:
        stats = _sync_apollo_source(
            source=source,
            client=client,
            execution_request_id=execution_request_id,
        )
    except ProviderAdapterError as exc:
        _record_source_error(
            source,
            code=exc.error_code,
            message=APOLLO_SEARCH_SAFE_ERROR,
        )
        raise PermanentJobError(
            APOLLO_SEARCH_SAFE_ERROR,
            code=exc.error_code,
        ) from None
    except Exception:
        _record_source_error(
            source,
            code="outbound_source_sync_failed",
            message=APOLLO_SEARCH_SAFE_ERROR,
        )
        raise PermanentJobError(
            APOLLO_SEARCH_SAFE_ERROR,
            code="outbound_source_sync_failed",
        ) from None

    now = timezone.now()
    SDROutboundSource.objects.filter(id=source.id, org_id=org_id).update(
        last_sync_at=now,
        next_sync_at=now + timedelta(hours=source.interval_hours),
        next_page=stats["next_page"],
        sync_count=source.sync_count + 1,
        last_sync_stats=stats,
        last_error_code="",
        last_error_message="",
    )
    adapter.mark_synced(org_id=org_id, synced_at=now)
    return {"source_id": str(source.id), "status": "succeeded", **stats}


def process_apollo_candidate_enrichment_job(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        source_id = UUID(str(payload["source_id"]))
        candidate_id = UUID(str(payload["candidate_id"]))
        execution_request_id = UUID(str(payload["execution_request_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The Apollo enrichment job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    candidate = (
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            id=candidate_id,
            source_id=source_id,
        )
        .select_related("source", "source__campaign")
        .first()
    )
    if candidate is None:
        _release_orphan_apollo_execution(
            org_id=org_id,
            execution_request_id=execution_request_id,
            error_code="apollo_candidate_not_found",
        )
        raise PermanentJobError(
            "The Apollo candidate no longer exists.",
            code="apollo_candidate_not_found",
        )
    source = candidate.source
    if source.campaign.status == OutboundCampaignStatus.ARCHIVED:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="campaign_archived",
        )
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            id=candidate.id,
            enrichment_request_id=execution_request_id,
        ).update(status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL)
        raise PermanentJobError(
            "The Apollo candidate campaign is archived.",
            code="campaign_archived",
        )
    try:
        adapter = prospect_source_adapter("apollo")
        client = adapter.client_for(org_id=org_id)
    except Exception:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="apollo_connection_unavailable",
        )
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            id=candidate.id,
            enrichment_request_id=execution_request_id,
        ).update(status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL)
        _record_source_error(
            source,
            code="apollo_connection_unavailable",
            message=APOLLO_ENRICH_SAFE_ERROR,
        )
        raise PermanentJobError(
            APOLLO_ENRICH_SAFE_ERROR,
            code="apollo_connection_unavailable",
        ) from None
    if client is None:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=execution_request_id,
            error_code="apollo_connection_unavailable",
        )
        SDRApolloCandidate.objects.filter(
            org_id=org_id,
            id=candidate.id,
            enrichment_request_id=execution_request_id,
        ).update(status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL)
        raise PermanentJobError(
            "The Apollo connection is not configured or active.",
            code="apollo_connection_unavailable",
        )

    try:
        result = _execute_approved_apollo_enrichment(
            source=source,
            candidate=candidate,
            client=client,
            execution_request_id=execution_request_id,
        )
    except ProviderAdapterError as exc:
        _record_source_error(
            source,
            code=exc.error_code,
            message=APOLLO_ENRICH_SAFE_ERROR,
        )
        # max_attempts=1 is also persisted on the job. Even transport-looking
        # errors are terminal because their provider outcome is UNKNOWN.
        raise PermanentJobError(
            APOLLO_ENRICH_SAFE_ERROR,
            code=exc.error_code,
        ) from None
    except Exception:
        _record_source_error(
            source,
            code="apollo_enrichment_failed",
            message=APOLLO_ENRICH_SAFE_ERROR,
        )
        raise PermanentJobError(
            APOLLO_ENRICH_SAFE_ERROR,
            code="apollo_enrichment_failed",
        ) from None
    return {
        "source_id": str(source.id),
        "candidate_id": str(candidate.id),
        "status": "succeeded",
        **result,
    }


def _execute_approved_apollo_enrichment(
    *,
    source: SDROutboundSource,
    candidate: SDRApolloCandidate,
    client,
    execution_request_id: UUID,
) -> dict[str, Any]:
    candidate = SDRApolloCandidate.objects.get(
        org=source.org,
        id=candidate.id,
        source=source,
    )
    request = external_execution_request(
        org=source.org,
        request_id=execution_request_id,
    )
    if request is None:
        raise ProviderAdapterError(
            "Apollo enrichment execution request was not found.",
            error_code="apollo_execution_request_not_found",
        )
    try:
        intent = apollo_enrichment_execution_intent(candidate)
        provider_person_id = candidate.get_provider_person_id()
    except Exception:
        if request.status == ExternalRequestStatus.RESERVED:
            _release_pending_apollo_execution(
                source=source,
                execution_request_id=request.id,
                error_code="apollo_candidate_credentials_unavailable",
            )
        _set_candidate_pending_if_bound(candidate=candidate, request=request)
        raise ProviderAdapterError(
            "Apollo candidate credentials are unavailable.",
            error_code="apollo_candidate_credentials_unavailable",
        ) from None
    if (
        request.channel != ExecutionChannel.APOLLO
        or request.action != APOLLO_ENRICH_ACTION
        or request.target_hash != intent.target_hash
        or request.payload_hash != intent.payload_hash
        or request.units != 1
        or candidate.enrichment_request_id != request.id
    ):
        if request.status == ExternalRequestStatus.RESERVED:
            _release_pending_apollo_execution(
                source=source,
                execution_request_id=request.id,
                error_code="apollo_enrichment_snapshot_changed",
            )
        _set_candidate_pending_if_bound(candidate=candidate, request=request)
        raise ProviderAdapterError(
            "Apollo enrichment approval no longer matches the candidate snapshot.",
            error_code="apollo_enrichment_snapshot_changed",
        )
    if request.status != ExternalRequestStatus.RESERVED:
        raise ProviderAdapterError(
            "Apollo enrichment was already attempted and cannot be replayed.",
            error_code="apollo_execution_not_replayable",
        )
    if candidate.status != ApolloCandidateStatus.ENRICHMENT_RESERVED:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=request.id,
            error_code="apollo_candidate_not_reserved",
        )
        raise ProviderAdapterError(
            "Apollo candidate is not reserved for this enrichment.",
            error_code="apollo_candidate_not_reserved",
        )
    try:
        bound_client = client.for_execution(
            org=source.org,
            action=APOLLO_ENRICH_ACTION,
            execution_request_id=request.id,
        )
    except Exception:
        _release_pending_apollo_execution(
            source=source,
            execution_request_id=request.id,
            error_code="apollo_client_not_execution_bound",
        )
        _set_candidate_pending_if_bound(candidate=candidate, request=request)
        raise ProviderAdapterError(
            "Apollo client cannot bind a one-call execution request.",
            error_code="apollo_client_not_execution_bound",
        ) from None
    try:
        _claim_apollo_execution(source=source, request=request)
    except ProviderAdapterError:
        request.refresh_from_db()
        if request.status == ExternalRequestStatus.FAILED:
            _set_candidate_pending_if_bound(candidate=candidate, request=request)
        raise

    try:
        person = bound_client.enrich_person(person_id=provider_person_id)
        if person is None:
            updated = SDRApolloCandidate.objects.filter(
                org=source.org,
                id=candidate.id,
                enrichment_request=request,
                status=ApolloCandidateStatus.ENRICHMENT_RESERVED,
            ).update(status=ApolloCandidateStatus.SKIPPED)
            if updated != 1:
                raise RuntimeError("Apollo candidate state changed during enrichment")
            batch_id = ""
            candidate_status = ApolloCandidateStatus.SKIPPED
        else:
            batch = persist_apollo_enrichment_import(
                source=source,
                candidate=candidate,
                execution_request=request,
                person=person,
            )
            batch_id = str(batch.id)
            candidate_status = ApolloCandidateStatus.IMPORT_QUEUED
    except ProviderAdapterError as exc:
        _settle_failed_apollo_call(
            source=source,
            request=request,
            error_code=exc.error_code,
            provider_call_started=True,
        )
        request.refresh_from_db()
        if request.status == ExternalRequestStatus.FAILED:
            _set_candidate_pending_if_bound(candidate=candidate, request=request)
        else:
            SDRApolloCandidate.objects.filter(
                org=source.org,
                id=candidate.id,
                enrichment_request=request,
            ).update(status=ApolloCandidateStatus.UNKNOWN)
        raise
    except Exception:
        _mark_apollo_unknown(source=source, request=request)
        SDRApolloCandidate.objects.filter(
            org=source.org,
            id=candidate.id,
            enrichment_request=request,
        ).update(status=ApolloCandidateStatus.UNKNOWN)
        raise

    _settle_apollo_success(source=source, request=request)
    return {
        "execution_request_id": str(request.id),
        "candidate_status": candidate_status,
        "person_import_batch_id": batch_id,
    }


def _set_candidate_pending_if_bound(
    *,
    candidate: SDRApolloCandidate,
    request: ExternalExecutionRequestPort,
) -> None:
    SDRApolloCandidate.objects.filter(
        org=candidate.org,
        id=candidate.id,
        enrichment_request=request,
        status=ApolloCandidateStatus.ENRICHMENT_RESERVED,
    ).update(status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL)


def _release_pending_apollo_execution(
    *,
    source: SDROutboundSource,
    execution_request_id: UUID | None,
    error_code: str,
) -> None:
    if execution_request_id is None:
        return
    request = external_execution_request(
        org=source.org,
        request_id=execution_request_id,
        status=ExternalRequestStatus.RESERVED,
    )
    if request is None:
        return
    try:
        release_execution(
            org=source.org,
            request_id=request.id,
            error_code=error_code,
        )
    except Exception:
        logger.exception("Could not release Apollo execution %s", request.id)


def _release_orphan_apollo_execution(
    *,
    org_id: UUID,
    execution_request_id: UUID | None,
    error_code: str,
) -> None:
    if execution_request_id is None:
        return
    request = external_execution_request(
        org_id=org_id,
        request_id=execution_request_id,
        status=ExternalRequestStatus.RESERVED,
        include_org=True,
    )
    if request is None:
        return
    try:
        release_execution(
            org=request.org,
            request_id=request.id,
            error_code=error_code,
        )
    except Exception:
        logger.exception("Could not release orphan Apollo execution %s", request.id)


def reconcile_outbound_sources(*, org_id: UUID, limit: int = 50) -> int:
    now = timezone.now()
    sources = SDROutboundSource.objects.filter(
        org_id=org_id,
        is_active=True,
        next_sync_at__lte=now,
    ).order_by("next_sync_at", "created_at")[:limit]
    queued = 0
    for source in sources:
        try:
            enqueue_outbound_source_sync(source)
            queued += 1
        except OutboundSourceUnavailable:
            logger.warning("Outbound source %s is not ready", source.id)
    return queued


def _sync_apollo_source(
    *,
    source: SDROutboundSource,
    client,
    execution_request_id: UUID | None = None,
) -> dict[str, Any]:
    if getattr(settings, "ALLOW_UNGUARDED_PROVIDER_IO", False):
        return _sync_apollo_source_unguarded(source=source, client=client)
    if execution_request_id is None:
        raise ProviderAdapterError(
            "An approved Apollo search execution is required.",
            error_code="apollo_search_approval_required",
        )
    return _execute_approved_apollo_search(
        source=source,
        client=client,
        execution_request_id=execution_request_id,
    )


def _execute_approved_apollo_search(
    *,
    source: SDROutboundSource,
    client,
    execution_request_id: UUID,
) -> dict[str, Any]:
    request = external_execution_request(
        org=source.org,
        request_id=execution_request_id,
    )
    if request is None:
        raise ProviderAdapterError(
            "Apollo search execution request was not found.",
            error_code="apollo_execution_request_not_found",
        )
    try:
        intent = apollo_search_execution_intent(source)
    except Exception:
        if request.status == ExternalRequestStatus.RESERVED:
            _release_pending_apollo_execution(
                source=source,
                execution_request_id=request.id,
                error_code="apollo_search_snapshot_unavailable",
            )
        raise ProviderAdapterError(
            "Apollo search snapshot is unavailable.",
            error_code="apollo_search_snapshot_unavailable",
        ) from None
    if (
        request.channel != ExecutionChannel.APOLLO
        or request.action != APOLLO_SEARCH_ACTION
        or request.target_hash != intent.target_hash
        or request.payload_hash != intent.payload_hash
        or request.units != 1
    ):
        if request.status == ExternalRequestStatus.RESERVED:
            try:
                release_execution(
                    org=source.org,
                    request_id=request.id,
                    error_code="apollo_search_snapshot_changed",
                )
            except Exception:
                logger.exception(
                    "Could not release stale Apollo search execution %s",
                    request.id,
                )
        raise ProviderAdapterError(
            "Apollo search approval no longer matches the source snapshot.",
            error_code="apollo_search_snapshot_changed",
        )
    if request.status != ExternalRequestStatus.RESERVED:
        raise ProviderAdapterError(
            "Apollo search execution was already attempted and cannot be replayed.",
            error_code="apollo_execution_not_replayable",
        )
    try:
        bound_client = client.for_execution(
            org=source.org,
            action=APOLLO_SEARCH_ACTION,
            execution_request_id=request.id,
        )
    except Exception:
        try:
            release_execution(
                org=source.org,
                request_id=request.id,
                error_code="apollo_client_not_execution_bound",
            )
        except Exception:
            logger.exception(
                "Could not release unbound Apollo execution %s", request.id
            )
        raise ProviderAdapterError(
            "Apollo client cannot bind a one-call execution request.",
            error_code="apollo_client_not_execution_bound",
        ) from None

    _claim_apollo_execution(source=source, request=request)

    payload = _apollo_search_payload(source)
    provider_call_started = False
    try:
        provider_call_started = True
        search = bound_client.search_people(**payload)
        candidate_stats = _persist_apollo_search_candidates(
            source=source,
            execution_request=request,
            search=search,
        )
    except ProviderAdapterError as exc:
        _settle_failed_apollo_call(
            source=source,
            request=request,
            error_code=exc.error_code,
            provider_call_started=provider_call_started,
        )
        raise
    except Exception:
        _mark_apollo_unknown(source=source, request=request)
        raise

    # Both transitions share an outer transaction. If DELIVERED cannot be
    # written, ACCEPTED and its quota projection roll back to SENDING so the
    # stale-request reconciler can conservatively mark UNKNOWN.
    _settle_apollo_success(source=source, request=request)

    pagination = search.get("pagination")
    if not isinstance(pagination, Mapping):
        pagination = {}
    try:
        total_entries = max(
            0,
            int(
                pagination.get(
                    "total_entries",
                    search.get("total_entries", candidate_stats["searched"]),
                )
            ),
        )
    except (TypeError, ValueError):
        total_entries = candidate_stats["searched"]
    total_pages = max(
        1, min(500, (total_entries + payload["per_page"] - 1) // payload["per_page"])
    )
    next_page = payload["page"] + 1 if payload["page"] < total_pages else 1
    return {
        "page": payload["page"],
        "next_page": next_page,
        **candidate_stats,
        "enrichment_requests": 0,
        "created": 0,
        "duplicates": 0,
        "invalid": 0,
        "total_entries": total_entries,
        "status": "awaiting_enrichment_approval",
        "search_execution_request_id": str(request.id),
    }


@transaction.atomic
def _claim_apollo_execution(
    *,
    source: SDROutboundSource,
    request: ExternalExecutionRequestPort,
) -> None:
    """Atomically ensure only one worker can turn RESERVED into SENDING."""

    locked = external_execution_request(
        org=source.org,
        request_id=request.id,
        for_update=True,
    )
    if locked is None or locked.status != ExternalRequestStatus.RESERVED:
        raise ProviderAdapterError(
            "Apollo execution was already claimed and cannot be replayed.",
            error_code="apollo_execution_not_replayable",
        )
    try:
        mark_execution_sending(org=source.org, request_id=locked.id)
    except ExecutionSafetyError as exc:
        raise ProviderAdapterError(
            exc.detail,
            error_code=exc.code,
        ) from exc


def _sync_apollo_source_unguarded(
    *,
    source: SDROutboundSource,
    client,
) -> dict[str, Any]:
    page = max(1, min(source.next_page, 500))
    per_page = min(100, max(25, source.max_results_per_sync * 4))
    search = client.search_people(
        filters=source.search_filters,
        page=page,
        per_page=per_page,
    )
    search_people = [
        item for item in search.get("people", []) if isinstance(item, Mapping)
    ]
    candidates = []
    for person in search_people:
        person_id = str(person.get("id") or person.get("person_id") or "").strip()
        if person_id:
            candidates.append((person_id, person))

    source_urls = [
        APOLLO_PERSON_URL.format(person_id=person_id) for person_id, _ in candidates
    ]
    existing_urls = set(
        source.campaign.prospects.filter(source_url__in=source_urls).values_list(
            "source_url", flat=True
        )
    )
    unseen_candidates = [
        (person_id, person)
        for person_id, person in candidates
        if APOLLO_PERSON_URL.format(person_id=person_id) not in existing_urls
    ]
    new_candidates = unseen_candidates[: source.max_results_per_sync]

    created = 0
    duplicates = len(candidates) - len(unseen_candidates)
    invalid = 0
    enriched = 0
    for person_id, search_person in new_candidates:
        person = client.enrich_person(person_id=person_id)
        enriched += 1
        if person is None:
            invalid += 1
            continue
        values = _prospect_values(
            source=source,
            person_id=person_id,
            person=person,
            search_person=search_person,
        )
        result = import_prospect_csv(
            campaign=source.campaign,
            csv_text=_single_record_csv(values),
        )
        created += result["created"]
        duplicates += result["duplicate_count"]
        invalid += result["error_count"]

    pagination = search.get("pagination")
    if not isinstance(pagination, Mapping):
        pagination = {}
    try:
        total_entries = max(
            0,
            int(
                pagination.get(
                    "total_entries",
                    search.get("total_entries", len(search_people)),
                )
            ),
        )
    except (TypeError, ValueError):
        total_entries = len(search_people)
    total_pages = max(1, min(500, (total_entries + per_page - 1) // per_page))
    next_page = page + 1 if page < total_pages else 1
    return {
        "page": page,
        "next_page": next_page,
        "searched": len(search_people),
        "enrichment_requests": enriched,
        "created": created,
        "duplicates": duplicates,
        "invalid": invalid,
        "total_entries": total_entries,
    }


@transaction.atomic
def _persist_apollo_search_candidates(
    *,
    source: SDROutboundSource,
    execution_request: ExternalExecutionRequestPort,
    search: Mapping[str, Any],
) -> dict[str, int]:
    """Persist only encrypted provider ids and non-identifying safe labels."""

    people = search.get("people")
    if not isinstance(people, list):
        raise ProviderAdapterError(
            "Apollo returned an invalid people search response.",
            error_code="apollo_invalid_search_response",
        )
    created_count = 0
    pending_count = 0
    invalid_count = 0
    seen_hashes: set[str] = set()
    for person in people:
        if not isinstance(person, Mapping):
            invalid_count += 1
            continue
        provider_person_id = str(
            person.get("id") or person.get("person_id") or ""
        ).strip()
        if not provider_person_id or len(provider_person_id) > 255:
            invalid_count += 1
            continue
        target_hash = hash_target_identifier(
            org=source.org,
            channel=ExecutionChannel.APOLLO,
            identifier=f"apollo-person:{provider_person_id}",
        )
        if target_hash in seen_hashes:
            continue
        seen_hashes.add(target_hash)
        candidate = (
            SDRApolloCandidate.objects.select_for_update()
            .filter(
                org=source.org,
                source=source,
                provider_person_id_hash=target_hash,
            )
            .first()
        )
        if candidate is None:
            candidate = SDRApolloCandidate(
                org=source.org,
                source=source,
                search_request=execution_request,
                provider_person_id_hash=target_hash,
                safe_label=f"Apollo candidate {target_hash[:8]}",
                status=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL,
            )
            candidate.set_provider_person_id(provider_person_id)
            candidate.save(force_insert=True)
            created_count += 1
        else:
            SDRApolloCandidate.objects.filter(
                org=source.org,
                id=candidate.id,
            ).update(search_request=execution_request)
        pending_count += 1
    return {
        "searched": len(people),
        "candidate_count": pending_count,
        "new_candidate_count": created_count,
        "invalid_candidate_count": invalid_count,
    }


def _is_explicit_apollo_4xx(error_code: str) -> bool:
    prefix = "apollo_http_"
    if not str(error_code).startswith(prefix):
        return False
    try:
        status_code = int(str(error_code)[len(prefix) :])
    except ValueError:
        return False
    # Timeout/early-data/limit responses can be returned after Apollo has
    # accepted and charged the request, so they are deliberately UNKNOWN.
    return 400 <= status_code < 500 and status_code not in {408, 425, 429}


def _settle_failed_apollo_call(
    *,
    source: SDROutboundSource,
    request: ExternalExecutionRequestPort,
    error_code: str,
    provider_call_started: bool,
) -> None:
    try:
        if not provider_call_started or _is_explicit_apollo_4xx(error_code):
            release_execution(
                org=source.org,
                request_id=request.id,
                error_code=error_code,
            )
        else:
            _mark_apollo_unknown(source=source, request=request)
    except Exception:
        logger.exception("Could not settle Apollo execution %s", request.id)


def _mark_apollo_unknown(
    *,
    source: SDROutboundSource,
    request: ExternalExecutionRequestPort,
) -> None:
    mark_provider_accepted(
        org=source.org,
        request_id=request.id,
        local_state_uncertain=True,
    )


@transaction.atomic
def _settle_apollo_success(
    *,
    source: SDROutboundSource,
    request: ExternalExecutionRequestPort,
) -> None:
    mark_provider_accepted(org=source.org, request_id=request.id)
    mark_execution_delivered(org=source.org, request_id=request.id)


@transaction.atomic
def persist_apollo_enrichment_import(
    *,
    source: SDROutboundSource,
    candidate: SDRApolloCandidate,
    execution_request: ExternalExecutionRequestPort,
    person: Mapping[str, Any],
):
    """Put an enriched profile through the canonical Person import ledger.

    This service intentionally does not call Apollo. A future per-candidate
    worker invokes it only after its own approved `enrich_person` request.
    """

    candidate = (
        SDRApolloCandidate.objects.select_for_update()
        .filter(
            org=source.org,
            id=candidate.id,
            source=source,
        )
        .first()
    )
    execution_request = external_execution_request(
        org=source.org,
        request_id=execution_request.id,
    )
    intent = (
        apollo_enrichment_execution_intent(candidate) if candidate is not None else None
    )
    if (
        candidate is None
        or intent is None
        or execution_request is None
        or execution_request.channel != ExecutionChannel.APOLLO
        or execution_request.action != APOLLO_ENRICH_ACTION
        or execution_request.status != ExternalRequestStatus.SENDING
        or execution_request.target_hash != intent.target_hash
        or execution_request.payload_hash != intent.payload_hash
        or execution_request.units != 1
        or candidate.enrichment_request_id != execution_request.id
        or candidate.status != ApolloCandidateStatus.ENRICHMENT_RESERVED
    ):
        raise ProviderAdapterError(
            "Apollo enrichment import scope is invalid.",
            error_code="apollo_enrichment_scope_mismatch",
        )
    provider_person_id = candidate.get_provider_person_id()
    organization = person.get("organization")
    organization = organization if isinstance(organization, Mapping) else {}
    phone = str(person.get("sanitized_phone") or person.get("phone") or "").strip()
    if not phone:
        phone_numbers = person.get("phone_numbers")
        if isinstance(phone_numbers, list):
            for item in phone_numbers:
                if isinstance(item, Mapping):
                    phone = str(
                        item.get("sanitized_number") or item.get("raw_number") or ""
                    ).strip()
                    if phone:
                        break
    first_name = str(person.get("first_name") or "").strip()
    last_name = str(person.get("last_name") or "").strip()
    display_name = " ".join(item for item in (first_name, last_name) if item).strip()
    record = ProviderPersonRecord(
        source_record_id=provider_person_id,
        display_name=display_name or "Apollo profile",
        first_name=first_name,
        last_name=last_name,
        current_title=str(person.get("title") or "").strip(),
        current_company=str(organization.get("name") or "").strip(),
        location=str(person.get("country") or person.get("city") or "").strip(),
        email=str(person.get("email") or "").strip(),
        phone=phone,
        linkedin=str(person.get("linkedin_url") or "").strip(),
        evidence_summary="Apollo enriched profile imported for human review",
        observed_at=timezone.now(),
    )
    preview = preview_provider_person_import(
        org=source.org,
        requested_by=None,
        idempotency_key=execution_request.id,
        source="apollo",
        source_namespace="apollo:person",
        records=[record],
    )
    commit = commit_person_import(
        org=source.org,
        requested_by=None,
        batch=preview.batch,
        expected_revision=preview.batch.revision,
        idempotency_key=uuid5(
            APOLLO_IMPORT_COMMIT_NAMESPACE,
            str(execution_request.id),
        ),
    )
    SDRApolloCandidate.objects.filter(
        org=source.org,
        id=candidate.id,
    ).update(
        enrichment_request=execution_request,
        import_batch=commit.batch,
        status=ApolloCandidateStatus.IMPORT_QUEUED,
    )
    return commit.batch


def _prospect_values(
    *,
    source: SDROutboundSource,
    person_id: str,
    person: Mapping[str, Any],
    search_person: Mapping[str, Any],
) -> dict[str, str]:
    organization = person.get("organization")
    if not isinstance(organization, Mapping):
        organization = search_person.get("organization")
    organization = organization if isinstance(organization, Mapping) else {}
    phone = str(person.get("sanitized_phone") or person.get("phone") or "").strip()
    if not phone:
        phone_numbers = person.get("phone_numbers")
        if isinstance(phone_numbers, list):
            for item in phone_numbers:
                if isinstance(item, Mapping):
                    phone = str(
                        item.get("sanitized_number") or item.get("raw_number") or ""
                    ).strip()
                    if phone:
                        break
    website = str(
        organization.get("website_url")
        or (
            f"https://{organization.get('primary_domain')}"
            if organization.get("primary_domain")
            else ""
        )
    ).strip()
    return {
        "first_name": str(
            person.get("first_name") or search_person.get("first_name") or ""
        ).strip(),
        "last_name": str(person.get("last_name") or "").strip(),
        "email": str(person.get("email") or "").strip(),
        "phone": phone,
        "job_title": str(
            person.get("title") or search_person.get("title") or ""
        ).strip(),
        "linkedin_url": str(person.get("linkedin_url") or "").strip(),
        "company_name": str(organization.get("name") or "").strip(),
        "website": website,
        "industry": str(organization.get("industry") or "").strip(),
        "country": str(person.get("country") or "").strip(),
        "recipient_timezone": "",
        "source_url": APOLLO_PERSON_URL.format(person_id=person_id),
        "notes": f"Automatically imported from Apollo source: {source.name}",
    }


def _single_record_csv(values: Mapping[str, str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_HEADERS)
    writer.writeheader()
    writer.writerow({field: values.get(field, "") for field in CSV_HEADERS})
    return output.getvalue()


def _record_source_error(
    source: SDROutboundSource,
    *,
    code: str,
    message: str,
) -> None:
    SDROutboundSource.objects.filter(id=source.id, org_id=source.org_id).update(
        last_error_code=code[:80],
        last_error_message=message[:1000],
    )


def _permanent_failure(
    source: SDROutboundSource,
    *,
    code: str,
    message: str,
):
    _record_source_error(source, code=code, message=message)
    raise PermanentJobError(message, code=code)


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch outbound source job %s", job.id)
