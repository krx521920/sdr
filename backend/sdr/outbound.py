"""Outbound prospect list import, deduplication, and durable SDR promotion."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job, replay_dead_letter
from leads.models import Lead
from sdr.domain import CompanySnapshot, LeadCandidate, LeadIdentity, LeadSource
from sdr.models import (
    LeadIntake,
    LeadIntakeSource,
    LeadNurtureEnrollment,
    NurtureEnrollmentStatus,
    OutboundCampaignStatus,
    OutboundProspectStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
)
from sdr.nurture import (
    enroll_intake_in_sequence,
    pause_enrollment,
    resume_enrollment,
)
from sdr.routing import normalize_country
from sdr.services import (
    IntakeAlreadyProcessing,
    IntakeProcessingFailed,
    process_candidate_intake,
)

logger = logging.getLogger(__name__)

OUTBOUND_PROSPECT_JOB = "sdr.process_outbound_prospect"
MAX_IMPORT_ROWS = 500
MAX_CSV_CHARS = 1_000_000
CHANNELS = frozenset({"email", "linkedin", "phone", "whatsapp"})
CSV_HEADERS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "job_title",
    "linkedin_url",
    "company_name",
    "website",
    "industry",
    "country",
    "source_url",
    "notes",
)
URL_FIELDS = ("linkedin_url", "website", "source_url")
FIELD_LIMITS = {
    "first_name": 255,
    "last_name": 255,
    "email": 254,
    "phone": 32,
    "job_title": 255,
    "linkedin_url": 500,
    "company_name": 255,
    "website": 500,
    "industry": 255,
    "country": 100,
    "source_url": 1000,
}


class OutboundImportError(ValueError):
    pass


class OutboundProspectUnavailable(ValueError):
    pass


class OutboundCampaignExecutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedProspect:
    row_number: int
    values: dict[str, str]
    material: str
    dedupe_key: str


def import_prospect_csv(
    *,
    campaign: SDROutboundCampaign,
    csv_text: str,
    promote_ready: bool = False,
    created_by_id: UUID | None = None,
) -> dict[str, Any]:
    parsed, errors = _parse_csv(csv_text)
    existing_keys = set(
        SDROutboundProspect.objects.filter(
            org_id=campaign.org_id,
            dedupe_key__in=[row.dedupe_key for row in parsed],
        ).values_list("dedupe_key", flat=True)
    )
    existing_crm_materials = _existing_crm_materials(
        org_id=campaign.org_id,
        parsed=parsed,
    )
    seen_keys: set[str] = set()
    duplicates: list[dict[str, Any]] = []
    prospects: list[SDROutboundProspect] = []
    for row in parsed:
        reason = ""
        if row.dedupe_key in seen_keys:
            reason = "Duplicate of an earlier CSV row."
        elif row.dedupe_key in existing_keys:
            reason = "Prospect already exists in an outbound campaign."
        elif row.material in existing_crm_materials:
            reason = "Contact already exists in the CRM lead database."
        if reason:
            duplicates.append({"row": row.row_number, "reason": reason})
            continue
        seen_keys.add(row.dedupe_key)
        prospects.append(
            SDROutboundProspect(
                org_id=campaign.org_id,
                campaign=campaign,
                dedupe_key=row.dedupe_key,
                created_by_id=created_by_id,
                **row.values,
            )
        )

    created = SDROutboundProspect.objects.bulk_create(prospects)
    queued = 0
    if promote_ready:
        for prospect in created:
            enqueue_outbound_prospect(prospect)
            queued += 1

    return {
        "total_rows": len(parsed) + len(errors),
        "created": len(created),
        "queued": queued,
        "duplicate_count": len(duplicates),
        "error_count": len(errors),
        "duplicates": duplicates,
        "errors": errors,
        "prospect_ids": [str(prospect.id) for prospect in created],
    }


def enqueue_outbound_prospect(
    prospect: SDROutboundProspect,
    *,
    campaign_run: int | None = None,
):
    if prospect.status == OutboundProspectStatus.DISQUALIFIED:
        raise OutboundProspectUnavailable("Restore this prospect before promotion.")
    if campaign_run is not None and campaign_run < 1:
        raise OutboundProspectUnavailable("Launch the campaign before queueing prospects.")
    idempotency_key = (
        f"outbound-prospect:{prospect.id}:campaign:{campaign_run}"
        if campaign_run is not None
        else f"outbound-prospect:{prospect.id}:manual"
    )
    payload = {
        "org_id": str(prospect.org_id),
        "prospect_id": str(prospect.id),
    }
    if campaign_run is not None:
        payload["campaign_run"] = campaign_run
    enqueued = enqueue_job(
        JobRequest(
            org_id=prospect.org_id,
            name=OUTBOUND_PROSPECT_JOB,
            idempotency_key=idempotency_key,
            payload=payload,
            max_attempts=5,
        )
    )
    job = enqueued.job
    terminal_replay = enqueued.terminal_replay
    if job.status == AutomationJobStatus.DEAD_LETTER:
        job = replay_dead_letter(job_id=job.id, org_id=prospect.org_id)
        terminal_replay = False
    elif job.status == AutomationJobStatus.CANCELLED:
        raise OutboundProspectUnavailable(
            "The previous promotion job was cancelled and cannot be replayed."
        )
    if prospect.status != OutboundProspectStatus.PROMOTED:
        SDROutboundProspect.objects.filter(
            id=prospect.id,
            org_id=prospect.org_id,
        ).update(
            status=OutboundProspectStatus.QUEUED,
            queued_at=timezone.now(),
            queued_run=campaign_run or 0,
            last_error_code="",
            last_error_message="",
        )
        prospect.status = OutboundProspectStatus.QUEUED
        prospect.queued_at = timezone.now()
        prospect.queued_run = campaign_run or 0
    if not terminal_replay:
        transaction.on_commit(lambda: _safe_dispatch(job))
    return job


def process_outbound_prospect_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        prospect_id = UUID(str(payload["prospect_id"]))
        campaign_run = (
            int(payload["campaign_run"])
            if payload.get("campaign_run") is not None
            else None
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The outbound prospect job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    prospect = (
        SDROutboundProspect.objects.filter(id=prospect_id, org_id=org_id)
        .select_related("campaign", "intake")
        .first()
    )
    if prospect is None:
        raise PermanentJobError(
            "The outbound prospect no longer exists.",
            code="outbound_prospect_not_found",
        )
    if campaign_run is not None and (
        campaign_run < 1
        or prospect.campaign.status != OutboundCampaignStatus.ACTIVE
        or prospect.campaign.run_count != campaign_run
        or prospect.queued_run != campaign_run
    ):
        if prospect.queued_run == campaign_run:
            SDROutboundProspect.objects.filter(
                id=prospect.id,
                org_id=org_id,
                queued_run=campaign_run,
                status__in=(
                    OutboundProspectStatus.QUEUED,
                    OutboundProspectStatus.PROCESSING,
                ),
            ).update(status=OutboundProspectStatus.READY)
        return {
            "prospect_id": str(prospect.id),
            "status": "skipped",
            "reason": "campaign_not_active",
        }
    if prospect.status == OutboundProspectStatus.PROMOTED and prospect.intake_id:
        return _promotion_result(prospect, replayed=True)
    if prospect.status == OutboundProspectStatus.DISQUALIFIED:
        raise PermanentJobError(
            "The outbound prospect was disqualified.",
            code="outbound_prospect_disqualified",
        )

    SDROutboundProspect.objects.filter(id=prospect.id, org_id=org_id).update(
        status=OutboundProspectStatus.PROCESSING,
        attempt_count=prospect.attempt_count + 1,
        last_error_code="",
        last_error_message="",
    )
    prospect.attempt_count += 1
    candidate = _candidate_from_prospect(prospect)
    try:
        result = process_candidate_intake(
            candidate=candidate,
            raw_payload=_raw_payload(prospect),
        )
    except IntakeAlreadyProcessing as exc:
        _mark_failed(prospect, "intake_already_processing", str(exc))
        raise RetryableJobError(
            "The outbound intake is already processing.",
            code="intake_already_processing",
        ) from exc
    except IntakeProcessingFailed as exc:
        _mark_failed(prospect, "intake_processing_failed", str(exc))
        raise RetryableJobError(
            "The outbound SDR intake pipeline failed.",
            code="intake_processing_failed",
        ) from exc
    except Exception as exc:
        _mark_failed(prospect, "outbound_promotion_failed", str(exc))
        raise

    intake = LeadIntake.objects.get(id=result.intake_id, org_id=org_id)
    if campaign_run is not None:
        campaign = (
            SDROutboundCampaign.objects.filter(
                id=prospect.campaign_id,
                org_id=org_id,
                status=OutboundCampaignStatus.ACTIVE,
                run_count=campaign_run,
            )
            .select_related("sequence")
            .first()
        )
        if campaign and campaign.sequence_id:
            try:
                enroll_intake_in_sequence(intake, campaign.sequence)
            except ValueError as exc:
                _mark_failed(prospect, "outbound_enrollment_unavailable", str(exc))
                raise RetryableJobError(
                    "The outbound nurture enrollment is temporarily unavailable.",
                    code="outbound_enrollment_unavailable",
                ) from exc
    SDROutboundProspect.objects.filter(id=prospect.id, org_id=org_id).update(
        status=OutboundProspectStatus.PROMOTED,
        intake=intake,
        promoted_at=timezone.now(),
        last_error_code="",
        last_error_message="",
    )
    prospect.status = OutboundProspectStatus.PROMOTED
    prospect.intake = intake
    prospect.promoted_at = timezone.now()
    return _promotion_result(prospect, replayed=result.replayed)


def restore_outbound_prospect(prospect: SDROutboundProspect) -> None:
    if prospect.status not in {
        OutboundProspectStatus.DISQUALIFIED,
        OutboundProspectStatus.FAILED,
    }:
        raise OutboundProspectUnavailable("Only failed or disqualified prospects can be restored.")
    prospect.status = OutboundProspectStatus.READY
    prospect.last_error_code = ""
    prospect.last_error_message = ""
    prospect.save(
        update_fields=["status", "last_error_code", "last_error_message", "updated_at"]
    )


CAMPAIGN_PAUSE_REASON = "Outbound campaign paused."


def launch_outbound_campaign(campaign: SDROutboundCampaign) -> dict[str, Any]:
    """Start a new guarded campaign run and release today's first batch."""

    with transaction.atomic():
        locked = (
            SDROutboundCampaign.objects.select_for_update()
            .select_related("sequence")
            .get(id=campaign.id, org_id=campaign.org_id)
        )
        _validate_campaign_for_execution(locked)
        if locked.status == OutboundCampaignStatus.ARCHIVED:
            raise OutboundCampaignExecutionError(
                "Archived campaigns cannot be launched."
            )
        if locked.status != OutboundCampaignStatus.ACTIVE or locked.run_count < 1:
            locked.run_count += 1
            locked.status = OutboundCampaignStatus.ACTIVE
            locked.launched_at = timezone.now()
            locked.completed_at = None
            locked.save(
                update_fields=[
                    "run_count",
                    "status",
                    "launched_at",
                    "completed_at",
                    "updated_at",
                ]
            )
            SDROutboundProspect.objects.filter(
                org_id=locked.org_id,
                campaign=locked,
                status=OutboundProspectStatus.QUEUED,
            ).update(status=OutboundProspectStatus.READY)

    resumed = _resume_campaign_enrollments(locked)
    refill = refill_outbound_campaign(locked)
    return {
        "action": "launch",
        "run_count": locked.run_count,
        "resumed": resumed,
        **refill,
    }


