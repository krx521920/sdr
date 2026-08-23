"""Django application services for durable SDR intake processing."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from sdr.adapters import DjangoCRMWriter, DjangoLeadDeduplicator
from sdr.application import LeadIntakePipeline
from sdr.compliance import ensure_intake_provenance, intake_data_restriction
from sdr.domain import LeadCandidate
from sdr.intelligence.service import LeadInspector
from sdr.models import LeadIntake, LeadIntakeStatus, LeadLifecycleEventType
from sdr.nurture import auto_enroll_intake
from sdr.response import (
    record_lifecycle_event,
    schedule_post_handoff_jobs,
)
from sdr.routing import RuleBasedSalesRouter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LeadIntakeResult:
    intake_id: UUID
    lead_id: UUID | None
    crm_created: bool | None
    matched_existing: bool
    qualification_score: int | None
    qualification_band: str
    assigned_profile_id: UUID | None
    routing_rule_id: UUID | None
    routing_reason: str
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class LeadIntakeAcceptance:
    intake_id: UUID
    status: str
    lead_id: UUID | None
    replayed: bool


class IntakeAlreadyProcessing(RuntimeError):
    def __init__(self, intake_id: UUID):
        super().__init__("lead intake is already processing")
        self.intake_id = intake_id


class IntakeProcessingFailed(RuntimeError):
    def __init__(self, intake_id: UUID):
        super().__init__("lead intake processing failed")
        self.intake_id = intake_id


PROCESSING_TIMEOUT = timedelta(minutes=5)


def accept_candidate_intake(
    *, candidate: LeadCandidate, raw_payload: Mapping[str, Any]
) -> LeadIntakeAcceptance:
    """Persist an inbound lead before scheduling any external work."""

    lookup = {
        "org_id": candidate.org_id,
        "source": candidate.source.value,
        "source_record_id": candidate.source_record_id,
    }
    with transaction.atomic():
        try:
            intake = LeadIntake.objects.select_for_update().get(**lookup)
            created = False
        except LeadIntake.DoesNotExist:
            try:
                with transaction.atomic():
                    intake = LeadIntake.objects.create(
                        **lookup,
                        raw_payload=dict(raw_payload),
                    )
                created = True
            except IntegrityError:
                intake = LeadIntake.objects.select_for_update().get(**lookup)
                created = False

        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.RECEIVED,
            event_key="received",
            data={"source": candidate.source.value},
        )
        ensure_intake_provenance(
            intake=intake,
            candidate=candidate,
            raw_payload=raw_payload,
        )
        return LeadIntakeAcceptance(
            intake_id=intake.id,
            status=intake.status,
            lead_id=intake.crm_lead_id,
            replayed=not created,
        )


def process_candidate_intake(
    *, candidate: LeadCandidate, raw_payload: Mapping[str, Any]
) -> LeadIntakeResult:
    """Run any normalized provider lead through the durable SDR pipeline."""
    intake, replayed = _claim_intake(candidate=candidate, raw_payload=raw_payload)
    if intake_data_restriction(intake):
        return _result_from_intake(intake, replayed=True)
    ensure_intake_provenance(
        intake=intake,
        candidate=candidate,
        raw_payload=raw_payload,
    )
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.RECEIVED,
        event_key="received",
        data={"source": candidate.source.value},
    )
    if replayed:
        try:
            schedule_post_handoff_jobs(intake)
        except Exception:
            logger.exception(
                "Could not reconcile response jobs for intake %s", intake.id
            )
        try:
            auto_enroll_intake(intake)
        except Exception:
            logger.exception(
                "Could not reconcile nurture enrollment for intake %s", intake.id
            )
        return _result_from_intake(intake, replayed=True)

    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.PROCESSING,
        event_key=f"processing:{intake.attempt_count}",
        data={"attempt": intake.attempt_count},
    )

    inspector = LeadInspector.for_intake(intake)
    pipeline = LeadIntakePipeline(
        deduplicator=DjangoLeadDeduplicator(),
        enricher=inspector,
        scorer=inspector,
        router=RuleBasedSalesRouter(),
        writer=DjangoCRMWriter(),
    )
    try:
        result = pipeline.process(candidate)
        inspector.complete(result.qualification)
    except Exception as exc:
        inspector.fail(exc)
        LeadIntake.objects.filter(id=intake.id, org_id=candidate.org_id).update(
            status=LeadIntakeStatus.FAILED,
            error_message=str(exc)[:2000],
        )
        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.FAILED,
            event_key=f"failed:{intake.attempt_count}",
            data={"error": (str(exc) or exc.__class__.__name__)[:500]},
        )
        raise IntakeProcessingFailed(intake.id) from exc

    LeadIntake.objects.filter(id=intake.id, org_id=candidate.org_id).update(
        normalized_payload=_normalized_payload(result.crm.lead_id, result.candidate),
        status=LeadIntakeStatus.COMPLETED,
        error_message="",
        qualification_score=result.qualification.score,
        qualification_band=result.qualification.band.value,
        matched_existing=result.existing_lead_id is not None,
        crm_created=result.crm.created,
        crm_lead_id=result.crm.lead_id,
        assigned_profile_id=result.assignment.profile_id,
        routing_rule_id=result.assignment.rule_id,
        routing_reason=result.assignment.reason,
        processed_at=timezone.now(),
    )
    intake.refresh_from_db()
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.QUALIFIED,
        event_key="qualified",
        data={
            "score": result.qualification.score,
            "band": result.qualification.band.value,
        },
    )
    if result.assignment.profile_id:
        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.ASSIGNED,
            event_key="assigned",
            data={
                "profile_id": str(result.assignment.profile_id),
                "routing_rule_id": (
                    str(result.assignment.rule_id)
                    if result.assignment.rule_id
                    else None
                ),
            },
        )
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.CRM_HANDOFF,
        event_key="crm_handoff",
        data={
            "lead_id": str(result.crm.lead_id),
            "created": result.crm.created,
        },
    )
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.COMPLETED,
        event_key="completed",
    )
    try:
        schedule_post_handoff_jobs(intake)
    except Exception:
        logger.exception("Could not schedule response jobs for intake %s", intake.id)
    try:
        auto_enroll_intake(intake)
    except Exception:
        logger.exception("Could not schedule nurture for intake %s", intake.id)
    return _result_from_intake(intake)


def _claim_intake(*, candidate: LeadCandidate, raw_payload: Mapping[str, Any]):
    lookup = {
        "org_id": candidate.org_id,
        "source": candidate.source.value,
        "source_record_id": candidate.source_record_id,
    }
    with transaction.atomic():
        try:
            intake = LeadIntake.objects.select_for_update().get(**lookup)
            created = False
        except LeadIntake.DoesNotExist:
            try:
                with transaction.atomic():
                    intake = LeadIntake.objects.create(
                        **lookup,
                        raw_payload=dict(raw_payload),
                    )
                created = True
            except IntegrityError:
                intake = LeadIntake.objects.select_for_update().get(**lookup)
                created = False

        if not created and (
            intake.status == LeadIntakeStatus.COMPLETED
            or intake_data_restriction(intake)
        ):
            return intake, True
        if (
            not created
            and intake.status == LeadIntakeStatus.PROCESSING
            and intake.updated_at > timezone.now() - PROCESSING_TIMEOUT
        ):
            raise IntakeAlreadyProcessing(intake.id)

        intake.raw_payload = dict(raw_payload)
        intake.status = LeadIntakeStatus.PROCESSING
        intake.error_message = ""
        intake.attempt_count += 1
        intake.save(
            update_fields=[
                "raw_payload",
                "status",
                "error_message",
                "attempt_count",
                "updated_at",
            ]
        )
        return intake, False


def _normalized_payload(lead_id: UUID, candidate: LeadCandidate) -> dict[str, Any]:
    return {
        "lead_id": str(lead_id),
        "source": candidate.source.value,
        "source_record_id": candidate.source_record_id,
        "identity": {
            "first_name": candidate.identity.first_name,
            "last_name": candidate.identity.last_name,
            "email": candidate.identity.email,
            "phone": candidate.identity.phone,
            "linkedin_url": candidate.identity.linkedin_url,
        },
        "company": {
            "name": candidate.company.name,
            "website": candidate.company.website,
            "industry": candidate.company.industry,
            "country": candidate.company.country,
        },
        "attributes": dict(candidate.attributes),
    }


def _result_from_intake(intake: LeadIntake, *, replayed: bool = False):
    return LeadIntakeResult(
        intake_id=intake.id,
        lead_id=intake.crm_lead_id,
        crm_created=intake.crm_created,
        matched_existing=intake.matched_existing,
        qualification_score=intake.qualification_score,
        qualification_band=intake.qualification_band,
        assigned_profile_id=intake.assigned_profile_id,
        routing_rule_id=intake.routing_rule_id,
        routing_reason=intake.routing_reason,
        replayed=replayed,
    )
