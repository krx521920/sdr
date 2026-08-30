from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
from uuid import uuid4

import pytest
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    transaction,
)
from django.db.models.query import QuerySet

from automation.tenant_context import database_org_context
from common.models import Org, Profile
from matching.decisions import MatchRankingConflict, apply_match_decision
from matching.models import (
    Match,
    MatchDecisionEvent,
    MatchOpportunity,
    MatchRevision,
    MatchRun,
    MatchStatus,
    Person,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS, triggers, and row-level concurrency are required.",
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


def _create_ranked_match(org, *, ranking_revision=7):
    person = Person.objects.create(org=org, display_name="Concurrent candidate")
    opportunity = MatchOpportunity.objects.create(
        org=org,
        opportunity_type="project",
        title="Concurrent opportunity",
    )
    return Match.objects.create(
        org=org,
        person=person,
        opportunity=opportunity,
        ranking_revision=ranking_revision,
    )


def _create_history_graph(org, actor):
    match = _create_ranked_match(org, ranking_revision=1)
    run = MatchRun.objects.create(
        org=org,
        opportunity=match.opportunity,
        requested_by=actor,
        request_hash="a" * 64,
        requested_person_ids=[str(match.person_id)],
        total_count=1,
        ranking_revision=1,
    )
    revision = MatchRevision.objects.create(
        org=org,
        match=match,
        run=run,
        revision=1,
        snapshot={"rank": 1},
        evidence_snapshot=[],
    )
    event = apply_match_decision(
        org=org,
        match_id=match.id,
        to_status=MatchStatus.REVIEWING,
        expected_decision_revision=0,
        expected_ranking_revision=1,
        reason_code="manual_review",
        reason="PostgreSQL security test",
        actor=actor,
        idempotency_key=str(uuid4()),
    ).event
    return run, revision, event


def _apply_decision_in_thread(*, org_id, actor_id, request_data):
    close_old_connections()
    try:
        with database_org_context(org_id):
            org = Org.objects.get(id=org_id)
            actor = Profile.objects.get(id=actor_id)
            return apply_match_decision(
                org=org,
                actor=actor,
                **request_data,
            )
    finally:
        close_old_connections()


def test_matching_history_rls_hides_rows_from_other_and_empty_context(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
):
    with database_org_context(org_a.id):
        run, revision, event = _create_history_graph(org_a, admin_profile)
        assert MatchRun.objects.filter(id=run.id).exists()
        assert MatchRevision.objects.filter(id=revision.id).exists()
        assert MatchDecisionEvent.objects.filter(id=event.id).exists()

    with database_org_context(org_b.id):
        assert not MatchRun.objects.filter(id=run.id).exists()
        assert not MatchRevision.objects.filter(id=revision.id).exists()
        assert not MatchDecisionEvent.objects.filter(id=event.id).exists()

    with _empty_database_org_context():
        assert not MatchRun.objects.filter(id=run.id).exists()
        assert not MatchRevision.objects.filter(id=revision.id).exists()
        assert not MatchDecisionEvent.objects.filter(id=event.id).exists()


def test_postgres_history_triggers_reject_raw_update_and_delete(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        _, revision, event = _create_history_graph(org_a, admin_profile)
        history_rows = (
            (MatchRevision, revision.id),
            (MatchDecisionEvent, event.id),
        )
        for model, row_id in history_rows:
            table = connection.ops.quote_name(model._meta.db_table)
            with pytest.raises(DatabaseError, match="append-only"):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE {table} SET created_at = created_at WHERE id = %s",
                            [row_id],
                        )
            with pytest.raises(DatabaseError, match="append-only"):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row_id])
            assert model.objects.filter(id=row_id).exists()


def test_postgres_decision_cas_rejects_concurrent_rerank(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        match = _create_ranked_match(org_a)

    cas_waiting = Event()
    rerank_committed = Event()
    original_update = QuerySet.update

    def gated_update(queryset, **kwargs):
        if queryset.model is Match and "decision_revision" in kwargs:
            cas_waiting.set()
            if not rerank_committed.wait(timeout=10):
                raise AssertionError("Timed out waiting for the concurrent rerank")
        return original_update(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "update", gated_update)
    request_data = {
        "match_id": match.id,
        "to_status": MatchStatus.REVIEWING,
        "expected_decision_revision": 0,
        "expected_ranking_revision": 7,
        "reason_code": "manual_review",
        "reason": "Review before rerank",
        "idempotency_key": str(uuid4()),
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _apply_decision_in_thread,
            org_id=org_a.id,
            actor_id=admin_profile.id,
            request_data=request_data,
        )
        try:
            assert cas_waiting.wait(timeout=10), "Decision did not reach its CAS update"
            with database_org_context(org_a.id):
                Match.objects.filter(id=match.id).update(ranking_revision=8)
        finally:
            rerank_committed.set()

        with pytest.raises(MatchRankingConflict):
            future.result(timeout=10)

    with database_org_context(org_a.id):
        match.refresh_from_db()
        assert match.status == MatchStatus.PROPOSED
        assert match.decision_revision == 0
        assert match.ranking_revision == 8
        assert not MatchDecisionEvent.objects.filter(match=match).exists()


def test_postgres_concurrent_identical_decisions_replay_one_event(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        match = _create_ranked_match(org_a)

    cas_barrier = Barrier(2)
    original_update = QuerySet.update

    def synchronized_update(queryset, **kwargs):
        if queryset.model is Match and "decision_revision" in kwargs:
            cas_barrier.wait(timeout=10)
        return original_update(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "update", synchronized_update)
    request_data = {
        "match_id": match.id,
        "to_status": MatchStatus.REVIEWING,
        "expected_decision_revision": 0,
        "expected_ranking_revision": 7,
        "reason_code": "manual_review",
        "reason": "One logical concurrent decision",
        "idempotency_key": str(uuid4()),
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _apply_decision_in_thread,
                org_id=org_a.id,
                actor_id=admin_profile.id,
                request_data=request_data,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]

    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].event.id == results[1].event.id
    with database_org_context(org_a.id):
        match.refresh_from_db()
        assert match.status == MatchStatus.REVIEWING
        assert match.decision_revision == 1
        assert MatchDecisionEvent.objects.filter(match=match).count() == 1
