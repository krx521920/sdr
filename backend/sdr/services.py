"""Django application services for durable SDR intake processing."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from integrations.providers.website.adapter import WebsiteFormNormalizer
from sdr.adapters import DjangoCRMWriter, DjangoLeadDeduplicator, LeastLoadedSalesRouter
from sdr.application import LeadIntakePipeline
from sdr.domain import LeadCandidate
from sdr.enrichment import EmailDomainEnricher
from sdr.models import LeadIntake, LeadIntakeStatus
from sdr.scoring import RuleBasedLeadScorer


@dataclass(frozen=True, slots=True)
class WebsiteIntakeResult:
    intake_id: UUID
    lead_id: UUID | None
    crm_created: bool | None
    matched_existing: bool
    qualification_score: int | None
    qualification_band: str
    assigned_profile_id: UUID | None
    replayed: bool = False


class IntakeAlreadyProcessing(RuntimeError):
    def __init__(self, intake_id: UUID):
        super().__init__("lead intake is already processing")
        self.intake_id = intake_id


class IntakeProcessingFailed(RuntimeError):
    def __init__(self, intake_id: UUID):
        super().__init__("lead intake processing failed")
        self.intake_id = intake_id


PROCESSING_TIMEOUT = timedelta(minutes=5)


def process_website_intake(*, org_id: UUID, payload: Mapping[str, Any]):
    normalizer = WebsiteFormNormalizer()
    candidate = normalizer.normalize(org_id=org_id, payload=payload)
    intake, replayed = _claim_intake(candidate=candidate, raw_payload=payload)
    if replayed:
        return _result_from_intake(intake, replayed=True)

    pipeline = LeadIntakePipeline(
        deduplicator=DjangoLeadDeduplicator(),
        enricher=EmailDomainEnricher(),
        scorer=RuleBasedLeadScorer(),
        router=LeastLoadedSalesRouter(),
        writer=DjangoCRMWriter(),
    )
    try:
        result = pipeline.process(candidate)
    except Exception as exc:
        LeadIntake.objects.filter(id=intake.id, org_id=org_id).update(
            status=LeadIntakeStatus.FAILED,
            error_message=str(exc)[:2000],
        )
        raise IntakeProcessingFailed(intake.id) from exc

    LeadIntake.objects.filter(id=intake.id, org_id=org_id).update(
        normalized_payload=_normalized_payload(result.crm.lead_id, result.candidate),
        status=LeadIntakeStatus.COMPLETED,
        error_message="",
        qualification_score=result.qualification.score,
        qualification_band=result.qualification.band.value,
        matched_existing=result.existing_lead_id is not None,
        crm_created=result.crm.created,
        crm_lead_id=result.crm.lead_id,
        assigned_profile_id=result.assignment.profile_id,
        processed_at=timezone.now(),
    )
    intake.refresh_from_db()
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

        if not created and intake.status == LeadIntakeStatus.COMPLETED:
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
    return WebsiteIntakeResult(
        intake_id=intake.id,
        lead_id=intake.crm_lead_id,
        crm_created=intake.crm_created,
        matched_existing=intake.matched_existing,
        qualification_score=intake.qualification_score,
        qualification_band=intake.qualification_band,
        assigned_profile_id=intake.assigned_profile_id,
        replayed=replayed,
    )
