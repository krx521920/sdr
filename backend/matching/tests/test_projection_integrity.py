from uuid import uuid4

import pytest
from django.db import connection
from django.test import override_settings

from matching.governance import mutate_person_governance
from matching.models import (
    Match,
    MatchOpportunity,
    MatchProjectionState,
    MatchRevision,
    MatchRevisionKind,
    MatchStatus,
    Person,
    PersonGovernanceStatus,
    PersonStatus,
)
from matching.services import recompute_opportunity_matches


def _ranked_graph(org):
    strong = Person.objects.create(
        org=org,
        display_name="Strong candidate",
        skills=["python"],
        availability="available",
    )
    remaining = Person.objects.create(
        org=org,
        display_name="Remaining candidate",
        skills=["design"],
        availability="unknown",
    )
    opportunity = MatchOpportunity.objects.create(
        org=org,
        opportunity_type="project",
        title="Projection integrity",
        required_criteria={"skills": ["python"]},
    )
    ranked = recompute_opportunity_matches(org=org, opportunity=opportunity)
    assert [item.person_id for item in ranked] == [strong.id, remaining.id]
    return strong, remaining, opportunity


@pytest.mark.django_db
def test_ineligible_transition_retires_projection_and_contiguously_reranks(org_a):
    strong, remaining, opportunity = _ranked_graph(org_a)
    match = Match.objects.get(org=org_a, person=strong, opportunity=opportunity)
    match.status = MatchStatus.SHORTLISTED
    match.save(update_fields=["status"])

    strong.status = PersonStatus.INACTIVE
    strong.save(update_fields=["status"])

    match.refresh_from_db()
    remaining_match = Match.objects.get(
        org=org_a,
        person=remaining,
        opportunity=opportunity,
    )
    assert match.projection_state == MatchProjectionState.RETIRED
    assert match.rank is None
    assert match.retired_at is not None
    assert match.retirement_reason == "person_status_inactive"
    assert match.status == MatchStatus.SHORTLISTED
    assert remaining_match.projection_state == MatchProjectionState.CURRENT
    assert remaining_match.rank == 1
    assert MatchRevision.objects.filter(
        org=org_a,
        match=match,
        revision_kind=MatchRevisionKind.RETIREMENT,
    ).exists()
    assert MatchRevision.objects.filter(
        org=org_a,
        match=remaining_match,
        revision_kind=MatchRevisionKind.RERANK,
    ).exists()


@pytest.mark.django_db
def test_unsaved_ineligible_value_does_not_retire_projection(org_a):
    strong, _remaining, opportunity = _ranked_graph(org_a)
    match = Match.objects.get(org=org_a, person=strong, opportunity=opportunity)

    strong.status = PersonStatus.INACTIVE
    strong.display_name = "Renamed only"
    strong.save(update_fields=["display_name"])

    strong.refresh_from_db()
    match.refresh_from_db()
    assert strong.status == PersonStatus.ACTIVE
    assert strong.display_name == "Renamed only"
    assert match.projection_state == MatchProjectionState.CURRENT
    assert match.rank == 1


@pytest.mark.django_db
def test_deletion_cancellation_requires_explicit_recompute_to_restore_match(
    org_a,
    admin_profile,
):
    strong, _remaining, opportunity = _ranked_graph(org_a)
    match = Match.objects.get(org=org_a, person=strong, opportunity=opportunity)

    requested = mutate_person_governance(
        org=org_a,
        person_id=strong.id,
        actor=admin_profile,
        idempotency_key=uuid4(),
        expected_revision=0,
        action="request",
    )
    assert requested.value.governance_status == PersonGovernanceStatus.DELETION_REQUESTED
    match.refresh_from_db()
    assert match.projection_state == MatchProjectionState.RETIRED

    cancelled = mutate_person_governance(
        org=org_a,
        person_id=strong.id,
        actor=admin_profile,
        idempotency_key=uuid4(),
        expected_revision=1,
        action="cancel",
    )
    assert cancelled.value.governance_status == PersonGovernanceStatus.ACTIVE
    match.refresh_from_db()
    assert match.projection_state == MatchProjectionState.RETIRED

    recompute_opportunity_matches(
        org=org_a,
        opportunity=opportunity,
        people=Person.objects.filter(id=strong.id),
    )
    match.refresh_from_db()
    assert match.projection_state == MatchProjectionState.CURRENT
    assert match.retired_at is None
    assert match.retirement_reason == ""
    assert MatchRevision.objects.filter(
        org=org_a,
        match=match,
        revision_kind=MatchRevisionKind.RETIREMENT,
    ).exists()


@pytest.mark.skipif(
    connection.vendor == "postgresql",
    reason="PostgreSQL rejects the bypass at its Person projection trigger.",
)
@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_default_reads_mutations_and_global_ranking_hide_bypassed_ineligible_person(
    admin_client,
    org_a,
):
    strong, remaining, opportunity = _ranked_graph(org_a)
    stale = Match.objects.get(org=org_a, person=strong, opportunity=opportunity)

    # QuerySet.update intentionally bypasses Person.save() on SQLite. Every
    # consumer still has to enforce Person eligibility independently.
    Person.objects.filter(org=org_a, id=strong.id).update(status=PersonStatus.INACTIVE)

    listed = admin_client.get(
        f"/api/matching/opportunities/{opportunity.id}/matches/"
    )
    detail = admin_client.get(f"/api/matching/matches/{stale.id}/")
    feedback_queue = admin_client.get("/api/matching/feedback/matches/")
    feedback_detail = admin_client.get(
        f"/api/matching/feedback/matches/{stale.id}/"
    )
    feedback_mutation = admin_client.post(
        f"/api/matching/matches/{stale.id}/feedback/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    decision_mutation = admin_client.patch(
        f"/api/matching/matches/{stale.id}/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert listed.status_code == 200
    assert [item["person"] for item in listed.json()["results"]] == [
        str(remaining.id)
    ]
    assert detail.status_code == 404
    assert feedback_queue.status_code == 200
    assert [
        item["person"]["id"] for item in feedback_queue.json()["results"]
    ] == [str(remaining.id)]
    assert feedback_detail.status_code == 404
    assert feedback_mutation.status_code == 404
    assert decision_mutation.status_code == 404

    recompute_opportunity_matches(
        org=org_a,
        opportunity=opportunity,
        people=Person.objects.filter(id=remaining.id),
    )
    stale.refresh_from_db()
    remaining_match = Match.objects.get(
        org=org_a,
        person=remaining,
        opportunity=opportunity,
    )
    assert stale.projection_state == MatchProjectionState.CURRENT
    assert stale.rank is None
    assert remaining_match.rank == 1


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_retired_projection_history_remains_readable(admin_client, org_a):
    strong, _remaining, opportunity = _ranked_graph(org_a)
    match = Match.objects.get(org=org_a, person=strong, opportunity=opportunity)
    strong.status = PersonStatus.INACTIVE
    strong.save(update_fields=["status"])

    current_detail = admin_client.get(f"/api/matching/matches/{match.id}/")
    revisions = admin_client.get(
        f"/api/matching/matches/{match.id}/revisions/"
    )
    decisions = admin_client.get(
        f"/api/matching/matches/{match.id}/decisions/"
    )

    assert current_detail.status_code == 404
    assert revisions.status_code == 200
    assert revisions.json()["count"] >= 1
    assert decisions.status_code == 200