def pause_outbound_campaign(campaign: SDROutboundCampaign) -> dict[str, Any]:
    """Pause future promotion and every active nurture enrollment in a campaign."""

    with transaction.atomic():
        locked = SDROutboundCampaign.objects.select_for_update().get(
            id=campaign.id,
            org_id=campaign.org_id,
        )
        if locked.status == OutboundCampaignStatus.ARCHIVED:
            raise OutboundCampaignExecutionError(
                "Archived campaigns are already inactive."
            )
        locked.status = OutboundCampaignStatus.PAUSED
        locked.save(update_fields=["status", "updated_at"])
        SDROutboundProspect.objects.filter(
            org_id=locked.org_id,
            campaign=locked,
            status=OutboundProspectStatus.QUEUED,
        ).update(status=OutboundProspectStatus.READY)

    paused = 0
    enrollments = LeadNurtureEnrollment.objects.filter(
        org_id=locked.org_id,
        intake__outbound_prospect__campaign=locked,
        status=NurtureEnrollmentStatus.ACTIVE,
    )
    for enrollment in enrollments.iterator(chunk_size=100):
        pause_enrollment(enrollment, reason=CAMPAIGN_PAUSE_REASON)
        paused += 1
    return {"action": "pause", "paused": paused, "queued": 0}


