"""Optimistic, idempotent human decisions for matching results."""

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from matching.models import Match, MatchDecisionEvent, MatchStatus

ALLOWED_MATCH_TRANSITIONS = {
    MatchStatus.PROPOSED: frozenset(
        {MatchStatus.REVIEWING, MatchStatus.SHORTLISTED, MatchStatus.REJECTED}
    ),
    MatchStatus.REVIEWING: frozenset(
        {MatchStatus.SHORTLISTED, MatchStatus.REJECTED}
    ),
    MatchStatus.SHORTLISTED: frozenset(
        {MatchStatus.REVIEWING, MatchStatus.ACCEPTED, MatchStatus.REJECTED}
    ),
    MatchStatus.ACCEPTED: frozenset(),
    MatchStatus.REJECTED: frozenset(),
    MatchStatus.EXPIRED: frozenset(),
}


class MatchDecisionError(Exception):
    status_code = 422
    code = "invalid_match_decision"

    def __init__(self, message, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self):
        return {"code": self.code, "detail": self.message, **self.details}


class MatchDecisionConflict(MatchDecisionError):
    status_code = 409
    code = "decision_revision_conflict"


class MatchRankingConflict(MatchDecisionError):
    status_code = 409
    code = "ranking_revision_conflict"


class MatchDecisionNotFound(MatchDecisionError):
    status_code = 404
    code = "match_not_found"


class InvalidMatchTransition(MatchDecisionError):
    status_code = 422
    code = "invalid_match_transition"


@dataclass(frozen=True)
class MatchDecisionResult:
    match: Match
    event: MatchDecisionEvent
    replayed: bool = False


def _normalized_request(
    *,
    match_id,
    to_status,
    expected_decision_revision,
    expected_ranking_revision,
    reason_code,
    reason,
    actor,
    idempotency_key,
):
    reason_code = str(reason_code or "").strip()
    reason = str(reason or "").strip()
    idempotency_key = str(idempotency_key or "").strip()
    if not reason_code:
        raise MatchDecisionError("reason_code is required")
    if not idempotency_key:
        raise MatchDecisionError("idempotency_key is required")
    if len(idempotency_key) > 128:
        raise MatchDecisionError("idempotency_key must be at most 128 characters")
    if isinstance(expected_decision_revision, bool) or not isinstance(
        expected_decision_revision, int
    ):
        raise MatchDecisionError("expected_decision_revision must be an integer")
    if expected_decision_revision < 0:
        raise MatchDecisionError("expected_decision_revision cannot be negative")
    if isinstance(expected_ranking_revision, bool) or not isinstance(
        expected_ranking_revision, int
    ):
        raise MatchDecisionError("expected_ranking_revision must be an integer")
    if expected_ranking_revision < 0:
        raise MatchDecisionError("expected_ranking_revision cannot be negative")
    if to_status not in MatchStatus.values:
        raise InvalidMatchTransition(f"Unknown match status: {to_status}")
    return {
        "match_id": match_id,
        "to_status": to_status,
        "expected_decision_revision": expected_decision_revision,
        "expected_ranking_revision": expected_ranking_revision,
        "reason_code": reason_code,
        "reason": reason,
        "actor": actor,
        "actor_id": getattr(actor, "id", None),
        "idempotency_key": idempotency_key,
    }


def _idempotent_result(*, org, existing, request_data):
    same_request = all(
        (
            existing.match_id == request_data["match_id"],
            existing.to_status == request_data["to_status"],
            existing.expected_decision_revision
            == request_data["expected_decision_revision"],
            existing.based_on_ranking_revision
            == request_data["expected_ranking_revision"],
            existing.reason_code == request_data["reason_code"],
            existing.reason == request_data["reason"],
            existing.actor_id == request_data["actor_id"],
        )
    )
    if not same_request:
        raise MatchDecisionConflict(
            "idempotency_key was already used for a different decision",
            idempotency_key=request_data["idempotency_key"],
        )
    match = Match.objects.filter(org=org, id=existing.match_id).first()
    if match is None:
        raise MatchDecisionNotFound("Match does not exist in this organization")
    return MatchDecisionResult(match=match, event=existing, replayed=True)


