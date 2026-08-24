"""Deterministic, evidence-linked person-to-opportunity matching."""

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from matching.models import (
    Evidence,
    EvidenceKind,
    Match,
    MatchEvidence,
    MatchEvidenceDirection,
    MatchOpportunity,
    Person,
)

ENGINE_VERSION = "rules-v1"
MAX_SYNC_RECOMPUTE_PEOPLE = 100
SUPPORTED_DIMENSIONS = ("skills", "titles", "locations", "availability")
FACT_ALIASES = {
    "skills": ("skills", "skill"),
    "titles": ("titles", "title", "roles", "role"),
    "locations": ("locations", "location"),
    "availability": ("availability", "availability_status"),
}


def _text(value):
    return " ".join(str(value or "").strip().lower().split())


def _values(value):
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return list(dict.fromkeys(item for item in (_text(item) for item in value) if item))


def _fact_values(facts, dimension):
    if not isinstance(facts, dict):
        return []
    values = []
    for key in FACT_ALIASES[dimension]:
        values.extend(_values(facts.get(key)))
    return list(dict.fromkeys(values))


def _is_match(candidate, target, dimension):
    if candidate == target:
        return True
    if dimension in {"titles", "locations"}:
        return target in candidate or candidate in target
    return False


def _matched_targets(candidates, targets, dimension):
    return [
        target
        for target in targets
        if any(_is_match(candidate, target, dimension) for candidate in candidates)
    ]


def _clamp_score(value):
    return max(0, min(100, int(round(value))))


@dataclass(frozen=True)
class MatchEvaluation:
    defaults: dict
    evidence_contributions: tuple


def evaluate_person(person, opportunity, evidence_items=None):
    """Return a reproducible assessment using only stored structured facts."""

    now = timezone.now()
    if evidence_items is None:
        evidence_items = list(
            Evidence.objects.filter(
                org=person.org,
                person=person,
                observed_at__lte=now,
            ).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=now))
        )
    else:
        evidence_items = [
            item
            for item in evidence_items
            if item.observed_at <= now
            and (item.valid_until is None or item.valid_until >= now)
        ]

    candidate = {
        "skills": _values(person.skills),
        "titles": _values([person.current_title, *list(person.roles or [])]),
        "locations": _values(person.location),
        "availability": _values(person.availability),
    }
    evidence_dimensions = {}
    for evidence in evidence_items:
        dimensions = {}
        for dimension in SUPPORTED_DIMENSIONS:
            values = _fact_values(evidence.facts, dimension)
            dimensions[dimension] = values
            candidate[dimension] = list(dict.fromkeys([*candidate[dimension], *values]))
        evidence_dimensions[evidence.id] = dimensions

    required = opportunity.required_criteria or {}
    preferred = opportunity.preferred_criteria or {}
    exclusions = opportunity.exclusion_criteria or {}
    weights = opportunity.scoring_weights or {}

    reasons = []
    gaps = []
    breakdown = {}
    required_total = 0
    required_matched = 0
    fit_numerator = 0.0
    fit_denominator = 0.0
    evidence_support = {}

    for dimension in SUPPORTED_DIMENSIONS:
        required_targets = _values(required.get(dimension))
        preferred_targets = _values(preferred.get(dimension))
        excluded_targets = _values(exclusions.get(dimension))
        required_hits = _matched_targets(
            candidate[dimension], required_targets, dimension
        )
        preferred_hits = _matched_targets(
            candidate[dimension], preferred_targets, dimension
        )
        excluded_hits = _matched_targets(
            candidate[dimension], excluded_targets, dimension
        )

        required_total += len(required_targets)
        required_matched += len(required_hits)
        desired = list(dict.fromkeys([*required_targets, *preferred_targets]))
        hits = list(dict.fromkeys([*required_hits, *preferred_hits]))
        weight = max(0.0, float(weights.get(dimension, 0) or 0))
        if desired and weight:
            fit_numerator += weight * (len(hits) / len(desired))
            fit_denominator += weight

        missing = [target for target in required_targets if target not in required_hits]
        if hits:
            reasons.append(
                {
                    "dimension": dimension,
                    "matched": hits,
                    "message": f"Matched {dimension}: {', '.join(hits)}",
                }
            )
        if missing:
            gaps.append(
                {
                    "dimension": dimension,
                    "missing": missing,
                    "message": f"Missing required {dimension}: {', '.join(missing)}",
                }
            )
        if excluded_hits:
            gaps.append(
                {
                    "dimension": dimension,
                    "excluded": excluded_hits,
                    "message": f"Matched exclusion {dimension}: {', '.join(excluded_hits)}",
                }
            )

        breakdown[dimension] = {
            "required": required_targets,
            "preferred": preferred_targets,
            "excluded": excluded_targets,
            "matched": hits,
            "missing_required": missing,
            "matched_exclusions": excluded_hits,
            "weight": weight,
        }

        for evidence in evidence_items:
            supported = _matched_targets(
                evidence_dimensions[evidence.id][dimension], hits, dimension
            )
            if supported:
                entry = evidence_support.setdefault(
                    evidence.id,
                    {"evidence": evidence, "dimensions": {}, "terms": 0},
                )
                entry["dimensions"][dimension] = supported
                entry["terms"] += len(supported)

    eligibility_score = (
        100 if required_total == 0 else 100 * required_matched / required_total
    )
    fit_score = 0 if fit_denominator == 0 else 100 * fit_numerator / fit_denominator

    confidences = [float(item.confidence) for item in evidence_items]
    trust_score = 100 * sum(confidences) / len(confidences) if confidences else 25
    verified = [
        item for item in evidence_items if item.kind == EvidenceKind.VERIFICATION
    ]
    if verified:
        trust_score = min(100, trust_score + 10)

    relationship_confidences = [
        float(item.confidence)
        for item in evidence_items
        if item.kind in {EvidenceKind.RELATIONSHIP, EvidenceKind.INTERACTION}
    ]
    relationship_score = (
        100 * sum(relationship_confidences) / len(relationship_confidences)
        if relationship_confidences
        else 0
    )

    requested_availability = _values(
        [
            *_values(required.get("availability")),
            *_values(preferred.get("availability")),
        ]
    )
    if requested_availability:
        availability_score = (
            100
            * len(
                _matched_targets(
                    candidate["availability"], requested_availability, "availability"
                )
            )
            / len(requested_availability)
        )
    else:
        availability_score = {
            "available": 100,
            "open_to_offers": 90,
            "unknown": 40,
            "busy": 20,
            "unavailable": 0,
        }.get(_text(person.availability), 40)

    exclusion_hit = any(item["matched_exclusions"] for item in breakdown.values())
    overall_score = (
        0.50 * fit_score
        + 0.15 * eligibility_score
        + 0.15 * trust_score
        + 0.10 * relationship_score
        + 0.10 * availability_score
    )
    if required_matched < required_total:
        overall_score = min(overall_score, 49)
    if exclusion_hit:
        overall_score = 0
        eligibility_score = 0

    average_confidence = sum(confidences) / len(confidences) if confidences else 0
    confidence = min(
        1, 0.20 + average_confidence * 0.55 + min(len(confidences), 5) * 0.05
    )

    contributions = []
    total_supported_terms = (
        sum(item["terms"] for item in evidence_support.values()) or 1
    )
    for support in evidence_support.values():
        evidence = support["evidence"]
        relevance = min(1, support["terms"] / total_supported_terms)
        contribution = overall_score * relevance * float(evidence.confidence)
        explanations = [
            f"{dimension}: {', '.join(terms)}"
            for dimension, terms in support["dimensions"].items()
        ]
        contributions.append(
            {
                "evidence": evidence,
                "direction": MatchEvidenceDirection.POSITIVE,
                "relevance": Decimal(str(round(relevance, 3))),
                "contribution": Decimal(str(round(contribution, 2))),
                "explanation": "; ".join(explanations)[:500],
            }
        )

    return MatchEvaluation(
        defaults={
            "overall_score": _clamp_score(overall_score),
            "eligibility_score": _clamp_score(eligibility_score),
            "fit_score": _clamp_score(fit_score),
            "trust_score": _clamp_score(trust_score),
            "relationship_score": _clamp_score(relationship_score),
            "availability_score": _clamp_score(availability_score),
            "confidence": Decimal(str(round(confidence, 3))),
            "reasons": reasons,
            "gaps": gaps,
            "score_breakdown": breakdown,
            "engine_version": ENGINE_VERSION,
            "model_provider": "",
            "model_name": "",
            "evaluated_at": now,
        },
        evidence_contributions=tuple(contributions),
    )