def finish_outbound_campaign(
    campaign: SDROutboundCampaign,
    *,
    archive: bool = False,
) -> dict[str, Any]:
    result = pause_outbound_campaign(campaign)
    next_status = (
        OutboundCampaignStatus.ARCHIVED
        if archive
        else OutboundCampaignStatus.COMPLETED
    )
    now = timezone.now()
    SDROutboundCampaign.objects.filter(
        id=campaign.id,
        org_id=campaign.org_id,
    ).update(
        status=next_status,
        completed_at=now if not archive else None,
    )
    result["action"] = "archive" if archive else "complete"
    return result


def refill_outbound_campaign(
    campaign: SDROutboundCampaign,
    *,
    failed_only: bool = False,
) -> dict[str, Any]:
    """Fill the current local day's durable promotion allowance."""

    with transaction.atomic():
        locked = (
            SDROutboundCampaign.objects.select_for_update()
            .select_related("sequence")
            .get(id=campaign.id, org_id=campaign.org_id)
        )
        _validate_campaign_for_execution(locked)
        if locked.status != OutboundCampaignStatus.ACTIVE or locked.run_count < 1:
            raise OutboundCampaignExecutionError(
                "Launch the campaign before queueing an outbound batch."
            )
        queued_today = SDROutboundProspect.objects.filter(
            org_id=locked.org_id,
            campaign=locked,
            queued_at__date=timezone.localdate(),
        ).count()
        remaining = max(0, locked.daily_send_limit - queued_today)
        base = (
            SDROutboundProspect.objects.select_for_update()
            .filter(
                org_id=locked.org_id,
                campaign=locked,
            )
            .exclude(email="")
            .order_by("created_at", "id")
        )
        newly_released = 0
        if failed_only:
            same_day = list(
                base.filter(
                    status=OutboundProspectStatus.FAILED,
                    queued_at__date=timezone.localdate(),
                    queued_run=locked.run_count,
                )[: locked.daily_send_limit]
            )
            older = list(
                base.filter(status=OutboundProspectStatus.FAILED)
                .exclude(id__in=[prospect.id for prospect in same_day])[:remaining]
            )
            prospects = [*same_day, *older]
            newly_released = len(older)
        else:
            same_day = list(
                base.filter(
                    status=OutboundProspectStatus.READY,
                    queued_at__date=timezone.localdate(),
                )
                .exclude(queued_run=locked.run_count)[: locked.daily_send_limit]
            )
            new = list(
                base.filter(status=OutboundProspectStatus.READY)
                .exclude(id__in=[prospect.id for prospect in same_day])[:remaining]
            )
            prospects = [*same_day, *new]
            newly_released = len(new)
        for prospect in prospects:
            enqueue_outbound_prospect(
                prospect,
                campaign_run=locked.run_count,
            )
        if prospects:
            locked.last_refilled_at = timezone.now()
            locked.save(update_fields=["last_refilled_at", "updated_at"])

    return {
        "queued": len(prospects),
        "daily_limit": locked.daily_send_limit,
        "used_today": queued_today + newly_released,
        "remaining_today": max(
            0,
            locked.daily_send_limit - queued_today - newly_released,
        ),
    }


