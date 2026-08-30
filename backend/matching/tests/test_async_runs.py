from uuid import uuid4

import pytest
from django.db import OperationalError
from django.test import override_settings

from automation.errors import PermanentJobError
from automation.models import AutomationJob, AutomationJobStatus
from automation.tasks import run_automation_job
from matching.jobs import process_recompute_opportunity_job
from matching.models import (
    Match,
    MatchOpportunity,
    MatchRevision,
    MatchRevisionKind,
    MatchRun,
    Person,
)
from matching.services import enqueue_opportunity_recompute


def _opportunity(org, *, status="open"):
    return MatchOpportunity.objects.create(
        org=org,
        opportunity_type="employment",
        status=status,
        title="Async matching test",
        required_criteria={"skills": ["python"]},
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_recompute_api_requires_uuid_idempotency_key(admin_client, org_a):
    opportunity = _opportunity(org_a)

    missing = admin_client.post(
        f"/api/matching/opportunities/{opportunity.id}/recompute/",
        {},
        format="json",
    )
    invalid = admin_client.post(
        f"/api/matching/opportunities/{opportunity.id}/recompute/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="not-a-uuid",
    )

    assert missing.status_code == 400
    assert invalid.status_code == 400
    assert "idempotency_key" in invalid.json()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_recompute_api_returns_safe_202_and_is_idempotent(
    admin_client,
    org_a,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    opportunity = _opportunity(org_a)
    person = Person.objects.create(
        org=org_a,
        display_name="Alice",
        skills=["python"],
    )
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    key = str(uuid4())
    url = f"/api/matching/opportunities/{opportunity.id}/recompute/"

    with django_capture_on_commit_callbacks(execute=True):
        first = admin_client.post(
            url,
            {"person_ids": [str(person.id)]},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
    with django_capture_on_commit_callbacks(execute=True):
        replay = admin_client.post(
            url,
            {"person_ids": [str(person.id)]},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    assert first.status_code == 202
    assert first.headers["Retry-After"] == "2"
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert MatchRun.objects.filter(org=org_a).count() == 1
    assert AutomationJob.objects.filter(org=org_a).count() == 1
    assert set(first.json()).isdisjoint(
        {"request_hash", "requested_person_ids", "payload", "result", "last_error_message"}
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_recompute_api_rejects_idempotency_key_reuse_with_different_body(
    admin_client,
    org_a,
    monkeypatch,
):
    opportunity = _opportunity(org_a)
    first_person = Person.objects.create(org=org_a, display_name="Alice")
    second_person = Person.objects.create(org=org_a, display_name="Bob")
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    key = str(uuid4())
    url = f"/api/matching/opportunities/{opportunity.id}/recompute/"

    first = admin_client.post(
        url,
        {"person_ids": [str(first_person.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    conflict = admin_client.post(
        url,
        {"person_ids": [str(second_person.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert MatchRun.objects.filter(org=org_a).count() == 1


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_broker_failure_keeps_persisted_run_visible(
    admin_client,
    org_a,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    opportunity = _opportunity(org_a)
    monkeypatch.setattr(
        "matching.services.dispatch_job",
        lambda job: (_ for _ in ()).throw(ConnectionError("broker down")),
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            f"/api/matching/opportunities/{opportunity.id}/recompute/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )

    assert response.status_code == 202
    run = MatchRun.objects.get(id=response.json()["id"])
    assert run.automation_job.status == AutomationJobStatus.PENDING
    detail = admin_client.get(response.json()["status_url"])
    assert detail.status_code == 200
    assert detail.json()["status"] == AutomationJobStatus.PENDING


@pytest.mark.django_db
def test_handler_completes_run_and_is_idempotent(org_a, admin_profile, monkeypatch):
    opportunity = _opportunity(org_a)
    person = Person.objects.create(
        org=org_a,
        display_name="Alice",
        skills=["python"],
    )
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=[person.id],
        idempotency_key=uuid4(),
    )

    first = process_recompute_opportunity_job(run.automation_job.payload)
    second = process_recompute_opportunity_job(run.automation_job.payload)

    run.refresh_from_db()
    opportunity.refresh_from_db()
    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert run.outcome == "succeeded"
    assert run.processed_count == 1
    assert opportunity.ranking_revision == 1
    assert MatchRevision.objects.filter(org=org_a, run=run).count() == 1


@pytest.mark.django_db
def test_handler_skips_non_executable_opportunity(org_a, admin_profile, monkeypatch):
    opportunity = _opportunity(org_a, status="paused")
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=[],
        idempotency_key=uuid4(),
    )

    result = process_recompute_opportunity_job(run.automation_job.payload)

    run.refresh_from_db()
    assert result["status"] == "skipped"
    assert result["reason"] == "opportunity_paused"
    assert run.completed_at is not None


@pytest.mark.django_db
def test_handler_fails_closed_when_snapshotted_person_becomes_inactive(
    org_a,
    admin_profile,
    monkeypatch,
):
    opportunity = _opportunity(org_a)
    person = Person.objects.create(org=org_a, display_name="Alice")
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=[person.id],
        idempotency_key=uuid4(),
    )
    person.status = "inactive"
    person.save(update_fields=["status", "updated_at"])

    with pytest.raises(PermanentJobError) as exc_info:
        process_recompute_opportunity_job(run.automation_job.payload)

    assert exc_info.value.code == "matching_candidate_snapshot_changed"
    opportunity.refresh_from_db()
    run.refresh_from_db()
    assert opportunity.ranking_revision == 0
    assert run.ranking_revision is None
    assert Match.objects.filter(opportunity=opportunity).count() == 0
    assert MatchRevision.objects.filter(run=run).count() == 0


@pytest.mark.django_db
def test_partial_recompute_versions_matches_whose_rank_changed(
    org_a,
    admin_profile,
    monkeypatch,
):
    opportunity = _opportunity(org_a)
    alice = Person.objects.create(
        org=org_a,
        display_name="Alice",
        skills=["python"],
    )
    bob = Person.objects.create(org=org_a, display_name="Bob")
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)

    first_run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=None,
        idempotency_key=uuid4(),
    )
    process_recompute_opportunity_job(first_run.automation_job.payload)
    assert Match.objects.get(person=alice, opportunity=opportunity).rank == 1

    bob.skills = ["python"]
    bob.availability = "available"
    bob.save(update_fields=["skills", "availability", "updated_at"])
    second_run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=[bob.id],
        idempotency_key=uuid4(),
    )
    process_recompute_opportunity_job(second_run.automation_job.payload)

    alice_match = Match.objects.get(person=alice, opportunity=opportunity)
    bob_match = Match.objects.get(person=bob, opportunity=opportunity)
    assert bob_match.rank == 1
    assert alice_match.rank == 2
    assert alice_match.ranking_revision == 2
    assert MatchRevision.objects.filter(run=second_run).count() == 2
    assert MatchRevision.objects.get(
        run=second_run,
        match=alice_match,
    ).revision_kind == MatchRevisionKind.RERANK


@pytest.mark.django_db
def test_handler_uses_opportunity_row_lock(
    org_a,
    admin_profile,
    monkeypatch,
):
    opportunity = _opportunity(org_a)
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=[],
        idempotency_key=uuid4(),
    )
    manager = MatchOpportunity.objects
    original = manager.select_for_update
    calls = []

    def tracked(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "select_for_update", tracked)
    process_recompute_opportunity_job(run.automation_job.payload)

    assert calls


@pytest.mark.django_db
@override_settings(AUTOMATION_RETRY_BASE_SECONDS=1, AUTOMATION_RETRY_MAX_SECONDS=1)
def test_temporary_database_failure_enters_retry_schedule(
    org_a,
    admin_profile,
    monkeypatch,
):
    opportunity = _opportunity(org_a)
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)
    run = enqueue_opportunity_recompute(
        org=org_a,
        opportunity=opportunity,
        requested_by=admin_profile,
        person_ids=[],
        idempotency_key=uuid4(),
    )
    monkeypatch.setattr(
        "matching.jobs.execute_opportunity_recompute",
        lambda **kwargs: (_ for _ in ()).throw(OperationalError("temporary")),
    )
    monkeypatch.setattr("automation.tasks.dispatch_job", lambda job: True)

    result = run_automation_job.run(
        str(run.automation_job_id),
        str(org_a.id),
    )

    run.automation_job.refresh_from_db()
    assert result["status"] == AutomationJobStatus.RETRY_SCHEDULED
    assert run.automation_job.status == AutomationJobStatus.RETRY_SCHEDULED
    assert run.automation_job.last_error_code == "matching_database_unavailable"
