"""Durable Meta Conversion Leads feedback from CRM funnel milestones."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from integrations.models import (
    FacebookConversionEvent,
    FacebookConversionEventStatus,
    FacebookConversionSettings,
)
from integrations.providers.facebook.client import (
    FacebookGraphAPIError,
    FacebookGraphClient,
)
from sdr.models import LeadIntake, LeadIntakeSource, LeadIntakeStatus

logger = logging.getLogger(__name__)

FACEBOOK_CONVERSION_JOB = "facebook.send_conversion_event"
MAX_EVENT_AGE = timedelta(days=7)
VALID_QUALIFICATION_BANDS = frozenset({"high", "medium", "low", "disqualified"})


def conversion_graph_client() -> FacebookGraphClient:
    return FacebookGraphClient(
        app_id=settings.META_APP_ID,
        app_secret=settings.META_APP_SECRET,
        api_version=settings.META_GRAPH_API_VERSION,
        base_url=settings.META_GRAPH_API_BASE_URL,
        timeout=settings.META_GRAPH_API_TIMEOUT,
    )


def schedule_conversion_events_for_intake(intake: LeadIntake) -> list[FacebookConversionEvent]:
    """Queue RawLead and configured qualified stages for one Meta lead intake."""

    if (
        intake.source != LeadIntakeSource.FACEBOOK_AD
        or intake.status != LeadIntakeStatus.COMPLETED
        or not _valid_leadgen_id(intake.source_record_id)
    ):
        return []
    configuration = _active_configuration(intake.org_id)
    if configuration is None:
        return []

    event_time = intake.processed_at or timezone.now()
    events = [
        _schedule_event(
            intake=intake,
            configuration=configuration,
            event_key=f"facebook:raw:{intake.id}",
            event_name=configuration.raw_lead_event_name,
            event_time=event_time,
        )
    ]
    qualified_bands = {
        str(value).strip().lower()
        for value in configuration.qualified_bands
        if str(value).strip().lower() in VALID_QUALIFICATION_BANDS
    }
    if intake.qualification_band in qualified_bands:
        events.append(
            _schedule_event(
                intake=intake,
                configuration=configuration,
                event_key=f"facebook:qualified:{intake.id}:{intake.qualification_band}",
                event_name=configuration.qualified_lead_event_name,
                event_time=event_time,
            )
        )
    return events


def schedule_converted_event_for_lead(
    *,
    org_id: UUID,
    lead_id: UUID,
    event_time,
) -> FacebookConversionEvent | None:
    """Queue the Converted stage for the latest Meta intake behind a CRM lead."""

    configuration = _active_configuration(org_id)
    if configuration is None:
        return None
    intake = (
        LeadIntake.objects.filter(
            org_id=org_id,
            crm_lead_id=lead_id,
            source=LeadIntakeSource.FACEBOOK_AD,
            status=LeadIntakeStatus.COMPLETED,
        )
        .order_by("-processed_at", "-created_at")
        .first()
    )
    if intake is None or not _valid_leadgen_id(intake.source_record_id):
        return None
    return _schedule_event(
        intake=intake,
        configuration=configuration,
        event_key=f"facebook:converted:{intake.id}",
        event_name=configuration.converted_event_name,
        event_time=event_time or timezone.now(),
    )


def reconcile_recent_conversion_events(*, org_id: UUID, limit: int = 500) -> int:
    """Backfill eligible Meta intakes from the provider's seven-day window."""

    cutoff = timezone.now() - MAX_EVENT_AGE
    intakes = list(
        LeadIntake.objects.filter(
            org_id=org_id,
            source=LeadIntakeSource.FACEBOOK_AD,
            status=LeadIntakeStatus.COMPLETED,
            processed_at__gte=cutoff,
        )
        .select_related("crm_lead")
        .order_by("processed_at")[:limit]
    )
    before = FacebookConversionEvent.objects.filter(
        org_id=org_id,
        created_at__gte=cutoff,
    ).count()
    for intake in intakes:
        schedule_conversion_events_for_intake(intake)
        if intake.crm_lead_id and (intake.crm_lead.status or "").lower() == "converted":
            schedule_converted_event_for_lead(
                org_id=org_id,
                lead_id=intake.crm_lead_id,
                event_time=intake.crm_lead.updated_at,
            )
    after = FacebookConversionEvent.objects.filter(
        org_id=org_id,
        created_at__gte=cutoff,
    ).count()
    return max(0, after - before)


