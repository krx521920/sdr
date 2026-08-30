import pytest
from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet

from matching.decisions import (
    ALLOWED_MATCH_TRANSITIONS,
    InvalidMatchTransition,
    MatchDecisionConflict,
    MatchDecisionError,
    MatchRankingConflict,
    apply_match_decision,
)
from matching.models import Match, MatchOpportunity, MatchStatus, Person


def _match(org, *, status=MatchStatus.PROPOSED):
    person = Person.objects.create(org=org, display_name="Alice")
    opportunity = MatchOpportunity.objects.create(
        org=org,
        opportunity_type="project",
        title="AI project",
    )
    return Match.objects.create(
        org=org,
        person=person,
        opportunity=opportunity,
        status=status,
        ranking_revision=7,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (from_status, to_status)
        for from_status, allowed in ALLOWED_MATCH_TRANSITIONS.items()
        for to_status in allowed
    ],
)
def test_allowed_match_status_transitions(
    org_a,
    admin_profile,
    from_status,
    to_status,
):
    match = _match(org_a, status=from_status)

    result = apply_match_decision(
        org=org_a,
        match_id=match.id,
        to_status=to_status,
        expected_decision_revision=0,
        expected_ranking_revision=7,
        reason_code="manual_review",
        reason="Reviewed by the matching operator",
        actor=admin_profile,
        idempotency_key=f"{from_status}-{to_status}",
    )

    assert result.replayed is False
    assert result.match.status == to_status
    assert result.match.decision_revision == 1
    assert result.match.decision_reason == "Reviewed by the matching operator"
    assert result.match.decided_by == admin_profile
    assert result.event.from_status == from_status
    assert result.event.to_status == to_status
    assert result.event.expected_decision_revision == 0
    assert result.event.resulting_decision_revision == 1
    assert result.event.based_on_ranking_revision == 7


@pytest.mark.django_db
@pytest.mark.parametrize(
    "terminal_status",
    [MatchStatus.ACCEPTED, MatchStatus.REJECTED, MatchStatus.EXPIRED],
)
def test_terminal_and_same_status_transitions_are_rejected(
    org_a,
    admin_profile,
    terminal_status,
):
    terminal = _match(org_a, status=terminal_status)
    with pytest.raises(InvalidMatchTransition) as terminal_error:
        apply_match_decision(
            org=org_a,
            match_id=terminal.id,
            to_status=MatchStatus.REVIEWING,
            expected_decision_revision=0,
            expected_ranking_revision=7,
            reason_code="manual_review",
            actor=admin_profile,
            idempotency_key=f"terminal-{terminal_status}",
        )
    assert terminal_error.value.status_code == 422

    proposed = _match(org_a)
    with pytest.raises(InvalidMatchTransition):
        apply_match_decision(
            org=org_a,
            match_id=proposed.id,
            to_status=MatchStatus.PROPOSED,
            expected_decision_revision=0,
            expected_ranking_revision=7,
            reason_code="manual_review",
            actor=admin_profile,
            idempotency_key=f"same-{terminal_status}",
        )


@pytest.mark.django_db
def test_decision_requires_reason_code_and_valid_actor(org_a, profile_b):
    match = _match(org_a)
    with pytest.raises(MatchDecisionError, match="reason_code"):
        apply_match_decision(
            org=org_a,
            match_id=match.id,
            to_status=MatchStatus.REVIEWING,
            expected_decision_revision=0,
            expected_ranking_revision=7,
            reason_code="",
            idempotency_key="missing-reason",
        )
    with pytest.raises(MatchDecisionError, match="another organization"):
        apply_match_decision(
            org=org_a,
            match_id=match.id,
            to_status=MatchStatus.REVIEWING,
            expected_decision_revision=0,
            expected_ranking_revision=7,
            reason_code="manual_review",
            actor=profile_b,
            idempotency_key="foreign-actor",
        )