@transaction.atomic
def recompute_opportunity_matches(*, org, opportunity, people=None):
    """Upsert assessments, citations, and stable ranks for one opportunity."""

    if opportunity.org_id != org.id:
        raise ValueError("Opportunity does not belong to the requested org")
    opportunity = MatchOpportunity.objects.select_for_update().get(
        org=org,
        id=opportunity.id,
    )
    queryset = (
        Person.objects.filter(org=org, status="active")
        if people is None
        else people.filter(org=org, status="active")
    )
    people_list = list(
        queryset.order_by("display_name", "id")[: MAX_SYNC_RECOMPUTE_PEOPLE + 1]
    )
    if len(people_list) > MAX_SYNC_RECOMPUTE_PEOPLE:
        raise ValueError(
            f"Synchronous recompute is limited to {MAX_SYNC_RECOMPUTE_PEOPLE} people."
        )
    evidence_by_person = {}
    now = timezone.now()
    evidence_queryset = Evidence.objects.filter(
        org=org,
        person_id__in=[person.id for person in people_list],
        observed_at__lte=now,
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=now))
    for evidence in evidence_queryset:
        evidence_by_person.setdefault(evidence.person_id, []).append(evidence)

    matches = []
    for person in people_list:
        evaluation = evaluate_person(
            person,
            opportunity,
            evidence_by_person.get(person.id, []),
        )
        match, _ = Match.objects.update_or_create(
            org=org,
            person=person,
            opportunity=opportunity,
            defaults=evaluation.defaults,
        )
        MatchEvidence.objects.filter(org=org, match=match).delete()
        MatchEvidence.objects.bulk_create(
            [
                MatchEvidence(org=org, match=match, **contribution)
                for contribution in evaluation.evidence_contributions
            ]
        )
        matches.append(match)

    globally_ranked = sorted(
        Match.objects.filter(org=org, opportunity=opportunity).select_related("person"),
        key=lambda item: (
            -item.overall_score,
            -float(item.confidence),
            str(item.person_id),
        ),
    )
    for rank, match in enumerate(globally_ranked, start=1):
        match.rank = rank
    Match.objects.bulk_update(globally_ranked, ["rank"])
    refreshed_ranks = {match.id: match.rank for match in globally_ranked}
    for match in matches:
        match.rank = refreshed_ranks[match.id]
    return sorted(matches, key=lambda item: item.rank)
