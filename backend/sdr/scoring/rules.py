"""Explainable baseline scoring before tenant-specific or LLM rules."""

from sdr.domain import LeadCandidate, QualificationBand, QualificationResult


class RuleBasedLeadScorer:
    def score(self, candidate: LeadCandidate) -> QualificationResult:
        score = 0
        reasons: list[str] = []

        def add(points: int, reason: str) -> None:
            nonlocal score
            score += points
            reasons.append(reason)

        if candidate.identity.email:
            add(20, "email provided")
        if candidate.attributes.get("is_business_email"):
            add(10, "business email domain")
        if candidate.identity.phone:
            add(15, "phone provided")
        if candidate.company.name:
            add(15, "company provided")
        if candidate.company.website:
            add(10, "company website available")
        if candidate.attributes.get("job_title"):
            add(10, "job title provided")
        if candidate.identity.linkedin_url:
            add(10, "LinkedIn profile provided")
        if len(str(candidate.attributes.get("message", "")).strip()) >= 20:
            add(10, "detailed inquiry provided")

        score = min(score, 100)
        if score >= 70:
            band = QualificationBand.HIGH
        elif score >= 40:
            band = QualificationBand.MEDIUM
        elif score >= 20:
            band = QualificationBand.LOW
        else:
            band = QualificationBand.DISQUALIFIED

        return QualificationResult(
            score=score,
            band=band,
            reasons=tuple(reasons),
            model_version="rules-v1",
        )
