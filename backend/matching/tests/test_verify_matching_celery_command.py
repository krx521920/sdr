from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from automation.tasks import run_automation_job
from common.models import Org
from matching.management.commands.verify_matching_celery import Command

FIXTURE_PREFIX = "matching-celery-verification-"


class _ImmediateResult:
    def __init__(self, value):
        self.value = value

    def get(self, **_kwargs):
        return self.value


def _synchronous_duplicate_delivery(*, args, **_kwargs):
    return _ImmediateResult(run_automation_job.run(*args))


@pytest.mark.django_db(transaction=True)
def test_command_requires_explicit_confirmation_before_writing():
    with pytest.raises(CommandError, match="--confirm-live-celery"):
        call_command("verify_matching_celery")

    assert not Org.objects.filter(name__startswith=FIXTURE_PREFIX).exists()


@pytest.mark.django_db(transaction=True)
def test_command_verifies_two_durable_jobs_without_a_real_broker(monkeypatch):
    dispatched = []

    def synchronous_dispatch(job):
        dispatched.append((job.id, job.name))
        run_automation_job.run(str(job.id), str(job.org_id))
        return True

    monkeypatch.setattr("matching.services.dispatch_job", synchronous_dispatch)
    monkeypatch.setattr(
        run_automation_job,
        "apply_async",
        _synchronous_duplicate_delivery,
    )
    output = StringIO()

    call_command(
        "verify_matching_celery",
        confirm_live_celery=True,
        timeout=5,
        poll_interval=0.1,
        stdout=output,
    )

    assert len(dispatched) == 2
    assert {name for _job_id, name in dispatched} == {"matching.recompute_opportunity"}
    text = output.getvalue()
    assert "verification_status=succeeded" in text
    assert "directed_processed=1" in text
    assert "batch_processed=2" in text
    assert "match_count=3" in text
    assert "duplicate_delivery_status=safe skipped=2 side_effects=0" in text
    assert "cleanup_status=retained_inactive" in text
    fixture = Org.objects.get(name__startswith=FIXTURE_PREFIX)
    assert fixture.is_active is False


@pytest.mark.django_db(transaction=True)
def test_cleanup_failure_deactivates_fixture_and_exits_nonzero(monkeypatch):
    def synchronous_dispatch(job):
        run_automation_job.run(str(job.id), str(job.org_id))
        return True

    monkeypatch.setattr("matching.services.dispatch_job", synchronous_dispatch)
    monkeypatch.setattr(
        run_automation_job,
        "apply_async",
        _synchronous_duplicate_delivery,
    )
    monkeypatch.setattr(
        Command,
        "_deactivate_fixture",
        lambda self, org_id: (_ for _ in ()).throw(RuntimeError("blocked")),
    )

    with pytest.raises(CommandError, match=r"cleanup_status=failed.*deactivated=yes"):
        call_command(
            "verify_matching_celery",
            confirm_live_celery=True,
            timeout=5,
            poll_interval=0.1,
            stdout=StringIO(),
        )

    fixture = Org.objects.get(name__startswith=FIXTURE_PREFIX)
    assert fixture.is_active is False
