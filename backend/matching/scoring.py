"""Versioned scoring policies and human-reviewed weight suggestions."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max
from django.utils import timezone

from matching.models import (
    MatchFeedbackEvent,
    MatchFeedbackAttribution,
    MatchFeedbackAssessment,
    MatchFeedbackEventKind,
    MatchOpportunityType,
    MatchScoringPolicy,
    MatchScoringPolicyAction,
    MatchScoringPolicyEvent,
    MatchScoringPolicyVersion,
    MatchScoringPolicyVersionSource,
    MatchWeightSuggestion,
    MatchWeightSuggestionReviewAction,
    MatchWeightSuggestionReviewEvent,
    MatchWeightSuggestionStatus,
    default_scoring_weights,
)
from matching.feedback import current_recommendation_events

DIMENSION_KEYS = ("skills", "titles", "locations", "availability")
COMPONENT_KEYS = ("fit", "eligibility", "trust", "relationship", "availability")
DEFAULT_COMPONENT_WEIGHTS = {
    "fit": 50,
    "eligibility": 15,
    "trust": 15,
    "relationship": 10,
    "availability": 10,
}
MIN_SUGGESTION_SAMPLE = 10


class ScoringPolicyError(Exception):
    status_code = 422
    code = "invalid_scoring_policy"

    def __init__(self, detail, **extra):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def as_dict(self):
        return {"code": self.code, "detail": self.detail, **self.extra}


class ScoringPolicyConflict(ScoringPolicyError):
    status_code = 409
    code = "scoring_policy_revision_conflict"


class ScoringPolicyNotFound(ScoringPolicyError):
    status_code = 404
    code = "scoring_policy_not_found"


@dataclass(frozen=True)
class PolicySelection:
    version: MatchScoringPolicyVersion | None
    checksum: str
    dimension_weights: dict
    component_weights: dict


@dataclass(frozen=True)
class PolicyMutationResult:
    policy: MatchScoringPolicy
    version: MatchScoringPolicyVersion
    event: MatchScoringPolicyEvent
    replayed: bool = False


@dataclass(frozen=True)
class SuggestionReviewResult:
    suggestion: MatchWeightSuggestion
    event: MatchWeightSuggestionReviewEvent
    draft: MatchScoringPolicyVersion | None
    replayed: bool = False


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_weights(value, keys, *, field):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ScoringPolicyError(f"{field} must contain exactly: {', '.join(keys)}")
    cleaned = {}
    for key in keys:
        weight = value[key]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ScoringPolicyError(f"{field}.{key} must be numeric")
        if weight < 0 or weight > 100:
            raise ScoringPolicyError(f"{field}.{key} must be between 0 and 100")
        cleaned[key] = float(weight)
    if sum(cleaned.values()) <= 0:
        raise ScoringPolicyError(f"{field} must contain a positive weight")
    return cleaned


def weights_checksum(dimension_weights, component_weights):
    return _hash(
        {
            "dimension_weights": dimension_weights,
            "component_weights": component_weights,
        }
    )


def resolve_policy_for_opportunity(opportunity):
    version = getattr(opportunity, "scoring_policy_version", None)
    if version is None:
        policy = (
            MatchScoringPolicy.objects.filter(
                org=opportunity.org,
                opportunity_type=opportunity.opportunity_type,
            )
            .select_related("active_version")
            .first()
        )
        version = policy.active_version if policy else None
    if version is not None:
        dimensions = dict(version.dimension_weights)
        components = dict(version.component_weights)
        return PolicySelection(version, version.checksum, dimensions, components)
    dimensions = {
        **default_scoring_weights(),
        **dict(opportunity.scoring_weights or {}),
    }
    components = dict(DEFAULT_COMPONENT_WEIGHTS)
    return PolicySelection(
        None,
        weights_checksum(dimensions, components),
        dimensions,
        components,
    )


def _policy_replay(*, org, existing, request_hash):
    if existing.request_hash != request_hash:
        raise ScoringPolicyConflict(
            "Idempotency-Key was already used for another policy mutation"
        )
    return PolicyMutationResult(
        policy=existing.policy,
        version=existing.policy_version,
        event=existing,
        replayed=True,
    )


def create_policy_draft(
    *,
    org,
    opportunity_type,
    dimension_weights,
    component_weights,
    expected_revision,
    idempotency_key,
    actor,
    rationale="",
    source=MatchScoringPolicyVersionSource.HUMAN,
):
    if actor is not None and actor.org_id != org.id:
        raise ScoringPolicyError("Scoring policy actor belongs to another organization")
    if opportunity_type not in MatchOpportunityType.values:
        raise ScoringPolicyError("Unknown opportunity_type")
    dimensions = validate_weights(
        dimension_weights, DIMENSION_KEYS, field="dimension_weights"
    )
    components = validate_weights(
        component_weights, COMPONENT_KEYS, field="component_weights"
    )
    payload = {
        "opportunity_type": opportunity_type,
        "dimension_weights": dimensions,
        "component_weights": components,
        "expected_revision": expected_revision,
        "rationale": str(rationale or "").strip(),
        "source": source,
    }
    request_hash = _hash(payload)
    try:
        return _create_policy_draft(
            org=org,
            payload=payload,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    except IntegrityError:
        existing = MatchScoringPolicyEvent.objects.filter(
            org=org, idempotency_key=idempotency_key
        ).select_related("policy", "policy_version").first()
        if existing is None:
            raise
        return _policy_replay(org=org, existing=existing, request_hash=request_hash)


@transaction.atomic
def _create_policy_draft(*, org, payload, request_hash, idempotency_key, actor):
    existing = MatchScoringPolicyEvent.objects.filter(
        org=org, idempotency_key=idempotency_key
    ).select_related("policy", "policy_version").first()
    if existing:
        return _policy_replay(org=org, existing=existing, request_hash=request_hash)
    policy, _ = MatchScoringPolicy.objects.get_or_create(
        org=org, opportunity_type=payload["opportunity_type"]
    )
    policy = MatchScoringPolicy.objects.select_for_update().get(id=policy.id, org=org)
    if policy.revision != payload["expected_revision"]:
        raise ScoringPolicyConflict(
            "Scoring policy revision is stale",
            expected_revision=payload["expected_revision"],
            current_revision=policy.revision,
        )
    next_version = (
        MatchScoringPolicyVersion.objects.filter(org=org, policy=policy).aggregate(
            value=Max("version")
        )["value"]
        or 0
    ) + 1
    checksum = weights_checksum(
        payload["dimension_weights"], payload["component_weights"]
    )
    version = MatchScoringPolicyVersion.objects.create(
        org=org,
        policy=policy,
        version=next_version,
        dimension_weights=payload["dimension_weights"],
        component_weights=payload["component_weights"],
        checksum=checksum,
        source=payload["source"],
        rationale=payload["rationale"],
        created_by_profile=actor,
    )
    resulting_revision = policy.revision + 1
    updated = MatchScoringPolicy.objects.filter(
        id=policy.id, revision=policy.revision
    ).update(
        revision=F("revision") + 1,
        updated_at=timezone.now(),
        updated_by_id=getattr(actor, "user_id", None),
    )
    if updated != 1:
        raise ScoringPolicyConflict("Scoring policy changed concurrently")
    event = MatchScoringPolicyEvent.objects.create(
        org=org,
        policy=policy,
        policy_version=version,
        action=MatchScoringPolicyAction.DRAFT_CREATED,
        expected_revision=policy.revision,
        resulting_revision=resulting_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        safe_snapshot={
            "version": version.version,
            "checksum": checksum,
            "source": version.source,
        },
    )
    policy.refresh_from_db()
    return PolicyMutationResult(policy=policy, version=version, event=event)


def publish_policy_version(
    *, org, version_id, expected_revision, idempotency_key, actor
):
    payload = {
        "version_id": str(version_id),
        "expected_revision": expected_revision,
        "action": MatchScoringPolicyAction.PUBLISHED,
    }
    request_hash = _hash(payload)
    return _mutate_policy_version(
        org=org,
        version_id=version_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        request_hash=request_hash,
        action=MatchScoringPolicyAction.PUBLISHED,
    )


def reject_policy_version(
    *, org, version_id, expected_revision, idempotency_key, actor, reason_code
):
    payload = {
        "version_id": str(version_id),
        "expected_revision": expected_revision,
        "action": MatchScoringPolicyAction.REJECTED,
        "reason_code": str(reason_code or "").strip(),
    }
    request_hash = _hash(payload)
    return _mutate_policy_version(
        org=org,
        version_id=version_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        request_hash=request_hash,
        action=MatchScoringPolicyAction.REJECTED,
        reason_code=payload["reason_code"],
    )


@transaction.atomic
def _mutate_policy_version(
    *, org, version_id, expected_revision, idempotency_key, actor, request_hash,
    action, reason_code=""
):
    existing = MatchScoringPolicyEvent.objects.filter(
        org=org, idempotency_key=idempotency_key
    ).select_related("policy", "policy_version").first()
    if existing:
        return _policy_replay(org=org, existing=existing, request_hash=request_hash)
    version = MatchScoringPolicyVersion.objects.filter(
        org=org, id=version_id
    ).select_related("policy").first()
    if version is None:
        raise ScoringPolicyNotFound("Scoring policy version does not exist")
    policy = MatchScoringPolicy.objects.select_for_update().get(
        org=org, id=version.policy_id
    )
    if actor is None or actor.org_id != org.id:
        raise ScoringPolicyError("A same-organization human actor is required")
    if policy.revision != expected_revision:
        raise ScoringPolicyConflict(
            "Scoring policy revision is stale",
            expected_revision=expected_revision,
            current_revision=policy.revision,
        )
    if version.source == MatchScoringPolicyVersionSource.AI_SUGGESTION:
        raise ScoringPolicyError("AI suggestion versions cannot be published directly")
    prior_actions = set(version.events.values_list("action", flat=True))
    if MatchScoringPolicyAction.REJECTED in prior_actions:
        raise ScoringPolicyConflict("Rejected policy versions cannot be published")
    if MatchScoringPolicyAction.PUBLISHED in prior_actions:
        raise ScoringPolicyConflict("Published policy versions cannot be mutated again")
    if action == MatchScoringPolicyAction.REJECTED and policy.active_version_id == version.id:
        raise ScoringPolicyConflict("The active policy version cannot be rejected")
    resulting_revision = policy.revision + 1
    updates = {
        "revision": F("revision") + 1,
        "updated_at": timezone.now(),
        "updated_by_id": getattr(actor, "user_id", None),
    }
    if action == MatchScoringPolicyAction.PUBLISHED:
        updates["active_version"] = version
    updated = MatchScoringPolicy.objects.filter(
        id=policy.id, revision=policy.revision
    ).update(**updates)
    if updated != 1:
        raise ScoringPolicyConflict("Scoring policy changed concurrently")
    event = MatchScoringPolicyEvent.objects.create(
        org=org,
        policy=policy,
        policy_version=version,
        action=action,
        expected_revision=policy.revision,
        resulting_revision=resulting_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        reason_code=reason_code,
        safe_snapshot={"version": version.version, "checksum": version.checksum},
    )
    policy.refresh_from_db()
    return PolicyMutationResult(policy=policy, version=version, event=event)


@transaction.atomic
def generate_weight_suggestion(
    *, org, opportunity_type, expected_revision, idempotency_key, actor=None
):
    if opportunity_type not in MatchOpportunityType.values:
        raise ScoringPolicyError("Unknown opportunity_type")
    existing = MatchWeightSuggestion.objects.filter(
        org=org, idempotency_key=idempotency_key
    ).first()
    if existing:
        request_hash = _hash(
            {
                "opportunity_type": opportunity_type,
                "expected_revision": expected_revision,
                "base_policy_checksum": existing.base_policy_checksum,
            }
        )
        if existing.request_hash != request_hash:
            raise ScoringPolicyConflict(
                "Idempotency-Key was already used for another suggestion"
            )
        return existing, True
    policy, _ = MatchScoringPolicy.objects.get_or_create(
        org=org, opportunity_type=opportunity_type
    )
    policy = MatchScoringPolicy.objects.select_for_update().select_related(
        "active_version"
    ).get(org=org, id=policy.id)
    if policy.revision != expected_revision:
        raise ScoringPolicyConflict(
            "Scoring policy revision is stale",
            expected_revision=expected_revision,
            current_revision=policy.revision,
        )
    selection = (
        PolicySelection(
            policy.active_version,
            policy.active_version.checksum,
            dict(policy.active_version.dimension_weights),
            dict(policy.active_version.component_weights),
        )
        if policy.active_version_id
        else PolicySelection(
            None,
            "",
            default_scoring_weights(),
            DEFAULT_COMPONENT_WEIGHTS,
        )
    )
    request_hash = _hash(
        {
            "opportunity_type": opportunity_type,
            "expected_revision": expected_revision,
            "base_policy_checksum": selection.checksum,
        }
    )
    current_events = current_recommendation_events(
        org=org, opportunity_type=opportunity_type
    )
    sample_count = current_events.count()
    if sample_count < MIN_SUGGESTION_SAMPLE:
        raise ScoringPolicyError(
            "At least 10 recommendation feedback events are required",
            sample_count=sample_count,
            minimum_sample=MIN_SUGGESTION_SAMPLE,
        )
    aggregates = list(
        MatchFeedbackAttribution.objects.filter(
            org=org,
            feedback_event__in=current_events,
        )
        .values("dimension", "assessment")
        .annotate(count=Count("id"))
        .order_by("dimension", "assessment")
    )
    dimensions = dict(selection.dimension_weights)
    by_dimension = {}
    for row in aggregates:
        by_dimension.setdefault(row["dimension"], {})[row["assessment"]] = row[
            "count"
        ]
    for dimension in DIMENSION_KEYS:
        counts = by_dimension.get(dimension, {})
        helpful = counts.get(MatchFeedbackAssessment.HELPFUL, 0)
        harmful = counts.get(MatchFeedbackAssessment.MISLEADING, 0) + counts.get(
            MatchFeedbackAssessment.OUTDATED, 0
        )
        rated = helpful + harmful
        if rated:
            delta = max(-10, min(10, round(10 * (helpful - harmful) / rated)))
            dimensions[dimension] = max(0, min(100, dimensions[dimension] + delta))
    analysis_hash = _hash(
        {
            "opportunity_type": opportunity_type,
            "sample_count": sample_count,
            "base_checksum": selection.checksum,
            "aggregates": aggregates,
        }
    )
    suggestion = MatchWeightSuggestion.objects.create(
        org=org,
        policy=policy,
        opportunity_type=opportunity_type,
        dimension_weights=dimensions,
        component_weights=selection.component_weights,
        rationale="Observational analytics draft; human review required.",
        sample_count=sample_count,
        analysis_hash=analysis_hash,
        base_policy_checksum=selection.checksum,
        generator="observational-v1",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    return suggestion, False


def _suggestion_review_replay(*, org, event, request_hash):
    if event.request_hash != request_hash:
        raise ScoringPolicyConflict(
            "Idempotency-Key was already used for another suggestion review"
        )
    suggestion = MatchWeightSuggestion.objects.get(org=org, id=event.suggestion_id)
    return SuggestionReviewResult(
        suggestion=suggestion,
        event=event,
        draft=suggestion.accepted_draft,
        replayed=True,
    )


@transaction.atomic
def review_weight_suggestion(
    *, org, suggestion_id, action, expected_revision, idempotency_key, actor,
    reason_code=""
):
    if actor is None or actor.org_id != org.id:
        raise ScoringPolicyError("A same-organization human reviewer is required")
    if action not in MatchWeightSuggestionReviewAction.values:
        raise ScoringPolicyError("Unknown review action")
    payload = {
        "suggestion_id": str(suggestion_id),
        "action": action,
        "expected_revision": expected_revision,
        "reason_code": str(reason_code or "").strip(),
    }
    request_hash = _hash(payload)
    existing = MatchWeightSuggestionReviewEvent.objects.filter(
        org=org, idempotency_key=idempotency_key
    ).first()
    if existing:
        return _suggestion_review_replay(
            org=org, event=existing, request_hash=request_hash
        )
    suggestion = (
        MatchWeightSuggestion.objects.select_for_update()
        .filter(org=org, id=suggestion_id)
        .first()
    )
    if suggestion is None:
        raise ScoringPolicyNotFound("Weight suggestion does not exist")
    if suggestion.revision != expected_revision:
        raise ScoringPolicyConflict(
            "Weight suggestion revision is stale",
            expected_revision=expected_revision,
            current_revision=suggestion.revision,
        )
    if suggestion.status != MatchWeightSuggestionStatus.PENDING:
        raise ScoringPolicyConflict("Weight suggestion was already reviewed")
    draft = None
    if action == MatchWeightSuggestionReviewAction.ACCEPT:
        draft_key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"matching:suggestion-draft:{suggestion.id}:{suggestion.revision}",
        )
        result = create_policy_draft(
            org=org,
            opportunity_type=suggestion.opportunity_type,
            dimension_weights=suggestion.dimension_weights,
            component_weights=suggestion.component_weights,
            expected_revision=suggestion.policy.revision,
            idempotency_key=draft_key,
            actor=actor,
            rationale=suggestion.rationale,
            source=MatchScoringPolicyVersionSource.HUMAN,
        )
        draft = result.version
        new_status = MatchWeightSuggestionStatus.ACCEPTED
    else:
        new_status = MatchWeightSuggestionStatus.REJECTED
    resulting_revision = suggestion.revision + 1
    updated = MatchWeightSuggestion.objects.filter(
        org=org, id=suggestion.id, revision=suggestion.revision
    ).update(
        status=new_status,
        revision=F("revision") + 1,
        reviewed_by=actor,
        reviewed_at=timezone.now(),
        accepted_draft=draft,
        updated_at=timezone.now(),
        updated_by_id=getattr(actor, "user_id", None),
    )
    if updated != 1:
        raise ScoringPolicyConflict("Weight suggestion changed concurrently")
    event = MatchWeightSuggestionReviewEvent.objects.create(
        org=org,
        suggestion=suggestion,
        action=action,
        expected_revision=suggestion.revision,
        resulting_revision=resulting_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        reason_code=payload["reason_code"],
        safe_snapshot={
            "status": new_status,
            "accepted_draft_id": str(draft.id) if draft else None,
        },
    )
    suggestion.refresh_from_db()
    return SuggestionReviewResult(
        suggestion=suggestion, event=event, draft=draft
    )