def process_facebook_conversion_job(
    payload: Mapping[str, Any],
    *,
    client: FacebookGraphClient | None = None,
) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        event_id = UUID(str(payload["event_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "Facebook conversion job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    event = FacebookConversionEvent.objects.filter(
        id=event_id,
        org_id=org_id,
    ).first()
    if event is None:
        raise PermanentJobError(
            "Facebook conversion event no longer exists.",
            code="conversion_event_not_found",
        )
    if event.status == FacebookConversionEventStatus.SENT:
        return {"event_id": str(event.id), "status": event.status, "replayed": True}

    configuration = FacebookConversionSettings.objects.filter(org_id=org_id).first()
    if configuration is None or not configuration.is_enabled:
        _cancel_event(event, "Meta conversion feedback is disabled.")
        return {"event_id": str(event.id), "status": event.status}
    if not configuration.access_token_ciphertext:
        _fail_event(event, "conversion_token_missing", "Meta access token is missing.")
        raise PermanentJobError(
            "Meta conversion access token is missing.",
            code="conversion_token_missing",
        )
    if event.event_time < timezone.now() - MAX_EVENT_AGE:
        _fail_event(
            event,
            "conversion_event_expired",
            "The CRM event is outside Meta's seven-day upload window.",
        )
        raise PermanentJobError(
            "The Meta conversion event is older than seven days.",
            code="conversion_event_expired",
        )

    event_payload = {
        "event_name": event.event_name,
        "event_time": int(event.event_time.timestamp()),
        "action_source": "system_generated",
        "user_data": {"lead_id": int(event.leadgen_id)},
        "custom_data": {
            "event_source": "crm",
            "lead_event_source": event.lead_event_source,
        },
    }
    now = timezone.now()
    FacebookConversionEvent.objects.filter(id=event.id, org_id=org_id).update(
        last_attempted_at=now,
    )
    try:
        response = (client or conversion_graph_client()).send_conversion_event(
            pixel_id=event.pixel_id,
            access_token=configuration.get_access_token(),
            event=event_payload,
            test_event_code=event.test_event_code,
        )
    except FacebookGraphAPIError as exc:
        error_code = "meta_conversion_retryable" if exc.retryable else "meta_conversion_rejected"
        _fail_event(event, error_code, str(exc), terminal=not exc.retryable)
        error_class = RetryableJobError if exc.retryable else PermanentJobError
        raise error_class(str(exc), code=error_code) from exc

    sent_at = timezone.now()
    try:
        events_received = int(response.get("events_received", 1))
    except (TypeError, ValueError):
        events_received = 1
    provider_trace_id = str(response.get("fbtrace_id", ""))[:255]
    FacebookConversionEvent.objects.filter(id=event.id, org_id=org_id).update(
        status=FacebookConversionEventStatus.SENT,
        provider_events_received=events_received,
        provider_trace_id=provider_trace_id,
        error_code="",
        error_message="",
        last_attempted_at=sent_at,
        sent_at=sent_at,
    )
    FacebookConversionSettings.objects.filter(id=configuration.id).update(
        last_event_sent_at=sent_at,
    )
    return {
        "event_id": str(event.id),
        "status": FacebookConversionEventStatus.SENT,
        "events_received": events_received,
        "provider_trace_id": provider_trace_id,
    }


@transaction.atomic
def _schedule_event(
    *,
    intake: LeadIntake,
    configuration: FacebookConversionSettings,
    event_key: str,
    event_name: str,
    event_time,
) -> FacebookConversionEvent:
    event, _ = FacebookConversionEvent.objects.get_or_create(
        org_id=intake.org_id,
        event_key=event_key,
        defaults={
            "intake": intake,
            "crm_lead_id": intake.crm_lead_id,
            "leadgen_id": intake.source_record_id,
            "event_name": event_name,
            "event_time": event_time,
            "pixel_id": configuration.pixel_id,
            "lead_event_source": configuration.lead_event_source,
            "test_event_code": configuration.test_event_code,
        },
    )
    enqueued = enqueue_job(
        JobRequest(
            org_id=intake.org_id,
            name=FACEBOOK_CONVERSION_JOB,
            idempotency_key=f"facebook-conversion:{event.id}",
            payload={
                "org_id": str(intake.org_id),
                "event_id": str(event.id),
            },
            max_attempts=6,
        )
    )
    if not enqueued.terminal_replay:
        try:
            dispatch_job(enqueued.job)
        except Exception:
            logger.exception("Could not dispatch Meta conversion event %s", event.id)
    return event


def _active_configuration(org_id: UUID) -> FacebookConversionSettings | None:
    return FacebookConversionSettings.objects.filter(
        org_id=org_id,
        is_enabled=True,
    ).exclude(pixel_id="").exclude(access_token_ciphertext="").first()


def _valid_leadgen_id(value: str) -> bool:
    cleaned = str(value or "").strip()
    return cleaned.isdigit() and len(cleaned) in {15, 16}


def _cancel_event(event: FacebookConversionEvent, message: str) -> None:
    FacebookConversionEvent.objects.filter(id=event.id, org_id=event.org_id).update(
        status=FacebookConversionEventStatus.CANCELLED,
        error_code="conversion_disabled",
        error_message=message[:1000],
        last_attempted_at=timezone.now(),
    )
    event.status = FacebookConversionEventStatus.CANCELLED


def _fail_event(
    event: FacebookConversionEvent,
    code: str,
    message: str,
    *,
    terminal: bool = True,
) -> None:
    FacebookConversionEvent.objects.filter(id=event.id, org_id=event.org_id).update(
        status=(
            FacebookConversionEventStatus.FAILED
            if terminal
            else FacebookConversionEventStatus.PENDING
        ),
        error_code=code[:80],
        error_message=(message or "Meta conversion event failed.")[:1000],
        last_attempted_at=timezone.now(),
    )
