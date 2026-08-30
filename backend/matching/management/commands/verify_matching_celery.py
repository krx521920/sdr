"""Run an isolated, end-to-end Celery acceptance check for matching.

The command deliberately creates no users, identities, evidence, or channel
records.  Its fixture is a uniquely named temporary organization containing
only two synthetic people and two synthetic opportunities.  It is marked
inactive after the check so immutable matching history remains auditable.
"""

import time
import uuid
from dataclasses import dataclass

from celery import current_app
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from automation.models import AutomationJobStatus
from automation.tenant_context import database_org_context
from common.models import Org
from matching.models import (
    Match,
    MatchOpportunity,
    MatchProjectionState,
    MatchRun,
    Person,
)
from matching.services import enqueue_opportunity_recompute

CONFIRMATION_OPTION = "--confirm-live-celery"
FIXTURE_ORG_PREFIX = "matching-celery-verification-"
TERMINAL_FAILURE_STATUSES = {
    AutomationJobStatus.DEAD_LETTER,
    AutomationJobStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class ExpectedRun:
    label: str
    run_id: uuid.UUID
    expected_count: int


class Command(BaseCommand):
    help = (
        "Verify real matching execution through the configured Celery broker and "
        "worker using a temporary, non-PII organization."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            CONFIRMATION_OPTION,
            action="store_true",
            dest="confirm_live_celery",
            help=(
                "Required acknowledgement that two real jobs will be published "
                "to the configured Celery broker."
            ),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=120.0,
            help="Maximum seconds to wait for both jobs (5-900; default: 120).",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=1.0,
            help="Seconds between durable-ledger polls (0.1-10; default: 1).",
        )

    def handle(self, *args, **options):
        if not options["confirm_live_celery"]:
            raise CommandError(
                f"Refusing to publish jobs without {CONFIRMATION_OPTION}."
            )

        timeout = float(options["timeout"])
        poll_interval = float(options["poll_interval"])
        if not 5.0 <= timeout <= 900.0:
            raise CommandError("timeout must be between 5 and 900 seconds.")
        if not 0.1 <= poll_interval <= 10.0:
            raise CommandError("poll_interval must be between 0.1 and 10 seconds.")
        if bool(current_app.conf.task_always_eager):
            raise CommandError(
                "Refusing to report a live Celery result while task_always_eager is enabled."
            )

        fixture_id = uuid.uuid4()
        org = None
        verification_error = None
        try:
            org = Org.objects.create(
                name=f"{FIXTURE_ORG_PREFIX}{fixture_id.hex}",
            )
            self._populate_fixture(org, fixture_id)
            self.stdout.write(f"fixture_org_id={org.id} status=created")
            expected_runs = self._enqueue_fixture_runs(org)
            for expected in expected_runs:
                with database_org_context(org.id):
                    run = MatchRun.objects.select_related("automation_job").get(
                        id=expected.run_id,
                        org=org,
                    )
                self.stdout.write(
                    f"{expected.label}_run_id={run.id} "
                    f"job_id={run.automation_job_id} status={run.automation_job.status}"
                )

            self._wait_for_runs(
                org_id=org.id,
                expected_runs=expected_runs,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            counts = self._validate_runs(org.id, expected_runs)
            self.stdout.write(
                self.style.SUCCESS(
                    "verification_status=succeeded "
                    f"directed_processed={counts['directed']} "
                    f"batch_processed={counts['batch']} "
                    f"match_count={counts['matches']}"
                )
            )
        except CommandError as exc:
            verification_error = exc
        except Exception as exc:
            verification_error = CommandError(
                f"verification_status=failed stage=unexpected "
                f"fixture_org_id={getattr(org, 'id', fixture_id)}"
            )
            verification_error.__cause__ = exc
        finally:
            if org is not None:
                cleanup_error = self._retain_fixture_inactive(org.id)
                if cleanup_error is None:
                    self.stdout.write(
                        f"cleanup_status=retained_inactive fixture_org_id={org.id}"
                    )
                else:
                    raise cleanup_error from verification_error

        if verification_error is not None:
            raise verification_error

    def _populate_fixture(self, org: Org, fixture_id: uuid.UUID) -> None:
        with database_org_context(org.id):
            Person.objects.bulk_create(
                [
                    Person(
                        org=org,
                        display_name=f"fixture-person-a-{fixture_id.hex}",
                        skills=["fixture-skill"],
                        availability="available",
                    ),
                    Person(
                        org=org,
                        display_name=f"fixture-person-b-{fixture_id.hex}",
                        skills=["fixture-skill"],
                        availability="available",
                    ),
                ]
            )
            MatchOpportunity.objects.bulk_create(
                [
                    MatchOpportunity(
                        org=org,
                        opportunity_type="employment",
                        status="open",
                        title=f"fixture-directed-{fixture_id.hex}",
                        required_criteria={"skills": ["fixture-skill"]},
                    ),
                    MatchOpportunity(
                        org=org,
                        opportunity_type="employment",
                        status="open",
                        title=f"fixture-batch-{fixture_id.hex}",
                        required_criteria={"skills": ["fixture-skill"]},
                    ),
                ]
            )

    def _enqueue_fixture_runs(self, org: Org) -> tuple[ExpectedRun, ExpectedRun]:
        with database_org_context(org.id):
            people = list(Person.objects.filter(org=org).order_by("display_name"))
            opportunities = list(
                MatchOpportunity.objects.filter(org=org).order_by("title")
            )
            if len(people) != 2 or len(opportunities) != 2:
                raise CommandError(
                    f"verification_status=failed stage=fixture_count fixture_org_id={org.id}"
                )
            opportunity_by_scope = {
                "batch": next(
                    item for item in opportunities if "-batch-" in item.title
                ),
                "directed": next(
                    item for item in opportunities if "-directed-" in item.title
                ),
            }
            directed = enqueue_opportunity_recompute(
                org=org,
                opportunity=opportunity_by_scope["directed"],
                requested_by=None,
                person_ids=[people[0].id],
                idempotency_key=uuid.uuid4(),
            )
            batch = enqueue_opportunity_recompute(
                org=org,
                opportunity=opportunity_by_scope["batch"],
                requested_by=None,
                person_ids=None,
                idempotency_key=uuid.uuid4(),
            )
        return (
            ExpectedRun("directed", directed.id, 1),
            ExpectedRun("batch", batch.id, 2),
        )

    def _wait_for_runs(
        self,
        *,
        org_id: uuid.UUID,
        expected_runs: tuple[ExpectedRun, ...],
        timeout: float,
        poll_interval: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        pending_ids = {expected.run_id for expected in expected_runs}
        while pending_ids:
            with database_org_context(org_id):
                snapshots = {
                    run.id: run
                    for run in MatchRun.objects.select_related("automation_job").filter(
                        org_id=org_id,
                        id__in=pending_ids,
                    )
                }
            if set(snapshots) != pending_ids:
                raise CommandError(
                    f"verification_status=failed stage=ledger_missing fixture_org_id={org_id}"
                )
            for run_id, run in snapshots.items():
                status = run.automation_job.status
                if status in TERMINAL_FAILURE_STATUSES:
                    raise CommandError(
                        "verification_status=failed stage=job_terminal "
                        f"fixture_org_id={org_id} run_id={run_id} status={status} "
                        f"error_code={run.automation_job.last_error_code or 'none'}"
                    )
                if status == AutomationJobStatus.SUCCEEDED:
                    pending_ids.remove(run_id)
            if not pending_ids:
                return
            if time.monotonic() >= deadline:
                statuses = ",".join(
                    sorted(
                        f"{run_id}:{snapshots[run_id].automation_job.status}"
                        for run_id in pending_ids
                    )
                )
                raise CommandError(
                    "verification_status=failed stage=timeout "
                    f"fixture_org_id={org_id} pending={statuses}"
                )
            time.sleep(poll_interval)

    def _validate_runs(
        self,
        org_id: uuid.UUID,
        expected_runs: tuple[ExpectedRun, ...],
    ) -> dict[str, int]:
        counts = {"matches": 0}
        with database_org_context(org_id):
            for expected in expected_runs:
                run = MatchRun.objects.select_related("automation_job").get(
                    org_id=org_id,
                    id=expected.run_id,
                )
                match_count = Match.objects.filter(
                    org_id=org_id,
                    opportunity_id=run.opportunity_id,
                    projection_state=MatchProjectionState.CURRENT,
                ).count()
                valid = (
                    run.automation_job.status == AutomationJobStatus.SUCCEEDED
                    and run.outcome == "succeeded"
                    and run.completed_at is not None
                    and run.total_count == expected.expected_count
                    and run.processed_count == expected.expected_count
                    and run.result_count == expected.expected_count
                    and len(run.requested_person_ids) == expected.expected_count
                    and match_count == expected.expected_count
                )
                if not valid:
                    raise CommandError(
                        "verification_status=failed stage=result_validation "
                        f"fixture_org_id={org_id} run_id={run.id} "
                        f"status={run.automation_job.status} "
                        f"processed={run.processed_count} "
                        f"result={run.result_count if run.result_count is not None else 'none'} "
                        f"matches={match_count}"
                    )
                counts[expected.label] = run.processed_count
                counts["matches"] += match_count
        return counts

    def _retain_fixture_inactive(self, org_id: uuid.UUID) -> CommandError | None:
        try:
            self._deactivate_fixture(org_id)
            return None
        except Exception as exc:
            deactivated = self._mark_fixture_inactive(org_id)
            error = CommandError(
                "cleanup_status=failed "
                f"fixture_org_id={org_id} deactivated={'yes' if deactivated else 'no'}"
            )
            error.__cause__ = exc
            return error

    def _deactivate_fixture(self, org_id: uuid.UUID) -> None:
        with transaction.atomic():
            org = Org.objects.select_for_update().filter(id=org_id).first()
            if org is None:
                raise RuntimeError("fixture organization no longer exists")
            if not str(org.name or "").startswith(FIXTURE_ORG_PREFIX):
                raise RuntimeError("refusing to change a non-fixture organization")
            org.is_active = False
            org.save(update_fields=["is_active", "updated_at"])

    def _mark_fixture_inactive(self, org_id: uuid.UUID) -> bool:
        try:
            return bool(
                Org.objects.filter(
                    id=org_id,
                    name__startswith=FIXTURE_ORG_PREFIX,
                ).update(is_active=False)
            )
        except Exception:
            return False
