from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from matching.models import (
    Evidence,
    Match,
    MatchOpportunity,
    MatchStatus,
    Person,
    PersonIdentity,
)
from matching.services import evaluate_person, recompute_opportunity_matches


@pytest.mark.django_db
def test_recompute_ranks_people_and_cites_supporting_evidence(org_a):
    strong = Person.objects.create(
        org=org_a,
        display_name="Alice",
        current_title="Senior SDR",
        location="Shanghai",
        availability="available",
        skills=["sales automation"],
    )
    weak = Person.objects.create(
        org=org_a,
        display_name="Bob",
        current_title="Designer",
        location="London",
        availability="busy",
        skills=["illustration"],
    )
    evidence = Evidence.objects.create(
        org=org_a,
        person=strong,
        kind="skill",
        source="linkedin",
        summary="Profile shows Python and Django delivery experience.",
        facts={"skills": ["Python", "Django"], "titles": ["SDR"]},
        source_record_id="linkedin-alice-1",
        confidence=Decimal("0.900"),
    )
    Evidence.objects.create(
        org=org_a,
        person=strong,
        kind="relationship",
        source="wechat",
        summary="Worked together successfully.",
        facts={"relationship_strength": 0.9},
        confidence=Decimal("0.800"),
    )
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="employment",
        status="open",
        title="AI SDR builder",
        required_criteria={"skills": ["python", "django"]},
        preferred_criteria={
            "titles": ["sdr"],
            "locations": ["shanghai"],
            "availability": ["available"],
        },
    )

    ranked = recompute_opportunity_matches(org=org_a, opportunity=opportunity)

    assert [item.person_id for item in ranked] == [strong.id, weak.id]
    assert ranked[0].rank == 1
    assert ranked[0].eligibility_score == 100
    assert ranked[0].overall_score > ranked[1].overall_score
    assert ranked[1].overall_score <= 49
    assert ranked[0].evidence_links.filter(evidence=evidence).exists()
    assert ranked[0].engine_version == "rules-v1"

    partial = recompute_opportunity_matches(
        org=org_a,
        opportunity=opportunity,
        people=Person.objects.filter(id=weak.id),
    )
    assert partial[0].rank == 2
    assert set(
        Match.objects.filter(org=org_a, opportunity=opportunity).values_list(
            "rank", flat=True
        )
    ) == {1, 2}


@pytest.mark.django_db
def test_recompute_preserves_human_review_status(org_a):
    Person.objects.create(org=org_a, display_name="Alice", skills=["python"])
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="Python project",
        required_criteria={"skills": ["python"]},
    )
    match = recompute_opportunity_matches(org=org_a, opportunity=opportunity)[0]
    match.status = MatchStatus.ACCEPTED
    match.save()

    refreshed = recompute_opportunity_matches(org=org_a, opportunity=opportunity)[0]

    assert refreshed.status == MatchStatus.ACCEPTED


@pytest.mark.django_db
def test_expired_evidence_is_not_used(org_a):
    person = Person.objects.create(org=org_a, display_name="Alice")
    expired = Evidence.objects.create(
        org=org_a,
        person=person,
        kind="skill",
        source="manual",
        summary="Old certification",
        facts={"skills": ["python"]},
        observed_at=timezone.now() - timedelta(days=10),
        valid_until=timezone.now() - timedelta(days=1),
        confidence=Decimal("1.000"),
    )
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="Python project",
        required_criteria={"skills": ["python"]},
    )

    evaluation = evaluate_person(person, opportunity)

    assert evaluation.defaults["eligibility_score"] == 0
    assert all(
        item["evidence"].id != expired.id for item in evaluation.evidence_contributions
    )


@pytest.mark.django_db
def test_future_evidence_is_not_used_and_recompute_locks_opportunity(org_a):
    person = Person.objects.create(org=org_a, display_name="Alice")
    future = Evidence.objects.create(
        org=org_a,
        person=person,
        kind="skill",
        source="manual",
        summary="Certification is not observable yet",
        facts={"skills": ["python"]},
        observed_at=timezone.now() + timedelta(days=1),
        confidence=Decimal("1.000"),
    )
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="Python project",
        required_criteria={"skills": ["python"]},
    )

    manager = MatchOpportunity.objects
    with patch.object(
        manager,
        "select_for_update",
        wraps=manager.select_for_update,
    ) as select_for_update:
        match = recompute_opportunity_matches(
            org=org_a,
            opportunity=opportunity,
        )[0]

    select_for_update.assert_called_once_with()
    assert match.eligibility_score == 0
    assert not match.evidence_links.filter(evidence=future).exists()


@pytest.mark.django_db
def test_related_records_reject_cross_org_links(org_a, org_b):
    person = Person.objects.create(org=org_a, display_name="Alice")
    identity = PersonIdentity(
        org=org_b,
        person=person,
        kind="email",
        normalized_value="alice@example.com",
    )

    with pytest.raises(ValidationError, match="person's org"):
        identity.full_clean()