def reconcile_outbound_campaigns(*, org_id: UUID, limit: int = 100) -> int:
    """Periodically refill active campaigns without exceeding their daily cap."""

    queued = 0
    campaigns = SDROutboundCampaign.objects.filter(
        org_id=org_id,
        status=OutboundCampaignStatus.ACTIVE,
        run_count__gt=0,
    ).order_by("last_refilled_at", "created_at")[:limit]
    for campaign in campaigns:
        try:
            queued += refill_outbound_campaign(campaign)["queued"]
        except OutboundCampaignExecutionError:
            logger.warning("Outbound campaign %s is not executable", campaign.id)
    return queued


def _validate_campaign_for_execution(campaign: SDROutboundCampaign) -> None:
    if "email" not in campaign.channels:
        raise OutboundCampaignExecutionError(
            "Enable the email channel before launching this campaign."
        )
    if not campaign.sequence_id:
        raise OutboundCampaignExecutionError(
            "Select an outbound nurture sequence before launching."
        )
    sequence = campaign.sequence
    if not sequence.is_active:
        raise OutboundCampaignExecutionError(
            "Enable the selected nurture sequence before launching."
        )
    if LeadIntakeSource.OUTBOUND not in sequence.sources:
        raise OutboundCampaignExecutionError(
            "The selected sequence must explicitly include the outbound source."
        )
    if not sequence.from_email:
        raise OutboundCampaignExecutionError(
            "Configure a sender email on the selected nurture sequence."
        )
    if not sequence.steps.exists():
        raise OutboundCampaignExecutionError(
            "Add at least one step to the selected nurture sequence."
        )


