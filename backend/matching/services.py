"""Deterministic, evidence-linked person-to-opportunity matching."""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from matching.models import (
    Evidence,
    EvidenceKind,
    Match,
    MatchEvidence,
    MatchEvidenceDirection,
    MatchOpportunity,
    Person,
)

logger = logging.getLogger(__name__)

ENGINE_VERSION = "rules-v1"
MAX_SYNC_RECOMPUTE_PEOPLE = 100
MAX_ASYNC_RECOMPUTE_PEOPLE = 500
RECOMPUTE_JOB_NAME = "matching.recompute_opportunity"
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


class RecomputeEnqueueError(ValueError):
    """A safe validation error raised before a matching job is queued."""


class RecomputeIdempotencyConflict(RecomputeEnqueueError):
    """The same idempotency key was reused for a different request."""


class RecomputeTargetNotFound(ValueError):
    """The queued matching target no longer exists in the requested tenant."""


class RecomputeSnapshotChanged(ValueError):
    """The queued candidate snapshot is no longer safe to evaluate."""


def _request_hash(*, opportunity_id: UUID, person_ids: list[UUID] | None) -> str:
    payload = json.dumps(
        {
            "opportunity_id": str(opportunity_id),
            "scope": "all_active" if person_ids is None else "selected",
            "person_ids": (
                []
                if person_ids is None
                else [str(person_id) for person_id in person_ids]
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception(
            "Matching recompute job %s was persisted but not dispatched", job.id
        )


@transaction.atomic
def enqueue_opportunity_recompute(
    *,
    org,
    opportunity,
    requested_by,
    person_ids,
    idempotency_key: UUID,
):
    """Persist an idempotent matching run and publish it after commit."""

    from matching.models import MatchRun

    if opportunity.org_id != org.id:
        raise RecomputeEnqueueError("Opportunity does not belong to this org.")

    people = Person.objects.filter(org=org, status="active")
    requested_ids = None
    if person_ids is not None:
        requested_ids = list(dict.fromkeys(person_ids))
        people = people.filter(id__in=requested_ids)
    resolved_ids = list(
        people.order_by("id").values_list("id", flat=True)[
            : MAX_ASYNC_RECOMPUTE_PEOPLE + 1
        ]
    )
    if requested_ids is not None and len(resolved_ids) != len(requested_ids):
        raise RecomputeEnqueueError(
            "Every requested person must be active and belong to this org."
        )
    if len(resolved_ids) > MAX_ASYNC_RECOMPUTE_PEOPLE:
        raise RecomputeEnqueueError(
            f"A recompute run cannot include more than {MAX_ASYNC_RECOMPUTE_PEOPLE} people."
        )

    request_hash = _request_hash(
        opportunity_id=opportunity.id,
        person_ids=None if requested_ids is None else resolved_ids,
    )
    proposed_run_id = uuid.uuid4()
    enqueued = enqueue_job(
        JobRequest(
            org_id=org.id,
            name=RECOMPUTE_JOB_NAME,
            idempotency_key=(
                f"matching-recompute:{opportunity.id}:{idempotency_key}"
            ),
            payload={
                "schema_version": 1,
                "org_id": str(org.id),
                "run_id": str(proposed_run_id),
                "opportunity_id": str(opportunity.id),
                "request_hash": request_hash,
            },
            max_attempts=3,
        )
    )
    run, _ = MatchRun.objects.get_or_create(
        automation_job=enqueued.job,
        defaults={
            "id": proposed_run_id,
            "org": org,
            "opportunity": opportunity,
            "requested_by": requested_by,
            "request_hash": request_hash,
            "requested_person_ids": [str(person_id) for person_id in resolved_ids],
            "total_count": len(resolved_ids),
            "engine_version": ENGINE_VERSION,
        },
    )
    if run.request_hash != request_hash:
        raise RecomputeIdempotencyConflict(
            "This Idempotency-Key was already used for a different recompute request."
        )
    transaction.on_commit(lambda: _safe_dispatch(enqueued.job))
    return run


def _run_result(run, *, reason=""):
    result = {
        "run_id": str(run.id),
        "opportunity_id": str(run.opportunity_id),
        "status": run.outcome or "succeeded",
        "evaluated_count": run.processed_count,
        "result_count": run.result_count or 0,
        "ranking_revision": run.ranking_revision,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
    if reason:
        result["reason"] = reason
    return result


@transaction.atomic
def execute_opportunity_recompute(
    *,
    org_id: UUID,
    opportunity_id: UUID,
    run_id: UUID,
    request_hash: str,
):
    """Execute one run while serializing ranking writes per opportunity."""

    from matching.models import MatchOpportunityStatus, MatchRun

    opportunity = (
        MatchOpportunity.objects.select_for_update()
        .filter(org_id=org_id, id=opportunity_id)
        .first()
    )
    if opportunity is None:
        raise RecomputeTargetNotFound("The matching opportunity no longer exists.")
    run = (
        MatchRun.objects.select_for_update()
        .filter(
            org_id=org_id,
            id=run_id,
            opportunity_id=opportunity_id,
            request_hash=request_hash,
        )
        .first()
    )
    if run is None:
        raise RecomputeTargetNotFound("The matching run no longer exists.")
    if run.completed_at is not None:
        return _run_result(run)

    if opportunity.status in {
        MatchOpportunityStatus.PAUSED,
        MatchOpportunityStatus.FILLED,
        MatchOpportunityStatus.CLOSED,
    }:
        run.processed_count = 0
        run.result_count = 0
        run.outcome = "skipped"
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "processed_count",
                "result_count",
                "outcome",
                "completed_at",
                "updated_at",
            ]
        )
        return _run_result(run, reason=f"opportunity_{opportunity.status}")

    people = Person.objects.filter(
        org_id=org_id,
        status="active",
        id__in=run.requested_person_ids,
    )
    resolved_person_ids = {
        str(person_id) for person_id in people.values_list("id", flat=True)
    }
    if resolved_person_ids != set(run.requested_person_ids):
        raise RecomputeSnapshotChanged(
            "The queued candidate snapshot changed before execution."
        )

    next_revision = opportunity.ranking_revision + 1
    opportunity.ranking_revision = next_revision
    opportunity.save(update_fields=["ranking_revision", "updated_at"])
    run.ranking_revision = next_revision
    run.engine_version = ENGINE_VERSION
    run.save(update_fields=["ranking_revision", "engine_version", "updated_at"])
    matches = recompute_opportunity_matches(
        org=opportunity.org,
        opportunity=opportunity,
        people=people,
        run=run,
        ranking_revision=next_revision,
        max_people=MAX_ASYNC_RECOMPUTE_PEOPLE,
    )
    run.processed_count = len(matches)
    run.result_count = len(matches)
    run.outcome = "succeeded"
    run.completed_at = timezone.now()
    run.save(
        update_fields=[
            "processed_count",
            "result_count",
            "outcome",
            "completed_at",
            "updated_at",
        ]
    )
    return _run_result(run)


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
def recompute_opportunity_matches(
    *,
    org,
    opportunity,
    people=None,
    run=None,
    ranking_revision=None,
    max_people=MAX_SYNC_RECOMPUTE_PEOPLE,
):
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
    people_list = list(queryset.order_by("display_name", "id")[: max_people + 1])
    if len(people_list) > max_people:
        raise ValueError(f"Recompute is limited to {max_people} people.")
    previous_ranks = dict(
        Match.objects.filter(org=org, opportunity=opportunity).values_list(
            "id", "rank"
        )
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
    evaluations = {}
    for person in people_list:
        evaluation = evaluate_person(
            person,
            opportunity,
            evidence_by_person.get(person.id, []),
        )
        defaults = dict(evaluation.defaults)
        if ranking_revision is not None:
            defaults["ranking_revision"] = ranking_revision
        match, _ = Match.objects.update_or_create(
            org=org,
            person=person,
            opportunity=opportunity,
            defaults=defaults,
        )
        MatchEvidence.objects.filter(org=org, match=match).delete()
        MatchEvidence.objects.bulk_create(
            [
                MatchEvidence(org=org, match=match, **contribution)
                for contribution in evaluation.evidence_contributions
            ]
        )
        matches.append(match)
        evaluations[match.id] = evaluation

    globally_ranked = sorted(
        Match.objects.filter(org=org, opportunity=opportunity).select_related("person"),
        key=lambda item: (
            -item.overall_score,
            -float(item.confidence),
            str(item.person_id),
        ),
    )
    evaluated_match_ids = set(evaluations)
    revision_matches = []
    for rank, match in enumerate(globally_ranked, start=1):
        previous_rank = previous_ranks.get(match.id)
        match.rank = rank
        if ranking_revision is not None and (
            match.id in evaluated_match_ids or previous_rank != rank
        ):
            match.ranking_revision = ranking_revision
            revision_matches.append(match)
    update_fields = ["rank"]
    if ranking_revision is not None:
        update_fields.append("ranking_revision")
    Match.objects.bulk_update(globally_ranked, update_fields)
    refreshed_ranks = {match.id: match.rank for match in globally_ranked}
    for match in matches:
        match.rank = refreshed_ranks[match.id]
    if run is not None:
        from matching.models import MatchRevision, MatchRevisionKind

        reranked_ids = [
            match.id
            for match in revision_matches
            if match.id not in evaluated_match_ids
        ]
        reranked_evidence = {}
        if reranked_ids:
            for link in MatchEvidence.objects.filter(
                org=org,
                match_id__in=reranked_ids,
            ).select_related("evidence"):
                reranked_evidence.setdefault(link.match_id, []).append(
                    {
                        "evidence_id": str(link.evidence_id),
                        "content_hash": link.evidence.content_hash,
                        "direction": link.direction,
                        "relevance": str(link.relevance),
                        "contribution": str(link.contribution),
                        "explanation": link.explanation,
                    }
                )

        for match in revision_matches:
            evaluation = evaluations.get(match.id)
            if evaluation is not None:
                evidence_snapshot = [
                    {
                        "evidence_id": str(item["evidence"].id),
                        "content_hash": item["evidence"].content_hash,
                        "direction": item["direction"],
                        "relevance": str(item["relevance"]),
                        "contribution": str(item["contribution"]),
                        "explanation": item["explanation"],
                    }
                    for item in evaluation.evidence_contributions
                ]
                revision_kind = MatchRevisionKind.EVALUATION
            else:
                evidence_snapshot = reranked_evidence.get(match.id, [])
                revision_kind = MatchRevisionKind.RERANK
            MatchRevision.objects.create(
                org=org,
                match=match,
                run=run,
                revision=ranking_revision,
                revision_kind=revision_kind,
                snapshot={
                    "status": match.status,
                    "overall_score": match.overall_score,
                    "eligibility_score": match.eligibility_score,
                    "fit_score": match.fit_score,
                    "trust_score": match.trust_score,
                    "relationship_score": match.relationship_score,
                    "availability_score": match.availability_score,
                    "confidence": str(match.confidence),
                    "rank": match.rank,
                    "reasons": match.reasons,
                    "gaps": match.gaps,
                    "score_breakdown": match.score_breakdown,
                },
                evidence_snapshot=evidence_snapshot,
                engine_version=match.engine_version,
                evaluated_at=match.evaluated_at,
            )
    return sorted(matches, key=lambda item: item.rank)
