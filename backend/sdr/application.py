"""Application service that executes one normalized SDR lead pipeline."""

from dataclasses import dataclass

from sdr.domain import HandoffPackage, LeadCandidate, PipelineResult
from sdr.ports import (
    CRMWriterPort,
    DeduplicationPort,
    EnrichmentPort,
    RoutingPort,
    ScoringPort,
)


@dataclass(slots=True)
class LeadIntakePipeline:
    deduplicator: DeduplicationPort
    enricher: EnrichmentPort
    scorer: ScoringPort
    router: RoutingPort
    writer: CRMWriterPort

    def process(self, candidate: LeadCandidate) -> PipelineResult:
        existing_lead_id = self.deduplicator.find_existing(candidate)
        enriched_candidate = self.enricher.enrich(candidate)
        qualification = self.scorer.score(enriched_candidate)
        assignment = self.router.route(enriched_candidate, qualification)
        package = HandoffPackage(
            candidate=enriched_candidate,
            qualification=qualification,
            assignment=assignment,
            existing_lead_id=existing_lead_id,
        )
        crm_result = self.writer.write_handoff(package)
        return PipelineResult(
            candidate=enriched_candidate,
            crm=crm_result,
            qualification=qualification,
            assignment=assignment,
            existing_lead_id=existing_lead_id,
        )
