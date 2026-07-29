"""Framework-independent values passed through the SDR pipeline."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID


class LeadSource(StrEnum):
    FACEBOOK_AD = "facebook_ad"
    WEBSITE_FORM = "website_form"
    LINKEDIN = "linkedin"
    EMAIL = "email"
    API = "api"
    MANUAL = "manual"


class QualificationBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    DISQUALIFIED = "disqualified"


@dataclass(frozen=True, slots=True)
class LeadIdentity:
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None


@dataclass(frozen=True, slots=True)
class CompanySnapshot:
    name: str | None = None
    website: str | None = None
    industry: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class LeadCandidate:
    """A normalized lead before it is written to the CRM."""

    org_id: UUID
    source: LeadSource
    source_record_id: str
    identity: LeadIdentity
    company: CompanySnapshot = field(default_factory=CompanySnapshot)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class IngestionBatch:
    records: tuple[LeadCandidate, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class QualificationResult:
    score: int
    band: QualificationBand
    reasons: tuple[str, ...] = ()
    model_version: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 100:
            raise ValueError("qualification score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class AssignmentDecision:
    profile_id: UUID | None = None
    team_id: UUID | None = None
    rule_id: UUID | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HandoffPackage:
    candidate: LeadCandidate
    qualification: QualificationResult
    assignment: AssignmentDecision
    existing_lead_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CRMWriteResult:
    lead_id: UUID
    created: bool
    contact_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    candidate: LeadCandidate
    crm: CRMWriteResult
    qualification: QualificationResult
    assignment: AssignmentDecision
    existing_lead_id: UUID | None = None
