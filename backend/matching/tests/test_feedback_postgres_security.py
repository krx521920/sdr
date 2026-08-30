from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from automation.tenant_context import database_org_context
from matching.feedback import record_match_feedback
from matching.models import (
    Evidence,
    Match,
    MatchFeedbackAttribution,
    MatchFeedbackEvent,
    MatchOpportunity,
    MatchRevision,
    MatchRun,
    MatchScoringPolicy,
    MatchScoringPolicyEvent,
    MatchScoringPolicyVersion,
    MatchWeightSuggestion,
    MatchWeightSuggestionReviewEvent,
    Person,
)
from matching.scoring import (
    DEFAULT_COMPONENT_WEIGHTS,
    create_policy_draft,
    review_weight_suggestion,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS and immutable-history triggers are required.",
    ),
    pytest.mark.django_db(transaction=True),
]


@contextmanager
def _empty_database_org_context():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_org', true)")
        previous = cursor.fetchone()[0] or ""
        cursor.execute("SELECT set_config('app.current_org', '', false)")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_org', %s, false)",
                [previous],
            )


def _create_feedback_policy_graph(*, org, actor, suffix):
    evaluated_at = timezone.now() - timedelta(minutes=1)
    person = Person.objects.create(org=org, display_name=f"Feedback {suffix}")
    evidence = Evidence.objects.create(
        org=org,
        person=person,
        kind="skill",
        source="manual",
        summary="PostgreSQL security evidence",
        facts={"skills": ["python"]},
    )
    opportunity = MatchOpportunity.objects.create(
        org=org,
        opportunity_type="employment",
        title=f"Security opportunity {suffix}",
    )
    match = Match.objects.create(
        org=org,
        person=person,
        opportunity=opportunity,
        evaluated_at=evaluated_at,
    )
    run = MatchRun.objects.create(
        org=org,
        opportunity=opportunity,
        request_hash="a" * 64,
        requested_person_ids=[str(person.id)],
        total_count=1,
        processed_count=1,
        result_count=1,
        ranking_revision=0,
        started_at=evaluated_at,
        completed_at=evaluated_at,
        outcome="succeeded",
    )
    MatchRevision.objects.create(
        org=org,
        match=match,
        run=run,
        revision=0,
        snapshot={"overall_score": 0},
        evidence_snapshot=[{"evidence_id": str(evidence.id)}],
        evaluated_at=evaluated_at,
    )
    feedback_result = record_match_feedback(
        org=org,
        match_id=match.id,
        event_kind="recommendation_feedback",
        expected_feedback_revision=0,
        expected_ranking_revision=0,
        idempotency_key=uuid4(),
        actor=actor,
        reason_code="human_review",
        occurred_at=timezone.now(),
        verdict="accurate",
        source="manual",
        attributions=(
            {
                "evidence_id": evidence.id,
                "dimension": "skills",
                "assessment": "helpful",
                "reason_code": "supported",
            },
        ),
    )
    feedback = feedback_result.event
    attribution = MatchFeedbackAttribution.objects.get(feedback_event=feedback)
    draft = create_policy_draft(
        org=org,
        opportunity_type="employment",
        dimension_weights={
            "skills": 45,
            "titles": 20,
            "locations": 15,
            "availability": 20,
        },
        component_weights=DEFAULT_COMPONENT_WEIGHTS,
        expected_revision=0,
        idempotency_key=uuid4(),
        actor=actor,
        rationale="PostgreSQL security draft",
    )
    policy = draft.policy
    version = draft.version
    policy_event = draft.event
    suggestion = MatchWeightSuggestion.objects.create(
        org=org,
        policy=policy,
        opportunity_type="employment",
        dimension_weights=version.dimension_weights,
        component_weights={},
        rationale="Bounded aggregate signals support a draft.",
        sample_count=20,
        analysis_hash="d" * 64,
        base_policy_checksum=version.checksum,
        generator="rules-v1",
        idempotency_key=uuid4(),
        request_hash="e" * 64,
    )
    review_result = review_weight_suggestion(
        org=org,
        suggestion_id=suggestion.id,
        action="reject",
        expected_revision=0,
        idempotency_key=uuid4(),
        actor=actor,
        reason_code="security_test",
    )
    review = review_result.event
    return {
        "feedback": feedback,
        "attribution": attribution,
        "policy": policy,
        "version": version,
        "policy_event": policy_event,
        "suggestion": suggestion,
        "review": review,
    }


def _assert_raw_mutation_rejected(model, row_id, message):
    table = connection.ops.quote_name(model._meta.db_table)
    with pytest.raises(DatabaseError, match=message):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET created_at = created_at WHERE id = %s",
                    [row_id],
                )
    with pytest.raises(DatabaseError, match=message):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row_id])
    assert model.objects.filter(id=row_id).exists()


def test_all_feedback_and_scoring_tables_force_rls(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
):
    with database_org_context(org_a.id):
        graph = _create_feedback_policy_graph(
            org=org_a,
            actor=admin_profile,
            suffix="rls",
        )
        rows = (
            (MatchFeedbackEvent, graph["feedback"].id),
            (MatchFeedbackAttribution, graph["attribution"].id),
            (MatchScoringPolicy, graph["policy"].id),
            (MatchScoringPolicyVersion, graph["version"].id),
            (MatchScoringPolicyEvent, graph["policy_event"].id),
            (MatchWeightSuggestion, graph["suggestion"].id),
            (MatchWeightSuggestionReviewEvent, graph["review"].id),
        )
        for model, row_id in rows:
            assert model.objects.filter(id=row_id).exists()

    with database_org_context(org_b.id):
        for model, row_id in rows:
            assert not model.objects.filter(id=row_id).exists()

    with _empty_database_org_context():
        for model, row_id in rows:
            assert not model.objects.filter(id=row_id).exists()


def test_feedback_history_rejects_raw_update_and_delete(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        graph = _create_feedback_policy_graph(
            org=org_a,
            actor=admin_profile,
            suffix="feedback-append-only",
        )
        for model, key in (
            (MatchFeedbackEvent, "feedback"),
            (MatchFeedbackAttribution, "attribution"),
        ):
            _assert_raw_mutation_rejected(
                model,
                graph[key].id,
                "matching feedback history is append-only",
            )


def test_scoring_history_rejects_raw_update_and_delete(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        graph = _create_feedback_policy_graph(
            org=org_a,
            actor=admin_profile,
            suffix="scoring-append-only",
        )
        for model, key in (
            (MatchScoringPolicyVersion, "version"),
            (MatchScoringPolicyEvent, "policy_event"),
            (MatchWeightSuggestionReviewEvent, "review"),
        ):
            _assert_raw_mutation_rejected(
                model,
                graph[key].id,
                "matching scoring history is append-only",
            )
