"""Recurring outbound prospect sources backed by durable automation jobs."""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from sdr.models import OutboundCampaignStatus, SDROutboundSource
from sdr.outbound import CSV_HEADERS, import_prospect_csv
from sdr.provider_ports import (
    ProviderAdapterError,
    ProviderAdapterUnavailable,
    prospect_source_adapter,
)

logger = logging.getLogger(__name__)

OUTBOUND_SOURCE_SYNC_JOB = "sdr.sync_outbound_source"
APOLLO_PERSON_URL = "https://app.apollo.io/#/people/{person_id}"


class OutboundSourceUnavailable(ValueError):
    pass


def enqueue_outbound_source_sync(
    source: SDROutboundSource,
    *,
    manual: bool = False,
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
    enqueued = enqueue_job(
        JobRequest(
            org_id=source.org_id,
            name=OUTBOUND_SOURCE_SYNC_JOB,
            idempotency_key=(
                f"outbound-source:{source.id}:sync:{source.sync_count + 1}"
            ),
            payload={
                "org_id": str(source.org_id),
                "source_id": str(source.id),
                "manual": manual,
            },
            max_attempts=5,
        )
    )
    job = enqueued.job
    terminal_replay = enqueued.terminal_replay
    if job.status == AutomationJobStatus.DEAD_LETTER:
        job = replay_dead_letter(job_id=job.id, org_id=source.org_id)
        terminal_replay = False
    elif job.status == AutomationJobStatus.CANCELLED:
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

    source = (
        SDROutboundSource.objects.filter(id=source_id, org_id=org_id)
        .select_related("campaign")
        .first()
    )
    if source is None:
        raise PermanentJobError(
            "The outbound source no longer exists.",
            code="outbound_source_not_found",
        )
    if not manual and not source.is_active:
        return {"source_id": str(source.id), "status": "skipped", "reason": "inactive"}
    if source.campaign.status == OutboundCampaignStatus.ARCHIVED:
        return {
            "source_id": str(source.id),
            "status": "skipped",
            "reason": "campaign_archived",
        }
    try:
        adapter = prospect_source_adapter("apollo")
        client = adapter.client_for(org_id=org_id)
    except ProviderAdapterUnavailable:
        client = None
    if client is None:
        return _permanent_failure(
            source,
            code="apollo_connection_unavailable",
            message="The Apollo connection is not configured or active.",
        )

    try:
        stats = _sync_apollo_source(source=source, client=client)
    except ProviderAdapterError as exc:
        _record_source_error(source, code=exc.error_code, message=str(exc))
        error_type = RetryableJobError if exc.retryable else PermanentJobError
        raise error_type(str(exc), code=exc.error_code) from exc
    except Exception as exc:
        _record_source_error(
            source,
            code="outbound_source_sync_failed",
            message=str(exc),
        )
        raise

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
) -> dict[str, Any]:
    page = max(1, min(source.next_page, 500))
    per_page = min(100, max(25, source.max_results_per_sync * 4))
    search = client.search_people(
        filters=source.search_filters,
        page=page,
        per_page=per_page,
    )
    search_people = [item for item in search.get("people", []) if isinstance(item, Mapping)]
    candidates = []
    for person in search_people:
        person_id = str(person.get("id") or person.get("person_id") or "").strip()
        if person_id:
            candidates.append((person_id, person))

    source_urls = [APOLLO_PERSON_URL.format(person_id=person_id) for person_id, _ in candidates]
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
        "first_name": str(person.get("first_name") or search_person.get("first_name") or "").strip(),
        "last_name": str(person.get("last_name") or "").strip(),
        "email": str(person.get("email") or "").strip(),
        "phone": phone,
        "job_title": str(person.get("title") or search_person.get("title") or "").strip(),
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
