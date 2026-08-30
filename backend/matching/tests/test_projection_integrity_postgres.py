from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    transaction,
)

from automation.tenant_context import database_org_context
from common.models import Org
from matching.models import (
    Match,
    MatchOpportunity,
    MatchProjectionState,
    Person,
    PersonStatus,
)
from matching.services import recompute_opportunity_matches

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL projection triggers, RLS, and row locks are required.",
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


def _ranked_graph(org):
    target = Person.objects.create(
        org=org,
        display_name="Concurrent target",
        skills=["python"],
    )
    remaining = Person.objects.create(
        org=org,
        display_name="Concurrent remaining",
        skills=["design"],
    )
    opportunity = MatchOpportunity.objects.create(
        org=org,
        opportunity_type="project",
        title="Projection concurrency",
        required_criteria={"skills": ["python"]},
    )
    recompute_opportunity_matches(org=org, opportunity=opportunity)
    return target, remaining, opportunity


def _deactivate_person(*, org_id, person_id, barrier):
    close_old_connections()
    try:
        with database_org_context(org_id):
            person = Person.objects.get(id=person_id)
            barrier.wait(timeout=10)
            person.status = PersonStatus.INACTIVE
            person.save(update_fields=["status"])
    finally:
        close_old_connections()


def _recompute_person(*, org_id, person_id, opportunity_id, barrier):
    close_old_connections()
    try:
        with database_org_context(org_id):
            org = Org.objects.get(id=org_id)
            opportunity = MatchOpportunity.objects.get(id=opportunity_id)
            barrier.wait(timeout=10)
            recompute_opportunity_matches(
                org=org,
                opportunity=opportunity,
                people=Person.objects.filter(id=person_id),
            )
    finally:
        close_old_connections()


def test_postgres_guards_reject_stale_current_and_cross_org_projections(
    transactional_db,
    org_a,
    org_b,
):
    with database_org_context(org_b.id):
        foreign_opportunity = MatchOpportunity.objects.create(
            org=org_b,
            opportunity_type="project",
            title="Foreign opportunity",
        )

    with database_org_context(org_a.id):
        person = Person.objects.create(org=org_a, display_name="Guarded person")
        inactive = Person.objects.create(
            org=org_a,
            display_name="Inactive person",
            status=PersonStatus.INACTIVE,
        )
        opportunity = MatchOpportunity.objects.create(
            org=org_a,
            opportunity_type="project",
            title="Guarded opportunity",
        )
        current = Match.objects.create(
            org=org_a,
            person=person,
            opportunity=opportunity,
            rank=1,
        )

        with pytest.raises(DatabaseError, match="person has a current match projection"):
            with transaction.atomic():
                Person.objects.filter(id=person.id).update(status=PersonStatus.INACTIVE)
        with pytest.raises(
            DatabaseError,
            match="current match projection requires an active person",
        ):
            with transaction.atomic():
                Match.objects.create(
                    org=org_a,
                    person=inactive,
                    opportunity=opportunity,
                )
        with pytest.raises(DatabaseError, match="organization mismatch"):
            with transaction.atomic():
                Match.objects.create(
                    org=org_a,
                    person=person,
                    opportunity_id=foreign_opportunity.id,
                )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                second = Person.objects.create(
                    org=org_a,
                    display_name="Duplicate rank person",
                )
                Match.objects.create(
                    org=org_a,
                    person=second,
                    opportunity=opportunity,
                    rank=1,
                )

        current.refresh_from_db()
        person.refresh_from_db()
        assert current.projection_state == MatchProjectionState.CURRENT
        assert person.status == PersonStatus.ACTIVE


def test_projection_rows_remain_force_rls_isolated(
    transactional_db,
    org_a,
    org_b,
):
    with database_org_context(org_a.id):
        target, _remaining, opportunity = _ranked_graph(org_a)
        match = Match.objects.get(person=target, opportunity=opportunity)
        target.status = PersonStatus.INACTIVE
        target.save(update_fields=["status"])
        match.refresh_from_db()
        assert match.projection_state == MatchProjectionState.RETIRED
        assert Match.objects.filter(id=match.id).exists()

    with database_org_context(org_b.id):
        assert not Match.objects.filter(id=match.id).exists()

    with _empty_database_org_context():
        assert not Match.objects.filter(id=match.id).exists()


def test_concurrent_recompute_cannot_resurrect_deactivated_person(
    transactional_db,
    org_a,
):
    with database_org_context(org_a.id):
        target, remaining, opportunity = _ranked_graph(org_a)

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                _deactivate_person,
                org_id=org_a.id,
                person_id=target.id,
                barrier=barrier,
            ),
            executor.submit(
                _recompute_person,
                org_id=org_a.id,
                person_id=target.id,
                opportunity_id=opportunity.id,
                barrier=barrier,
            ),
        )
        for future in futures:
            future.result(timeout=30)

    with database_org_context(org_a.id):
        target.refresh_from_db()
        target_match = Match.objects.get(person=target, opportunity=opportunity)
        remaining_match = Match.objects.get(
            person=remaining,
            opportunity=opportunity,
        )
        assert target.status == PersonStatus.INACTIVE
        assert target_match.projection_state == MatchProjectionState.RETIRED
        assert target_match.rank is None
        assert remaining_match.projection_state == MatchProjectionState.CURRENT
        assert remaining_match.rank == 1


def test_two_people_can_be_concurrently_retired_from_one_opportunity(
    transactional_db,
    org_a,
):
    with database_org_context(org_a.id):
        first, second, opportunity = _ranked_graph(org_a)

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                _deactivate_person,
                org_id=org_a.id,
                person_id=first.id,
                barrier=barrier,
            ),
            executor.submit(
                _deactivate_person,
                org_id=org_a.id,
                person_id=second.id,
                barrier=barrier,
            ),
        )
        for future in futures:
            future.result(timeout=30)

    with database_org_context(org_a.id):
        first.refresh_from_db()
        second.refresh_from_db()
        opportunity.refresh_from_db()
        projections = list(
            Match.objects.filter(opportunity=opportunity).order_by("person_id")
        )
        assert first.status == PersonStatus.INACTIVE
        assert second.status == PersonStatus.INACTIVE
        assert opportunity.ranking_revision == 2
        assert len(projections) == 2
        assert all(
            match.projection_state == MatchProjectionState.RETIRED
            for match in projections
        )
        assert all(match.rank is None for match in projections)
