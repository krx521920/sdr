import pytest
from django.test import override_settings
from django.utils import timezone

from automation.jobs import JobRequest
from automation.models import (
    AutomationAttemptStatus,
    AutomationJob,
    AutomationJobStatus,
)
from automation.services import dispatch_job, enqueue_job
from automation.tasks import run_automation_job

TEST_HANDLERS = {
    "test.echo": "automation.tests.handlers.echo_job",
    "test.retryable": "automation.tests.handlers.retryable_failure",
    "test.permanent": "automation.tests.handlers.permanent_failure",
}


@pytest.mark.django_db
def test_enqueue_job_is_idempotent_per_tenant(org_a, org_b):
    first = enqueue_job(
        JobRequest(
            org_id=org_a.id,
            name="test.echo",
            idempotency_key="event-1",
            payload={"value": "first"},
        )
    )
    replay = enqueue_job(
        JobRequest(
            org_id=org_a.id,
            name="test.echo",
            idempotency_key="event-1",
            payload={"value": "changed"},
        )
    )
    other_tenant = enqueue_job(
        JobRequest(
            org_id=org_b.id,
            name="test.echo",
            idempotency_key="event-1",
        )
    )

    assert first.created is True
    assert replay.created is False
    assert replay.job.id == first.job.id
    assert replay.job.payload == {"value": "first"}
    assert other_tenant.job.id != first.job.id


@pytest.mark.django_db
def test_broker_failure_leaves_persisted_job_recoverable(org_a, monkeypatch):
    enqueued = enqueue_job(
        JobRequest(
            org_id=org_a.id,
            name="test.echo",
            idempotency_key="event-broker-down",
        )
    )
    monkeypatch.setattr(
        "automation.tasks.run_automation_job.apply_async",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("broker down")),
    )

    with pytest.raises(ConnectionError, match="broker down"):
        dispatch_job(enqueued.job)

    enqueued.job.refresh_from_db()
    assert enqueued.job.status == AutomationJobStatus.PENDING
    assert enqueued.job.last_error_code == "broker_unavailable"


@pytest.mark.django_db
@override_settings(AUTOMATION_JOB_HANDLERS=TEST_HANDLERS)
def test_runner_completes_job_and_attempt_audit(org_a):
    enqueued = enqueue_job(
        JobRequest(
            org_id=org_a.id,
            name="test.echo",
            idempotency_key="event-success",
            payload={"value": "done"},
        )
    )

    task_result = run_automation_job.run(str(enqueued.job.id), str(org_a.id))

    enqueued.job.refresh_from_db()
    attempt = enqueued.job.attempts.get()
    assert task_result["status"] == AutomationJobStatus.SUCCEEDED
    assert enqueued.job.status == AutomationJobStatus.SUCCEEDED
    assert enqueued.job.result == {"echo": "done"}
    assert attempt.status == AutomationAttemptStatus.SUCCEEDED
    assert attempt.finished_at is not None


@pytest.mark.django_db
@override_settings(
    AUTOMATION_JOB_HANDLERS=TEST_HANDLERS,
    AUTOMATION_RETRY_BASE_SECONDS=0,
    AUTOMATION_RETRY_MAX_SECONDS=0,
)
def test_retryable_job_moves_to_dead_letter_after_attempt_limit(org_a, monkeypatch):
    monkeypatch.setattr("automation.tasks.dispatch_job", lambda job: True)
    enqueued = enqueue_job(
        JobRequest(
            org_id=org_a.id,
            name="test.retryable",
            idempotency_key="event-retry",
            max_attempts=2,
        )
    )

    first = run_automation_job.run(str(enqueued.job.id), str(org_a.id))
    second = run_automation_job.run(str(enqueued.job.id), str(org_a.id))

    enqueued.job.refresh_from_db()
    assert first["status"] == AutomationJobStatus.RETRY_SCHEDULED
    assert second["status"] == AutomationJobStatus.DEAD_LETTER
    assert enqueued.job.status == AutomationJobStatus.DEAD_LETTER
    assert enqueued.job.attempt_count == 2
    assert (
        enqueued.job.attempts.filter(status=AutomationAttemptStatus.FAILED).count() == 2
    )
    assert enqueued.job.last_error_code == "test_temporary"


@pytest.mark.django_db
@override_settings(AUTOMATION_JOB_HANDLERS=TEST_HANDLERS)
def test_permanent_failure_enters_dead_letter_without_retry(org_a, monkeypatch):
    monkeypatch.setattr("automation.tasks.dispatch_job", lambda job: True)
    enqueued = enqueue_job(
        JobRequest(
            org_id=org_a.id,
            name="test.permanent",
            idempotency_key="event-permanent",
            max_attempts=5,
        )
    )

    run_automation_job.run(str(enqueued.job.id), str(org_a.id))

    enqueued.job.refresh_from_db()
    assert enqueued.job.status == AutomationJobStatus.DEAD_LETTER
    assert enqueued.job.attempt_count == 1
    assert enqueued.job.last_error_code == "test_permanent"


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="automation.tests.urls",
    AUTOMATION_MANUAL_RETRY_ATTEMPTS=3,
)
def test_admin_can_list_and_replay_only_own_tenant_jobs(
    admin_client,
    user_client,
    org_b_client,
    org_a,
    monkeypatch,
):
    job = AutomationJob.objects.create(
        org=org_a,
        name="test.echo",
        idempotency_key="dead-event",
        status=AutomationJobStatus.DEAD_LETTER,
        max_attempts=2,
        attempt_count=2,
        scheduled_for=timezone.now(),
    )
    monkeypatch.setattr("automation.views.dispatch_job", lambda replayed: True)

    denied = user_client.get("/api/automation/jobs/")
    listed = admin_client.get("/api/automation/jobs/")
    other_tenant = org_b_client.get("/api/automation/jobs/")
    replayed = admin_client.post(f"/api/automation/jobs/{job.id}/retry/")

    assert denied.status_code == 403
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["summary"] == {"dead_letter": 1}
    assert other_tenant.json()["count"] == 0
    assert replayed.status_code == 200
    job.refresh_from_db()
    assert job.status == AutomationJobStatus.PENDING
    assert job.max_attempts == 5
    assert job.replay_count == 1