def _resume_campaign_enrollments(campaign: SDROutboundCampaign) -> int:
    resumed = 0
    enrollments = LeadNurtureEnrollment.objects.filter(
        org_id=campaign.org_id,
        intake__outbound_prospect__campaign=campaign,
        status=NurtureEnrollmentStatus.PAUSED,
        stop_reason=CAMPAIGN_PAUSE_REASON,
    )
    for enrollment in enrollments.iterator(chunk_size=100):
        resume_enrollment(enrollment)
        resumed += 1
    return resumed


def _parse_csv(csv_text: str) -> tuple[list[ParsedProspect], list[dict[str, Any]]]:
    if len(csv_text) > MAX_CSV_CHARS:
        raise OutboundImportError("CSV content exceeds the 1 MB limit.")
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        raise OutboundImportError("CSV is empty.")
    headers = [(value or "").strip().lower().lstrip("\ufeff") for value in rows[0]]
    if "company_name" not in headers:
        raise OutboundImportError("CSV must include the company_name header.")
    unknown = [value for value in headers if value and value not in CSV_HEADERS]
    if unknown:
        raise OutboundImportError(f"Unknown CSV headers: {', '.join(unknown)}.")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise OutboundImportError(
            f"CSV contains more than {MAX_IMPORT_ROWS} prospect rows."
        )

    parsed: list[ParsedProspect] = []
    errors: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(rows[1:], start=2):
        if not any((value or "").strip() for value in raw_row):
            continue
        values = {
            header: (raw_row[index].strip() if index < len(raw_row) else "")
            for index, header in enumerate(headers)
            if header
        }
        values = {header: values.get(header, "") for header in CSV_HEADERS}
        row_errors = _clean_and_validate(values)
        if row_errors:
            errors.extend(
                {"row": row_number, "field": field, "message": message}
                for field, message in row_errors
            )
            continue
        material = _dedupe_material(values)
        parsed.append(
            ParsedProspect(
                row_number=row_number,
                values=values,
                material=material,
                dedupe_key=hashlib.sha256(material.encode()).hexdigest(),
            )
        )
    return parsed, errors


