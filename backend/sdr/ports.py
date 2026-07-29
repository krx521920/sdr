"""Stable interfaces implemented by CRM and provider adapters."""

from typing import Protocol
from uuid import UUID

from sdr.domain import (
    AssignmentDecision,
    HandoffPackage,
    IngestionBatch,
    LeadCandidate,
    QualificationResult,
)


class LeadSourcePort(Protocol):
    def fetch(self, *, org_id: UUID, cursor: str | None = None) -> IngestionBatch: ...


class DeduplicationPort(Protocol):
    def find_existing(self, candidate: LeadCandidate) -> UUID | None: ...


class EnrichmentPort(Protocol):
    def enrich(self, candidate: LeadCandidate) -> LeadCandidate: ...


class ScoringPort(Protocol):
    def score(self, candidate: LeadCandidate) -> QualificationResult: ...


class RoutingPort(Protocol):
    def route(
        self, candidate: LeadCandidate, qualification: QualificationResult
    ) -> AssignmentDecision: ...


class CRMWriterPort(Protocol):
    def write_handoff(self, package: HandoffPackage) -> UUID: ...