@pytest.mark.django_db
def test_stale_expected_revision_is_a_409_without_event(org_a, admin_profile):
    match = _match(org_a)
    match.decision_revision = 2
    match.save(update_fields=["decision_revision"])

    with pytest.raises(MatchDecisionConflict) as exc_info:
        apply_match_decision(
            org=org_a,
            match_id=match.id,
            to_status=MatchStatus.REVIEWING,
            expected_decision_revision=1,
            expected_ranking_revision=7,
            reason_code="manual_review",
            actor=admin_profile,
            idempotency_key="stale-revision",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {
        "expected_revision": 1,
        "current_revision": 2,
    }
    assert match.decision_events.count() == 0


@pytest.mark.django_db
def test_stale_ranking_revision_is_a_409_without_event(org_a, admin_profile):
    match = _match(org_a)

    with pytest.raises(MatchRankingConflict) as exc_info:
        apply_match_decision(
            org=org_a,
            match_id=match.id,
            to_status=MatchStatus.REVIEWING,
            expected_decision_revision=0,
            expected_ranking_revision=6,
            reason_code="manual_review",
            actor=admin_profile,
            idempotency_key="stale-ranking",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {
        "expected_revision": 6,
        "current_revision": 7,
    }
    assert match.decision_events.count() == 0


@pytest.mark.django_db
def test_decision_rejects_ranking_changed_during_cas(
    org_a,
    admin_profile,
    monkeypatch,
):
    match = _match(org_a)
    original_update = QuerySet.update
    race_injected = False

    def update_with_concurrent_rerank(queryset, **kwargs):
        nonlocal race_injected
        if not race_injected and "decision_revision" in kwargs:
            race_injected = True
            original_update(
                Match.objects.filter(pk=match.pk),
                ranking_revision=8,
            )
        return original_update(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "update", update_with_concurrent_rerank)

    with pytest.raises(MatchRankingConflict) as exc_info:
        apply_match_decision(
            org=org_a,
            match_id=match.id,
            to_status=MatchStatus.REVIEWING,
            expected_decision_revision=0,
            expected_ranking_revision=7,
            reason_code="manual_review",
            actor=admin_profile,
            idempotency_key="ranking-cas-race",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {
        "expected_revision": 7,
        "current_revision": 8,
    }
    match.refresh_from_db()
    assert match.status == MatchStatus.PROPOSED
    assert match.decision_revision == 0
    assert match.decision_events.count() == 0


@pytest.mark.django_db
def test_identical_idempotent_retry_returns_original_event(org_a, admin_profile):
    match = _match(org_a)
    request = {
        "org": org_a,
        "match_id": match.id,
        "to_status": MatchStatus.REVIEWING,
        "expected_decision_revision": 0,
        "expected_ranking_revision": 7,
        "reason_code": "manual_review",
        "reason": "Initial review",
        "actor": admin_profile,
        "idempotency_key": "retry-safe-key",
    }

    first = apply_match_decision(**request)
    replay = apply_match_decision(**request)

    assert replay.replayed is True
    assert replay.event.id == first.event.id
    assert replay.match.decision_revision == 1
    assert match.decision_events.count() == 1


@pytest.mark.django_db
def test_concurrent_identical_decision_replays_winning_event(
    org_a,
    admin_profile,
    monkeypatch,
):
    match = _match(org_a)
    original_update = QuerySet.update
    race_injected = False

    def update_with_winning_request(queryset, **kwargs):
        nonlocal race_injected
        if not race_injected and "decision_revision" in kwargs:
            race_injected = True
            original_update(
                Match.objects.filter(pk=match.pk),
                status=MatchStatus.REVIEWING,
                decision_revision=1,
                decision_reason="Initial review",
                decided_by=admin_profile,
            )
            match.decision_events.create(
                org=org_a,
                from_status=MatchStatus.PROPOSED,
                to_status=MatchStatus.REVIEWING,
                reason_code="manual_review",
                reason="Initial review",
                expected_decision_revision=0,
                resulting_decision_revision=1,
                based_on_ranking_revision=7,
                actor=admin_profile,
                idempotency_key="concurrent-retry-safe-key",
            )
        return original_update(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "update", update_with_winning_request)

    result = apply_match_decision(
        org=org_a,
        match_id=match.id,
        to_status=MatchStatus.REVIEWING,
        expected_decision_revision=0,
        expected_ranking_revision=7,
        reason_code="manual_review",
        reason="Initial review",
        actor=admin_profile,
        idempotency_key="concurrent-retry-safe-key",
    )

    assert result.replayed is True
    assert result.match.status == MatchStatus.REVIEWING
    assert result.match.decision_revision == 1
    assert match.decision_events.count() == 1


@pytest.mark.django_db
def test_idempotency_key_payload_mismatch_is_a_409(org_a, admin_profile):
    match = _match(org_a)
    apply_match_decision(
        org=org_a,
        match_id=match.id,
        to_status=MatchStatus.REVIEWING,
        expected_decision_revision=0,
        expected_ranking_revision=7,
        reason_code="manual_review",
        actor=admin_profile,
        idempotency_key="reused-key",
    )

    with pytest.raises(MatchDecisionConflict) as exc_info:
        apply_match_decision(
            org=org_a,
            match_id=match.id,
            to_status=MatchStatus.SHORTLISTED,
            expected_decision_revision=0,
            expected_ranking_revision=7,
            reason_code="manual_review",
            actor=admin_profile,
            idempotency_key="reused-key",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["idempotency_key"] == "reused-key"


@pytest.mark.django_db
def test_append_only_decision_event_rejects_mutation(org_a, admin_profile):
    match = _match(org_a)
    event = apply_match_decision(
        org=org_a,
        match_id=match.id,
        to_status=MatchStatus.REVIEWING,
        expected_decision_revision=0,
        expected_ranking_revision=7,
        reason_code="manual_review",
        actor=admin_profile,
        idempotency_key="immutable-event",
    ).event

    event.reason = "tampered"
    with pytest.raises(ValidationError, match="cannot be updated"):
        event.save()
    with pytest.raises(ValidationError, match="cannot be deleted"):
        event.delete()
    with pytest.raises(ValidationError, match="cannot be updated"):
        match.decision_events.update(reason="tampered")
    with pytest.raises(ValidationError, match="cannot be deleted"):
        match.decision_events.all().delete()
