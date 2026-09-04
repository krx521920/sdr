"""Prove that persisted matching work survives broker and worker interruption."""

from __future__ import annotations

import time
from datetime import timedelta
from uuid import UUID

from celery import current_app
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from automation.models import AutomationAttemptStatus, AutomationJobStatus
from automation.services import claim_job
from automation.tasks import dispatch_due_jobs, run_automation_job
from automation.tenant_context import database_org_context
from common.models import Org
from matching.models import Match, MatchOpportunity, MatchProjectionState, MatchRun, Person
from matching.services import enqueue_opportunity_recompute

FIXTURE_ORG_PREFIX = "matching-celery-recovery-"
EXPECTED_PERSON_COUNT = 2


class Command(BaseCommand):
    help = (
        "Prepare or verify a non-PII matching fixture used to prove Redis queue "
        "durability and stale Celery job recovery across a service restart."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--prepare",
            metavar="UUID",
            help="Create and publish the restart fixture while workers are stopped.",
        )
        mode.add_argument(
            "--verify",
            metavar="UUID",
            help="Verify the prepared fixture after Redis and workers restart.",
        )
        parser.add_argument(
            "--confirm-workers-stopped",
            action="store_true",
            help="Required acknowledgement for --prepare.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=120.0,
            help="Maximum seconds for each live Celery result (5-300; default: 120).",
        )

    def handle(self, *args, **options):
        token = self._parse_token(options["prepare"] or options["verify"])
        timeout = float(options["timeout"])
        if not 5.0 <= timeout <= 300.0:
            raise CommandError("timeout must be between 5 and 300 seconds.")
        if bool(current_app.conf.task_always_eager):
            raise CommandError(
                "Refusing to report a live Celery result while task_always_eager is enabled."
            )

        if options["prepare"]:
            if not options["confirm_workers_stopped"]:
                raise CommandError(
                    "--prepare requires --confirm-workers-stopped."
                )
            self._prepare(token)
            return
        if options["confirm_workers_stopped"]:
            raise CommandError("--confirm-workers-stopped is only valid with --prepare.")
        self._verify(token, timeout=timeout)

    @staticmethod
    def _parse_token(raw: str) -> UUID:
        try:
            return UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise CommandError("The fixture token must be a UUID.") from exc

    @staticmethod
    def _org_name(token: UUID) -> str:
        return f"{FIXTURE_ORG_PREFIX}{token.hex}"

    def _prepare(self, token: UUID) -> None:
        replies = current_app.control.inspect(timeout=2).ping()
        if replies:
            raise CommandError(
                "recovery_status=failed stage=prepare reason=workers_still_running"
            )

        org = Org.objects.filter(name=self._org_name(token)).first()
        if org is not None:
            raise CommandError(
                f"recovery_status=failed stage=prepare reason=fixture_exists org_id={org.id}"
            )
        org = Org.objects.create(name=self._org_name(token))

        try:
            with database_org_context(org.id):
                Person.objects.bulk_create(
                    [
                        Person(
                            org=org,
                            display_name=f"recovery-person-a-{token.hex}",
                            skills=["recovery-skill"],
                            availability="available",
                        ),
                        Person(
                            org=org,
                            display_name=f"recovery-person-b-{token.hex}",
                            skills=["recovery-skill"],
                            availability="available",
                        ),
                    ]
                )
                durable_opportunity = MatchOpportunity.objects.create(
                    org=org,
                    opportunity_type="employment",
                    status="open",
                    title=f"recovery-durable-{token.hex}",
                    required_criteria={"skills": ["recovery-skill"]},
                )
                stale_opportunity = MatchOpportunity.objects.create(
                    org=org,
                    opportunity_type="employment",
                    status="open",
                    title=f"recovery-stale-{token.hex}",
                    required_criteria={"skills": ["recovery-skill"]},
                )
                durable_run = enqueue_opportunity_recompute(
                    org=org,
                    opportunity=durable_opportunity,
                    requested_by=None,
                    person_ids=None,
                    idempotency_key=token,
                )
                stale_run = enqueue_opportunity_recompute(
                    org=org,
                    opportunity=stale_opportunity,
                    requested_by=None,
                    person_ids=None,
                    idempotency_key=token,
                )
                claimed = claim_job(
                    job_id=stale_run.automation_job_id,
                    org_id=org.id,
                )
                if claimed is None:
                    raise CommandError(
                        "recovery_status=failed stage=prepare reason=stale_claim_failed"
                    )
                stale_started_at = timezone.now() - timedelta(
                    seconds=settings.AUTOMATION_JOB_LEASE_SECONDS + 60
                )
                type(claimed.job).objects.filter(id=claimed.job.id).update(
                    started_at=stale_started_at
                )
                type(claimed.attempt).objects.filter(id=claimed.attempt.id).update(
                    started_at=stale_started_at
                )
        except Exception:
            Org.objects.filter(id=org.id).update(is_active=False)
            raise

        self.stdout.write(
            self.style.SUCCESS(
                "recovery_status=prepared "
                f"fixture_token={token} org_id={org.id} "
                f"durable_run_id={durable_run.id} stale_run_id={stale_run.id}"
            )
        )

    def _verify(self, token: UUID, *, timeout: float) -> None:
        org = Org.objects.filter(
            name=self._org_name(token),
            is_active=True,
        ).first()
        if org is None:
            raise CommandError(
                "recovery_status=failed stage=verify reason=fixture_not_found"
            )

        verification_error = None
        try:
            runs = self._fixture_runs(org.id, token)
            self._wait_for_runs(
                org_id=org.id,
                runs={"durable": runs["durable"]},
                timeout=timeout,
            )
            self._validate_durable_broker_delivery(org.id, runs["durable"])
            self.stdout.write(
                "broker_restart_delivery=ok source=redis_persisted_message attempts=1"
            )

            first_dispatch = self._dispatch_recovery(timeout)
            self.stdout.write(
                "recovery_dispatch_pass=1 "
                f"recovered={int(first_dispatch.get('recovered', 0))} "
                f"dispatched={int(first_dispatch.get('dispatched', 0))}"
            )

            time.sleep(settings.AUTOMATION_RETRY_BASE_SECONDS + 1)
            second_dispatch = self._dispatch_recovery(timeout)
            self.stdout.write(
                "recovery_dispatch_pass=2 "
                f"recovered={int(second_dispatch.get('recovered', 0))} "
                f"dispatched={int(second_dispatch.get('dispatched', 0))}"
            )

            self._wait_for_runs(
                org_id=org.id,
                runs={"stale": runs["stale"]},
                timeout=timeout,
            )
            self._validate_runs(org.id, runs)
            self._verify_duplicate_delivery(org.id, runs, timeout=timeout)
            self.stdout.write(
                self.style.SUCCESS(
                    "recovery_status=succeeded broker_restart_survival=ok "
                    "stale_running_recovery=ok duplicate_side_effects=0"
                )
            )
        except Exception as exc:
            verification_error = exc
        finally:
            Org.objects.filter(id=org.id, name=self._org_name(token)).update(
                is_active=False
            )
            self.stdout.write(f"cleanup_status=retained_inactive fixture_org_id={org.id}")

        if verification_error is not None:
            raise verification_error

    @staticmethod
    def _dispatch_recovery(timeout: float) -> dict:
        try:
            result = dispatch_due_jobs.apply_async().get(timeout=timeout, propagate=True)
        except Exception as exc:
            raise CommandError(
                "recovery_status=failed stage=dispatcher_timeout"
            ) from exc
        if not isinstance(result, dict):
            raise CommandError(
                "recovery_status=failed stage=dispatcher_result reason=invalid"
            )
        return result

    def _fixture_runs(self, org_id: UUID, token: UUID) -> dict[str, UUID]:
        with database_org_context(org_id):
            runs = list(
                MatchRun.objects.select_related("opportunity").filter(org_id=org_id)
            )
        by_label = {}
        for run in runs:
            if run.opportunity.title == f"recovery-durable-{token.hex}":
                by_label["durable"] = run.id
            elif run.opportunity.title == f"recovery-stale-{token.hex}":
                by_label["stale"] = run.id
        if set(by_label) != {"durable", "stale"}:
            raise CommandError(
                "recovery_status=failed stage=verify reason=fixture_runs_missing"
            )
        return by_label

    @staticmethod
    def _validate_durable_broker_delivery(org_id: UUID, run_id: UUID) -> None:
        with database_org_context(org_id):
            run = MatchRun.objects.select_related("automation_job").get(
                org_id=org_id,
                id=run_id,
            )
            if (
                run.automation_job.status != AutomationJobStatus.SUCCEEDED
                or run.automation_job.attempt_count != 1
            ):
                raise CommandError(
                    "recovery_status=failed stage=broker_restart_delivery "
                    f"status={run.automation_job.status} "
                    f"attempts={run.automation_job.attempt_count}"
                )

    def _wait_for_runs(
        self,
        *,
        org_id: UUID,
        runs: dict[str, UUID],
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        pending = dict(runs)
        while pending:
            with database_org_context(org_id):
                snapshots = {
                    run.id: run
                    for run in MatchRun.objects.select_related("automation_job").filter(
                        org_id=org_id,
                        id__in=pending.values(),
                    )
                }
            for label, run_id in list(pending.items()):
                run = snapshots.get(run_id)
                if run is None:
                    raise CommandError(
                        "recovery_status=failed stage=ledger_missing "
                        f"label={label} run_id={run_id}"
                    )
                if run.automation_job.status in {
                    AutomationJobStatus.DEAD_LETTER,
                    AutomationJobStatus.CANCELLED,
                }:
                    raise CommandError(
                        "recovery_status=failed stage=job_terminal "
                        f"label={label} status={run.automation_job.status}"
                    )
                if run.automation_job.status == AutomationJobStatus.SUCCEEDED:
                    pending.pop(label)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise CommandError(
                    "recovery_status=failed stage=timeout "
                    f"pending={','.join(sorted(pending))}"
                )
            time.sleep(0.5)

    def _validate_runs(self, org_id: UUID, runs: dict[str, UUID]) -> None:
        with database_org_context(org_id):
            snapshots = {
                run.id: run
                for run in MatchRun.objects.select_related("automation_job").filter(
                    org_id=org_id,
                    id__in=runs.values(),
                )
            }
            for label, run_id in runs.items():
                run = snapshots[run_id]
                match_count = Match.objects.filter(
                    org_id=org_id,
                    opportunity_id=run.opportunity_id,
                    projection_state=MatchProjectionState.CURRENT,
                ).count()
                expected_attempts = 1 if label == "durable" else 2
                if not (
                    run.outcome == "succeeded"
                    and run.processed_count == EXPECTED_PERSON_COUNT
                    and run.result_count == EXPECTED_PERSON_COUNT
                    and match_count == EXPECTED_PERSON_COUNT
                    and run.automation_job.attempt_count == expected_attempts
                ):
                    raise CommandError(
                        "recovery_status=failed stage=result_validation "
                        f"label={label} attempts={run.automation_job.attempt_count} "
                        f"processed={run.processed_count} matches={match_count}"
                    )

            stale_job = snapshots[runs["stale"]].automation_job
            attempts = list(stale_job.attempts.order_by("attempt_number"))
            if (
                len(attempts) != 2
                or attempts[0].status != AutomationAttemptStatus.FAILED
                or attempts[0].error_code != "execution_lease_expired"
                or attempts[1].status != AutomationAttemptStatus.SUCCEEDED
            ):
                raise CommandError(
                    "recovery_status=failed stage=attempt_history reason=invalid"
                )

    @staticmethod
    def _state(org_id: UUID, runs: dict[str, UUID]) -> tuple:
        with database_org_context(org_id):
            return tuple(
                (
                    run.id,
                    run.automation_job.attempt_count,
                    run.processed_count,
                    run.result_count,
                    run.completed_at,
                    Match.objects.filter(
                        org_id=org_id,
                        opportunity_id=run.opportunity_id,
                        projection_state=MatchProjectionState.CURRENT,
                    ).count(),
                )
                for run in MatchRun.objects.select_related("automation_job")
                .filter(org_id=org_id, id__in=runs.values())
                .order_by("id")
            )

    def _verify_duplicate_delivery(
        self,
        org_id: UUID,
        runs: dict[str, UUID],
        *,
        timeout: float,
    ) -> None:
        before = self._state(org_id, runs)
        pending = []
        with database_org_context(org_id):
            for run in MatchRun.objects.select_related("automation_job").filter(
                org_id=org_id,
                id__in=runs.values(),
            ):
                pending.append(
                    run_automation_job.apply_async(
                        args=[str(run.automation_job_id), str(org_id)],
                        queue=run.automation_job.queue,
                    )
                )
        for result in pending:
            payload = result.get(timeout=timeout, propagate=True)
            if not isinstance(payload, dict) or payload.get("status") != "skipped":
                raise CommandError(
                    "recovery_status=failed stage=duplicate_delivery reason=claimed"
                )
        if self._state(org_id, runs) != before:
            raise CommandError(
                "recovery_status=failed stage=duplicate_delivery reason=side_effect"
            )