def _clean_and_validate(values: dict[str, str]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    for key, value in list(values.items()):
        values[key] = re.sub(r"\s+", " ", value).strip() if key != "notes" else value.strip()
    values["email"] = values["email"].lower()
    values["phone"] = re.sub(r"\s+", " ", values["phone"])
    values["country"] = normalize_country(values["country"])
    for field in URL_FIELDS:
        if values[field] and "://" not in values[field]:
            values[field] = f"https://{values[field]}"

    if not values["company_name"]:
        errors.append(("company_name", "Company name is required."))
    if not any(
        values[field]
        for field in ("email", "phone", "linkedin_url", "website")
    ):
        errors.append(
            (
                "identity",
                "Add an email, phone, LinkedIn URL, or company website.",
            )
        )
    if values["email"]:
        try:
            validate_email(values["email"])
        except ValidationError:
            errors.append(("email", "Enter a valid email address."))
    validator = URLValidator(schemes=("http", "https"))
    for field in URL_FIELDS:
        if not values[field]:
            continue
        try:
            validator(values[field])
        except ValidationError:
            errors.append((field, "Enter a valid HTTP or HTTPS URL."))
    for field, limit in FIELD_LIMITS.items():
        if len(values[field]) > limit:
            errors.append((field, f"Must not exceed {limit} characters."))
    return errors


def _dedupe_material(values: Mapping[str, str]) -> str:
    if values["email"]:
        return f"email:{values['email'].casefold()}"
    if values["linkedin_url"]:
        return f"linkedin:{_normalized_url(values['linkedin_url'])}"
    if values["phone"]:
        return f"phone:{_normalized_phone(values['phone'])}"
    return (
        f"company:{_normalized_url(values['website'])}:"
        f"{values['company_name'].casefold()}"
    )


def _existing_crm_materials(
    *, org_id: UUID, parsed: list[ParsedProspect]
) -> set[str]:
    materials = {row.material for row in parsed}
    result: set[str] = set()
    emails = {
        material.removeprefix("email:")
        for material in materials
        if material.startswith("email:")
    }
    if emails:
        result.update(
            f"email:{value}"
            for value in Lead.objects.filter(org_id=org_id)
            .exclude(email__isnull=True)
            .annotate(normalized=Lower("email"))
            .filter(normalized__in=emails)
            .values_list("normalized", flat=True)
        )
    linkedin = {
        material.removeprefix("linkedin:")
        for material in materials
        if material.startswith("linkedin:")
    }
    phone = {
        material.removeprefix("phone:")
        for material in materials
        if material.startswith("phone:")
    }
    company = {material for material in materials if material.startswith("company:")}
    if linkedin or phone or company:
        leads = Lead.objects.filter(org_id=org_id).values(
            "linkedin_url",
            "phone",
            "website",
            "company_name",
        )
        for lead in leads:
            if lead["linkedin_url"]:
                material = f"linkedin:{_normalized_url(lead['linkedin_url'])}"
                if material.removeprefix("linkedin:") in linkedin:
                    result.add(material)
            if lead["phone"]:
                material = f"phone:{_normalized_phone(lead['phone'])}"
                if material.removeprefix("phone:") in phone:
                    result.add(material)
            if lead["website"] and lead["company_name"]:
                material = (
                    f"company:{_normalized_url(lead['website'])}:"
                    f"{lead['company_name'].casefold()}"
                )
                if material in company:
                    result.add(material)
    return result


def _candidate_from_prospect(prospect: SDROutboundProspect) -> LeadCandidate:
    return LeadCandidate(
        org_id=prospect.org_id,
        source=LeadSource.OUTBOUND,
        source_record_id=str(prospect.id),
        identity=LeadIdentity(
            first_name=prospect.first_name or None,
            last_name=prospect.last_name or None,
            email=prospect.email or None,
            phone=prospect.phone or None,
            linkedin_url=prospect.linkedin_url or None,
        ),
        company=CompanySnapshot(
            name=prospect.company_name,
            website=prospect.website or None,
            industry=prospect.industry or None,
            country=prospect.country or None,
        ),
        attributes={
            "job_title": prospect.job_title,
            "message": prospect.notes,
            "outbound_campaign_id": str(prospect.campaign_id),
            "outbound_campaign_name": prospect.campaign.name,
            "source_url": prospect.source_url,
        },
    )


def _raw_payload(prospect: SDROutboundProspect) -> dict[str, Any]:
    return {
        "prospect_id": str(prospect.id),
        "campaign_id": str(prospect.campaign_id),
        "campaign_name": prospect.campaign.name,
        **{
            field: getattr(prospect, field)
            for field in CSV_HEADERS
        },
    }


def _mark_failed(prospect: SDROutboundProspect, code: str, message: str) -> None:
    SDROutboundProspect.objects.filter(
        id=prospect.id,
        org_id=prospect.org_id,
    ).update(
        status=OutboundProspectStatus.FAILED,
        last_error_code=code,
        last_error_message=(message or code)[:1000],
    )


def _promotion_result(
    prospect: SDROutboundProspect,
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "prospect_id": str(prospect.id),
        "intake_id": str(prospect.intake_id) if prospect.intake_id else None,
        "lead_id": (
            str(prospect.intake.crm_lead_id)
            if prospect.intake_id and prospect.intake
            else None
        ),
        "status": prospect.status,
        "replayed": replayed,
    }


def _normalized_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def _normalized_url(value: str) -> str:
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = parsed.path.rstrip("/").casefold()
    return urlunsplit(("https", host, path, "", ""))


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Outbound prospect job %s was persisted but not dispatched", job.id)
