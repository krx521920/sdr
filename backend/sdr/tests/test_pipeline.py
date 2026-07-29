from dataclasses import replace
from uuid import uuid4

from sdr.application import LeadIntakePipeline
from sdr.domain import (
    AssignmentDecision,
    CompanySnapshot,
    CRMWriteResult,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)


def test_pipeline_runs_in_required_order_and_preserves_duplicate():
    calls: list[str] = []
    existing_lead_id = uuid4()
    assigned_profile_id = uuid4()
    written_lead_id = uuid4()

    class Deduplicator:
        def find_existing(self, candidate):
            calls.append("deduplicate")
            return existing_lead_id

    class Enricher:
        def enrich(self, candidate):
            calls.append("enrich")
            return replace(candidate, company=CompanySnapshot(name="Acme"))

    class Scorer:
        def score(self, candidate):
            calls.append("score")
            assert candidate.company.name == "Acme"
            return QualificationResult(80, QualificationBand.HIGH)

    class Router:
        def route(self, candidate, qualification):
            calls.append("route")
            return AssignmentDecision(profile_id=assigned_profile_id)

    class Writer:
        def write_handoff(self, package):
            calls.append("handoff")
            assert package.existing_lead_id == existing_lead_id
            return CRMWriteResult(lead_id=written_lead_id, created=False)

    pipeline = LeadIntakePipeline(
        deduplicator=Deduplicator(),
        enricher=Enricher(),
        scorer=Scorer(),
        router=Router(),
        writer=Writer(),
    )
    result = pipeline.process(
        LeadCandidate(
            org_id=uuid4(),
            source=LeadSource.WEBSITE_FORM,
            source_record_id="form-1",
            identity=LeadIdentity(email="buyer@example.com"),
        )
    )

    assert calls == ["deduplicate", "enrich", "score", "route", "handoff"]
    assert result.existing_lead_id == existing_lead_id
    assert result.crm.lead_id == written_lead_id
    assert result.assignment.profile_id == assigned_profile_id
