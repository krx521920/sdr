from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from automation.models import AutomationJobStatus
from common.models import Org
from matching.management.commands.verify_matching_recovery import FIXTURE_ORG_PREFIX
from matching.models import MatchRun


def test_recovery_command_rejects_invalid_fixture_token():
    with pytest.raises(CommandError, match="must be a UUID"):
        call_command(
            "verify_matching_recovery",
            "--prepare",
            "not-a-uuid",
            "--confirm-workers-stopped",
        )


def test_recovery_prepare_requires_explicit_worker_acknowledgement():
    with pytest.raises(CommandError, match="requires --confirm-workers-stopped"):
        call_command("verify_matching_recovery", "--prepare", str(uuid4()))


@pytest.mark.django_db
def test_recovery_prepare_persists_durable_and_stale_runs(monkeypatch):
    token = uuid4()
    inspector = SimpleNamespace(ping=lambda: None)
    monkeypatch.setattr(
        "matching.management.commands.verify_matching_recovery.current_app.control.inspect",
        lambda timeout: inspector,
    )
    monkeypatch.setattr("matching.services.dispatch_job", lambda job: True)

    call_command(
        "verify_matching_recovery",
        "--prepare",
        str(token),
        "--confirm-workers-stopped",
    )

    org = Org.objects.get(name=f"{FIXTURE_ORG_PREFIX}{token.hex}")
    runs = list(
        MatchRun.objects.filter(org=org)
        .select_related("automation_job", "opportunity")
        .order_by("opportunity__title")
    )
    assert len(runs) == 2
    by_title = {run.opportunity.title: run for run in runs}
    durable = by_title[f"recovery-durable-{token.hex}"]
    stale = by_title[f"recovery-stale-{token.hex}"]
    assert durable.automation_job.status == AutomationJobStatus.PENDING
    assert durable.automation_job.attempt_count == 0
    assert stale.automation_job.status == AutomationJobStatus.RUNNING
    assert stale.automation_job.attempt_count == 1
    assert stale.automation_job.attempts.count() == 1
