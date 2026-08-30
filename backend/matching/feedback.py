"""Idempotent, append-only feedback and lifecycle outcome services."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Count, F, OuterRef, Subquery
from django.utils import timezone

from matching.models import (
    Evidence,
    Match,
    MatchFeedbackAction,
    MatchFeedbackAssessment,
    MatchFeedbackAttribution,
    MatchFeedbackDimension,
    MatchFeedbackEvent,
    MatchFeedbackEventKind,
    MatchFeedbackSource,
    MatchOpportunityType,
    MatchOutcomeCode,
    MatchProjectionState,
    MatchRecommendationVerdict,
    MatchRevision,
    PersonGovernanceStatus,
    PersonStatus,
)

MAX_FEEDBACK_ATTRIBUTIONS = 20
MIN_INSIGHT_SAMPLE = 10

COMMON_OUTCOMES = {
    MatchOutcomeCode.CONTACT_ATTEMPTED,
    MatchOutcomeCode.CONTACT_REACHED,
    MatchOutcomeCode.NOT_PURSUED,
    MatchOutcomeCode.WITHDREW,
}
OUTCOMES_BY_TYPE = {
    MatchOpportunityType.CUSTOMER: COMMON_OUTCOMES
    | {MatchOutcomeCode.DEAL_WON, MatchOutcomeCode.DEAL_LOST},
    MatchOpportunityType.EMPLOYMENT: COMMON_OUTCOMES
    | {
        MatchOutcomeCode.INTERVIEW_SCHEDULED,
        MatchOutcomeCode.INTERVIEW_COMPLETED,
        MatchOutcomeCode.HIRED,
        MatchOutcomeCode.NOT_HIRED,
    },
    MatchOpportunityType.CONTRACTOR: COMMON_OUTCOMES
    | {
        MatchOutcomeCode.INTERVIEW_SCHEDULED,
        MatchOutcomeCode.INTERVIEW_COMPLETED,
        MatchOutcomeCode.COLLABORATION_STARTED,
        MatchOutcomeCode.COLLABORATION_COMPLETED,
    },
    MatchOpportunityType.PROJECT: COMMON_OUTCOMES
    | {
        MatchOutcomeCode.COLLABORATION_STARTED,
        MatchOutcomeCode.COLLABORATION_COMPLETED,
    },
    MatchOpportunityType.EXPERT: COMMON_OUTCOMES
    | {
        MatchOutcomeCode.INTERVIEW_SCHEDULED,
        MatchOutcomeCode.INTERVIEW_COMPLETED,
        MatchOutcomeCode.COLLABORATION_STARTED,
        MatchOutcomeCode.COLLABORATION_COMPLETED,
    },
    MatchOpportunityType.REFERRAL: COMMON_OUTCOMES
    | {MatchOutcomeCode.REFERRAL_MADE, MatchOutcomeCode.REFERRAL_ACCEPTED},
    MatchOpportunityType.PARTNERSHIP: COMMON_OUTCOMES
    | {
        MatchOutcomeCode.DEAL_WON,
        MatchOutcomeCode.DEAL_LOST,
        MatchOutcomeCode.COLLABORATION_STARTED,
        MatchOutcomeCode.COLLABORATION_COMPLETED,
    },
}


class MatchFeedbackError(Exception):
    status_code = 422
    code = "invalid_match_feedback"

    def __init__(self, detail, **extra):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def as_dict(self):
        return {"code": self.code, "detail": self.detail, **self.extra}


class MatchFeedbackConflict(MatchFeedbackError):
    status_code = 409
    code = "feedback_revision_conflict"


class MatchFeedbackNotFound(MatchFeedbackError):
    status_code = 404
    code = "match_not_found"


@dataclass(frozen=True)
class MatchFeedbackResult:
    match: Match
    event: MatchFeedbackEvent
    replayed: bool = False


def _hash_payload(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_attributions(items):
    if len(items) > MAX_FEEDBACK_ATTRIBUTIONS:
        raise MatchFeedbackError(
            f"attributions cannot contain more than {MAX_FEEDBACK_ATTRIBUTIONS} items"
        )
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise MatchFeedbackError("Each attribution must be an object")
        if item.get("dimension") not in MatchFeedbackDimension.values:
            raise MatchFeedbackError("Unknown feedback attribution dimension")
        if item.get("assessment") not in MatchFeedbackAssessment.values:
            raise MatchFeedbackError("Unknown feedback attribution assessment")
        reason_code = str(item.get("reason_code") or "").strip()
        if reason_code and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", reason_code):
            raise MatchFeedbackError("Invalid attribution reason_code")
        value = {
            "evidence_id": str(item["evidence_id"]) if item.get("evidence_id") else None,
            "dimension": item["dimension"],
            "assessment": item["assessment"],
            "reason_code": reason_code,
        }
        key = (value["evidence_id"], value["dimension"])
        if key in seen:
            raise MatchFeedbackError("Duplicate evidence/dimension attribution")
        seen.add(key)
        normalized.append(value)
    return normalized


def _idempotent_result(*, org, existing, request_hash):
    if existing.request_hash != request_hash:
        raise MatchFeedbackConflict(
            "Idempotency-Key was already used for different feedback",
            idempotency_key=str(existing.idempotency_key),
        )
    match = Match.objects.filter(org=org, id=existing.match_id).first()
    if match is None:
        raise MatchFeedbackNotFound("Match does not exist in this organization")
    return MatchFeedbackResult(match=match, event=existing, replayed=True)


def record_match_feedback(
    *,
    org,
    match_id,
    event_kind,
    expected_feedback_revision,
    expected_ranking_revision,
    idempotency_key,
    actor,
    reason_code,
    occurred_at,
    verdict="",
    outcome_code="",
    note="",
    action=MatchFeedbackAction.RECORD,
    supersedes_id=None,
    source="manual",
    attributions=(),
):
    if not isinstance(idempotency_key, UUID):
        raise MatchFeedbackError("idempotency_key must be a UUID")
    for name, value in (
        ("expected_feedback_revision", expected_feedback_revision),
        ("expected_ranking_revision", expected_ranking_revision),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MatchFeedbackError(f"{name} must be a non-negative integer")
    if event_kind not in MatchFeedbackEventKind.values:
        raise MatchFeedbackError("Unknown feedback event kind")
    if action not in MatchFeedbackAction.values:
        raise MatchFeedbackError("Unknown feedback action")
    if source not in MatchFeedbackSource.values:
        raise MatchFeedbackError("Unknown feedback source")
    if not isinstance(occurred_at, datetime):
        raise MatchFeedbackError("occurred_at must be a datetime")
    reason_code = str(reason_code or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", reason_code):
        raise MatchFeedbackError("Invalid reason_code")
    note = str(note or "").strip()
    if len(note) > 1000:
        raise MatchFeedbackError("note must be at most 1000 characters")
    normalized_attributions = _normalize_attributions(attributions)
    payload = {
        "match_id": str(match_id),
        "event_kind": event_kind,
        "action": action,
        "expected_feedback_revision": expected_feedback_revision,
        "expected_ranking_revision": expected_ranking_revision,
        "reason_code": reason_code,
        "occurred_at": occurred_at,
        "verdict": verdict,
        "outcome_code": outcome_code,
        "note": note,
        "supersedes_id": str(supersedes_id) if supersedes_id else None,
        "source": source,
        "attributions": normalized_attributions,
    }
    request_hash = _hash_payload(payload)
    try:
        return _record_match_feedback(
            org=org,
            actor=actor,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
        )
    except IntegrityError:
        existing = MatchFeedbackEvent.objects.filter(
            org=org, idempotency_key=idempotency_key
        ).first()
        if existing is None:
            raise
        return _idempotent_result(
            org=org, existing=existing, request_hash=request_hash
        )


@transaction.atomic
def _record_match_feedback(*, org, actor, idempotency_key, request_hash, payload):
    existing = MatchFeedbackEvent.objects.filter(
        org=org, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        return _idempotent_result(
            org=org, existing=existing, request_hash=request_hash
        )
    match = (
        Match.objects.select_for_update()
        .select_related("opportunity")
        .filter(
            org=org,
            id=payload["match_id"],
            projection_state=MatchProjectionState.CURRENT,
            person__status=PersonStatus.ACTIVE,
            person__governance_status=PersonGovernanceStatus.ACTIVE,
        )
        .first()
    )
    if match is None:
        raise MatchFeedbackNotFound("Match does not exist in this organization")
    if actor is not None and actor.org_id != org.id:
        raise MatchFeedbackError("Feedback actor belongs to another organization")
    if match.feedback_revision != payload["expected_feedback_revision"]:
        raise MatchFeedbackConflict(
            "Match feedback revision is stale",
            expected_revision=payload["expected_feedback_revision"],
            current_revision=match.feedback_revision,
        )
    if match.ranking_revision != payload["expected_ranking_revision"]:
        raise MatchFeedbackConflict(
            "Match ranking revision is stale",
            expected_ranking_revision=payload["expected_ranking_revision"],
            current_ranking_revision=match.ranking_revision,
        )
    if not payload["reason_code"]:
        raise MatchFeedbackError("reason_code is required")
    if payload["occurred_at"] > timezone.now():
        raise MatchFeedbackError("occurred_at cannot be in the future")

    revision = MatchRevision.objects.filter(
        org=org,
        match=match,
        revision=payload["expected_ranking_revision"],
    ).first()
    if revision and payload["occurred_at"] < revision.evaluated_at:
        raise MatchFeedbackError("occurred_at cannot precede the match revision")

    supersedes = None
    if payload["action"] != MatchFeedbackAction.RECORD:
        supersedes = MatchFeedbackEvent.objects.filter(
            org=org,
            id=payload["supersedes_id"],
            match=match,
            event_kind=payload["event_kind"],
        ).first()
        if supersedes is None:
            raise MatchFeedbackError("correct and retract require a valid supersedes")
    elif payload["supersedes_id"]:
        raise MatchFeedbackError("record must not supersede another event")

    if payload["action"] == MatchFeedbackAction.RETRACT:
        if payload["verdict"] or payload["outcome_code"]:
            raise MatchFeedbackError("retract cannot include verdict or outcome_code")
    elif payload["event_kind"] == MatchFeedbackEventKind.RECOMMENDATION:
        if payload["verdict"] not in MatchRecommendationVerdict.values or payload[
            "verdict"
        ] == MatchRecommendationVerdict.UNKNOWN:
            raise MatchFeedbackError("A valid verdict is required")
        if payload["outcome_code"]:
            raise MatchFeedbackError("Recommendation feedback cannot include outcome_code")
    elif payload["event_kind"] == MatchFeedbackEventKind.OUTCOME:
        allowed = OUTCOMES_BY_TYPE.get(match.opportunity.opportunity_type, set())
        if payload["outcome_code"] not in allowed:
            raise MatchFeedbackError(
                "Outcome is not valid for this opportunity type",
                opportunity_type=match.opportunity.opportunity_type,
            )
        if payload["verdict"]:
            raise MatchFeedbackError("Lifecycle outcome cannot include verdict")
    else:
        raise MatchFeedbackError("Unknown feedback event kind")
    if payload["source"] not in MatchFeedbackSource.values:
        raise MatchFeedbackError("Unknown feedback source")
    if payload["event_kind"] != MatchFeedbackEventKind.RECOMMENDATION and payload[
        "attributions"
    ]:
        raise MatchFeedbackError("Attributions are only valid for recommendation feedback")

    evidence_ids = {
        item["evidence_id"]
        for item in payload["attributions"]
        if item["evidence_id"]
    }
    evidence = {
        str(item.id): item
        for item in Evidence.objects.filter(
            org=org, person=match.person, id__in=evidence_ids
        )
    }
    if set(evidence) != evidence_ids:
        raise MatchFeedbackError("Every attributed evidence must describe the matched person")
    if evidence_ids:
        if revision is None:
            raise MatchFeedbackError("Evidence attribution requires a stored match revision")
        snapshot_ids = {
            str(item.get("evidence_id"))
            for item in revision.evidence_snapshot
            if isinstance(item, dict) and item.get("evidence_id")
        }
        if not evidence_ids.issubset(snapshot_ids):
            raise MatchFeedbackError("Attributed evidence was not in the match revision")

    resulting_revision = match.feedback_revision + 1
    projection = {
        "feedback_revision": F("feedback_revision") + 1,
        "updated_at": timezone.now(),
        "updated_by_id": getattr(actor, "user_id", None),
    }
    if payload["event_kind"] == MatchFeedbackEventKind.RECOMMENDATION:
        projection["recommendation_verdict"] = (
            MatchRecommendationVerdict.UNKNOWN
            if payload["action"] == MatchFeedbackAction.RETRACT
            else payload["verdict"]
        )
    else:
        projection["latest_outcome_code"] = (
            "" if payload["action"] == MatchFeedbackAction.RETRACT else payload["outcome_code"]
        )
        projection["latest_outcome_at"] = (
            None if payload["action"] == MatchFeedbackAction.RETRACT else payload["occurred_at"]
        )
    updated = Match.objects.filter(
        org=org,
        id=match.id,
        projection_state=MatchProjectionState.CURRENT,
        person__status=PersonStatus.ACTIVE,
        person__governance_status=PersonGovernanceStatus.ACTIVE,
        feedback_revision=payload["expected_feedback_revision"],
        ranking_revision=payload["expected_ranking_revision"],
    ).update(**projection)
    if updated != 1:
        raise MatchFeedbackConflict("Match feedback changed concurrently")

    event = MatchFeedbackEvent.objects.create(
        org=org,
        match=match,
        match_revision=revision,
        event_kind=payload["event_kind"],
        action=payload["action"],
        verdict=payload["verdict"],
        outcome_code=payload["outcome_code"],
        reason_code=payload["reason_code"],
        note=payload["note"],
        occurred_at=payload["occurred_at"],
        source=payload["source"],
        expected_feedback_revision=payload["expected_feedback_revision"],
        resulting_feedback_revision=resulting_revision,
        based_on_ranking_revision=payload["expected_ranking_revision"],
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        supersedes=supersedes,
        safe_snapshot={
            "event_kind": payload["event_kind"],
            "action": payload["action"],
            "verdict": payload["verdict"],
            "outcome_code": payload["outcome_code"],
            "reason_code": payload["reason_code"],
            "attribution_count": len(payload["attributions"]),
        },
    )
    MatchFeedbackAttribution.objects.bulk_create(
        [
            MatchFeedbackAttribution(
                org=org,
                feedback_event=event,
                evidence=evidence.get(item["evidence_id"]),
                dimension=item["dimension"],
                assessment=item["assessment"],
                reason_code=item["reason_code"],
            )
            for item in payload["attributions"]
        ]
    )
    match.refresh_from_db()
    return MatchFeedbackResult(match=match, event=event)


def feedback_overview(*, org, opportunity_type=""):
    events = MatchFeedbackEvent.objects.filter(org=org)
    if opportunity_type:
        events = events.filter(match__opportunity__opportunity_type=opportunity_type)
    counts = {
        row["event_kind"]: row["count"]
        for row in events.values("event_kind").annotate(count=Count("id"))
    }
    matches = Match.objects.filter(
        org=org,
        projection_state=MatchProjectionState.CURRENT,
        person__status=PersonStatus.ACTIVE,
        person__governance_status=PersonGovernanceStatus.ACTIVE,
    )
    if opportunity_type:
        matches = matches.filter(opportunity__opportunity_type=opportunity_type)
    verdicts = {
        row["recommendation_verdict"]: row["count"]
        for row in matches.values("recommendation_verdict").annotate(count=Count("id"))
    }
    total_matches = matches.count()
    reviewed_matches = matches.exclude(
        recommendation_verdict=MatchRecommendationVerdict.UNKNOWN
    ).count()
    current_recommendations = current_recommendation_events(
        org=org, opportunity_type=opportunity_type
    )
    return {
        "observational": True,
        "coverage": {
            "total_matches": total_matches,
            "reviewed_matches": reviewed_matches,
            "rate": round(reviewed_matches / total_matches, 4) if total_matches else 0,
        },
        "event_count": events.count(),
        "recommendation_feedback_count": current_recommendations.count(),
        "lifecycle_outcome_count": counts.get(MatchFeedbackEventKind.OUTCOME, 0),
        "verdicts": verdicts,
    }


def feedback_insights(*, org, opportunity_type=""):
    events = current_recommendation_events(
        org=org, opportunity_type=opportunity_type
    )
    sample_count = events.count()
    if sample_count < MIN_INSIGHT_SAMPLE:
        return {
            "observational": True,
            "suppressed": True,
            "minimum_sample": MIN_INSIGHT_SAMPLE,
            "sample_count": sample_count,
            "dimensions": [],
        }
    dimensions = list(
        MatchFeedbackAttribution.objects.filter(org=org, feedback_event__in=events)
        .values("dimension", "assessment")
        .annotate(count=Count("id"))
        .order_by("dimension", "assessment")
    )
    return {
        "observational": True,
        "suppressed": False,
        "minimum_sample": MIN_INSIGHT_SAMPLE,
        "sample_count": sample_count,
        "dimensions": dimensions,
    }


def current_recommendation_events(*, org, opportunity_type=""):
    latest_id = (
        MatchFeedbackEvent.objects.filter(
            org=org,
            event_kind=MatchFeedbackEventKind.RECOMMENDATION,
            match_id=OuterRef("match_id"),
        )
        .order_by("-resulting_feedback_revision")
        .values("id")[:1]
    )
    queryset = (
        MatchFeedbackEvent.objects.filter(
            org=org,
            event_kind=MatchFeedbackEventKind.RECOMMENDATION,
        )
        .annotate(current_event_id=Subquery(latest_id))
        .filter(id=F("current_event_id"))
        .exclude(action=MatchFeedbackAction.RETRACT)
        .exclude(verdict=MatchRecommendationVerdict.UNKNOWN)
    )
    if opportunity_type:
        queryset = queryset.filter(
            match__opportunity__opportunity_type=opportunity_type
        )
    return queryset