def apply_match_decision(
    *,
    org,
    match_id,
    to_status,
    expected_decision_revision,
    expected_ranking_revision,
    reason_code,
    idempotency_key,
    reason="",
    actor=None,
):
    """Apply one state transition or return the prior idempotent result."""

    request_data = _normalized_request(
        match_id=match_id,
        to_status=to_status,
        expected_decision_revision=expected_decision_revision,
        expected_ranking_revision=expected_ranking_revision,
        reason_code=reason_code,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
    )
    try:
        return _apply_match_decision(org=org, request_data=request_data)
    except IntegrityError:
        existing = MatchDecisionEvent.objects.filter(
            org=org,
            idempotency_key=request_data["idempotency_key"],
        ).first()
        if existing is None:
            raise
        return _idempotent_result(
            org=org,
            existing=existing,
            request_data=request_data,
        )


@transaction.atomic
def _apply_match_decision(*, org, request_data):
    existing = MatchDecisionEvent.objects.filter(
        org=org,
        idempotency_key=request_data["idempotency_key"],
    ).first()
    if existing is not None:
        return _idempotent_result(
            org=org,
            existing=existing,
            request_data=request_data,
        )

    actor = request_data["actor"]
    if actor is not None and actor.org_id != org.id:
        raise MatchDecisionError("Decision actor belongs to another organization")

    match = Match.objects.filter(org=org, id=request_data["match_id"]).first()
    if match is None:
        raise MatchDecisionNotFound("Match does not exist in this organization")

    expected_revision = request_data["expected_decision_revision"]
    if match.decision_revision != expected_revision:
        raise MatchDecisionConflict(
            "Match decision revision is stale",
            expected_revision=expected_revision,
            current_revision=match.decision_revision,
        )
    expected_ranking_revision = request_data["expected_ranking_revision"]
    if match.ranking_revision != expected_ranking_revision:
        raise MatchRankingConflict(
            "Match ranking revision is stale",
            expected_revision=expected_ranking_revision,
            current_revision=match.ranking_revision,
        )

    allowed = ALLOWED_MATCH_TRANSITIONS.get(match.status, frozenset())
    if request_data["to_status"] not in allowed:
        raise InvalidMatchTransition(
            f"Cannot transition match from {match.status} to "
            f"{request_data['to_status']}",
            from_status=match.status,
            to_status=request_data["to_status"],
        )

    decided_at = timezone.now()
    resulting_revision = expected_revision + 1
    updated = Match.objects.filter(
        org=org,
        id=match.id,
        status=match.status,
        decision_revision=expected_revision,
        ranking_revision=expected_ranking_revision,
    ).update(
        status=request_data["to_status"],
        decision_revision=F("decision_revision") + 1,
        decision_reason=request_data["reason"],
        decided_at=decided_at,
        decided_by=actor,
        updated_at=decided_at,
        updated_by_id=getattr(actor, "user_id", None),
    )
    if updated != 1:
        existing = MatchDecisionEvent.objects.filter(
            org=org,
            idempotency_key=request_data["idempotency_key"],
        ).first()
        if existing is not None:
            return _idempotent_result(
                org=org,
                existing=existing,
                request_data=request_data,
            )
        current = (
            Match.objects.filter(org=org, id=match.id)
            .values("decision_revision", "ranking_revision")
            .first()
        )
        if (
            current is not None
            and current["ranking_revision"] != expected_ranking_revision
        ):
            raise MatchRankingConflict(
                "Match ranking revision changed concurrently",
                expected_revision=expected_ranking_revision,
                current_revision=current["ranking_revision"],
            )
        raise MatchDecisionConflict(
            "Match decision revision changed concurrently",
            expected_revision=expected_revision,
            current_revision=(
                current["decision_revision"] if current is not None else None
            ),
        )

    event = MatchDecisionEvent.objects.create(
        org=org,
        match=match,
        from_status=match.status,
        to_status=request_data["to_status"],
        reason_code=request_data["reason_code"],
        reason=request_data["reason"],
        expected_decision_revision=expected_revision,
        resulting_decision_revision=resulting_revision,
        based_on_ranking_revision=expected_ranking_revision,
        actor=actor,
        idempotency_key=request_data["idempotency_key"],
    )
    match.refresh_from_db()
    return MatchDecisionResult(match=match, event=event)
