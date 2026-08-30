import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from matching.models import (
    Match,
    MatchOpportunity,
    MatchRevision,
    MatchRun,
    Person,
)


@pytest.mark.django_db
def test_match_run_supports_in_progress_updates(org_a, admin_profile):
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="AI project",
    )
    run = MatchRun.objects.create(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        request_hash="a" * 64,
        total_count=10,
    )

    assert run.started_at is None
    assert run.completed_at is None
    assert run.outcome == ""

    run.started_at = timezone.now()
    run.processed_count = 4
    run.save(update_fields=["started_at", "processed_count", "updated_at"])
    run.refresh_from_db()
    assert run.processed_count == 4
    assert run.started_at is not None


@pytest.mark.django_db
def test_match_revision_is_append_only_and_same_opportunity(org_a):
    person = Person.objects.create(org=org_a, display_name="Alice")
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="AI project",
    )
    other_opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="Other project",
    )
    match = Match.objects.create(
        org=org_a,
        person=person,
        opportunity=opportunity,
        ranking_revision=1,
    )
    run = MatchRun.objects.create(
        org=org_a,
        opportunity=opportunity,
        request_hash="b" * 64,
        ranking_revision=1,
    )
    revision = MatchRevision.objects.create(
        org=org_a,
        match=match,
        run=run,
        revision=1,
        snapshot={"overall_score": 80},
        evidence_snapshot=[],
    )

    revision.snapshot = {"overall_score": 0}
    with pytest.raises(ValidationError, match="cannot be updated"):
        revision.save()
    with pytest.raises(ValidationError, match="cannot be deleted"):
        revision.delete()
    with pytest.raises(ValidationError, match="cannot be updated"):
        match.revisions.update(snapshot={})
    with pytest.raises(ValidationError, match="cannot be deleted"):
        match.revisions.all().delete()

    other_run = MatchRun.objects.create(
        org=org_a,
        opportunity=other_opportunity,
        request_hash="c" * 64,
    )
    invalid = MatchRevision(
        org=org_a,
        match=match,
        run=other_run,
        revision=2,
    )
    with pytest.raises(ValidationError, match="target the match opportunity"):
        invalid.full_clean()
