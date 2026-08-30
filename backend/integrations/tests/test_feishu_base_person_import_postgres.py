from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Event, Lock
from uuid import uuid4

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import override_settings
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.tenant_context import database_org_context
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    reconcile_stale_reserved,
    reconcile_stale_sending,
)
from integrations.models import (
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    FeishuBaseConnection,
    FeishuBasePersonImport,
    FeishuBasePersonImportStatus,
)
from integrations.providers.feishu_base.client import FeishuBaseRecord
from integrations.providers.feishu_base.person_import import (
    enqueue_feishu_person_import,
    feishu_person_import_execution_intent,
    process_feishu_base_person_import_job,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS and Feishu Base triggers are required.",
    ),
    pytest.mark.django_db(transaction=True),
]

MAPPING = {"display_name": "Name", "email": "Email"}


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
                "SELECT set_config('app.current_org', %s, false)", [previous]
            )


def _queue(org, actor, monkeypatch):
    connection_row = FeishuBaseConnection(
        org=org,
        app_id="cli_pg_import",
        app_token="base-pg-private",
        table_id="table-pg-private",
        field_mapping={},
        is_active=True,
    )
    connection_row.set_app_secret("pg-secret")
    connection_row.full_clean()
    connection_row.save()
    configure_organization_execution(
        org=org, actor=actor, enabled=True, daily_limit=10
    )
    configure_channel(
        org=org,
        actor=actor,
        channel=ExecutionChannel.FEISHU,
        enabled=True,
        test_mode=True,
        daily_limit=10,
        per_execution_limit=1,
    )
    intent = feishu_person_import_execution_intent(
        connection=connection_row,
        mapping=MAPPING,
        row_limit=2,
    )
    target = add_test_target(
        org=org,
        actor=actor,
        channel=ExecutionChannel.FEISHU,
        identifier=intent.test_target_identifier,
        safe_label="Concurrent import Base",
    )
    approval = issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=intent.channel,
        action=intent.action,
        target_hash=target.identifier_hash,
        payload_hash=intent.payload_hash,
        units=1,
    ).approval
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import.dispatch_job",
        lambda job: False,
    )
    result = enqueue_feishu_person_import(
        connection=connection_row,
        requested_by=actor,
        mapping=MAPPING,
        row_limit=2,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    )
    return result.person_import


class _ConcurrentImportClient:
    def __init__(self):
        self.started = Event()
        self.release = Event()
        self.lock = Lock()
        self.read_count = 0
        self.execution_request_id = None
        self.observations = []

    def for_execution(self, *, execution_request_id, **kwargs):
        self.execution_request_id = execution_request_id
        return self

    def _observe(self, stage):
        request_status = ExternalExecutionRequest.objects.get(
            id=self.execution_request_id
        ).status
        import_status = FeishuBasePersonImport.objects.get(
            execution_request_id=self.execution_request_id
        ).status
        with self.lock:
            self.observations.append((stage, request_status, import_status))
        assert request_status == ExternalRequestStatus.SENDING
        assert import_status == FeishuBasePersonImportStatus.READING

    def tenant_access_token(self, **kwargs):
        self._observe("authenticate")
        return "access-token"

    def list_fields(self, **kwargs):
        self._observe("list_fields")
        return [
            {"field_name": "Name", "type": 1},
            {"field_name": "Email", "type": 1},
        ]

    def list_records(self, **kwargs):
        self._observe("list_records")
        with self.lock:
            self.read_count += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return [
            FeishuBaseRecord(
                record_id="raw-concurrent-record",
                fields={"Name": "Concurrent Person", "Email": "person@example.com"},
            )
        ]


@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_two_workers_can_read_only_one_provider_snapshot(
    transactional_db, org_a, admin_profile, monkeypatch
):
    with database_org_context(org_a.id):
        person_import = _queue(org_a, admin_profile, monkeypatch)
        payload = dict(person_import.automation_job.payload)
    client = _ConcurrentImportClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )
    start = Barrier(2)

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                start.wait(timeout=10)
                try:
                    process_feishu_base_person_import_job(payload)
                except PermanentJobError as exc:
                    return exc.code
                return "succeeded"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        assert client.started.wait(timeout=10)
        done, _pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
        assert done
        client.release.set()
        outcomes = [future.result(timeout=10) for future in futures]

    assert client.read_count == 1
    assert [stage for stage, _request, _ledger in client.observations] == [
        "authenticate",
        "list_fields",
        "list_records",
    ]
    assert sorted(outcomes) == ["feishu_execution_not_replayable", "succeeded"]
    with database_org_context(org_a.id):
        person_import.refresh_from_db()
        assert person_import.status == "previewed"
        assert person_import.import_batch_id is not None


@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_active_read_wins_race_with_stale_reconciler_without_false_unknown(
    transactional_db, org_a, admin_profile, monkeypatch
):
    with database_org_context(org_a.id):
        person_import = _queue(org_a, admin_profile, monkeypatch)
        payload = dict(person_import.automation_job.payload)
    client = _ConcurrentImportClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                return process_feishu_base_person_import_job(payload)
        finally:
            close_old_connections()

    reconcile_started = Event()

    def reconciler():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                reconcile_started.set()
                return reconcile_stale_sending(
                    org=org_a,
                    # Include the freshly claimed SENDING request. The
                    # reconciler then waits behind the active reader's locks.
                    older_than=timezone.now() + timedelta(minutes=1),
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker_future = executor.submit(worker)
        assert client.started.wait(timeout=10), "worker never entered record read"
        with database_org_context(org_a.id):
            assert ExternalExecutionRequest.objects.get(
                id=person_import.execution_request_id
            ).status == ExternalRequestStatus.SENDING
            assert FeishuBasePersonImport.objects.get(
                id=person_import.id
            ).status == FeishuBasePersonImportStatus.READING
        reconcile_future = executor.submit(reconciler)
        assert reconcile_started.wait(timeout=10)
        done, _pending = wait([reconcile_future], timeout=1)
        assert not done, "stale reconciler bypassed the active read locks"
        client.release.set()
        worker_result = worker_future.result(timeout=10)
        reconciled = reconcile_future.result(timeout=10)

    assert worker_result["status"] == "previewed"
    assert reconciled == []
    assert client.read_count == 1
    with database_org_context(org_a.id):
        person_import.refresh_from_db()
        person_import.execution_request.refresh_from_db()
        assert person_import.status == "previewed"
        assert person_import.execution_request.status == "delivered"
        assert person_import.import_batch_id is not None


@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_hard_crash_rolls_back_post_read_transaction_before_stale_reconciliation(
    transactional_db, org_a, admin_profile, monkeypatch
):
    with database_org_context(org_a.id):
        person_import = _queue(org_a, admin_profile, monkeypatch)
        payload = dict(person_import.automation_job.payload)

    class HardCrashClient(_ConcurrentImportClient):
        def list_records(self, **kwargs):
            self._observe("list_records")
            with self.lock:
                self.read_count += 1
            raise SystemExit("simulated worker crash")

    client = HardCrashClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    with database_org_context(org_a.id):
        with pytest.raises(SystemExit):
            process_feishu_base_person_import_job(payload)
        person_import.refresh_from_db()
        person_import.execution_request.refresh_from_db()
        control = ChannelExecutionControl.objects.get(
            org=org_a,
            channel=ExecutionChannel.FEISHU,
        )
        assert person_import.status == FeishuBasePersonImportStatus.READING
        assert person_import.execution_request.status == ExternalRequestStatus.SENDING
        assert person_import.import_batch_id is None
        assert control.reserved_units == 1
        assert control.consumed_units == 0

        reconciled = reconcile_stale_sending(
            org=org_a,
            older_than=timezone.now() + timedelta(minutes=1),
        )
        person_import.refresh_from_db()
        person_import.execution_request.refresh_from_db()
        control.refresh_from_db()
        assert person_import.execution_request_id in reconciled
        assert person_import.status == FeishuBasePersonImportStatus.UNKNOWN
        assert person_import.execution_request.status == ExternalRequestStatus.UNKNOWN
        assert control.reserved_units == 0
        assert control.consumed_units == 1
        assert client.read_count == 1


@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_stale_reserved_projection_failure_rolls_back_postgres_refund(
    transactional_db, org_a, admin_profile, monkeypatch
):
    with database_org_context(org_a.id):
        person_import = _queue(org_a, admin_profile, monkeypatch)
        monkeypatch.setattr(
            "integrations.execution_safety._project_feishu_stale_failed",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("projection failed")),
        )

        with pytest.raises(RuntimeError, match="projection failed"):
            reconcile_stale_reserved(
                org=org_a,
                older_than=timezone.now() + timedelta(minutes=1),
            )

        person_import.refresh_from_db()
        person_import.execution_request.refresh_from_db()
        control = ChannelExecutionControl.objects.get(
            org=org_a,
            channel=ExecutionChannel.FEISHU,
        )
        assert person_import.status == FeishuBasePersonImportStatus.QUEUED
        assert person_import.execution_request.status == ExternalRequestStatus.RESERVED
        assert control.reserved_units == 1
        assert control.consumed_units == 0


@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_import_ledger_rls_and_cross_org_requester_trigger(
    transactional_db, org_a, admin_profile, profile_b, monkeypatch
):
    with database_org_context(org_a.id):
        person_import = _queue(org_a, admin_profile, monkeypatch)
        import_id = person_import.id

    with _empty_database_org_context():
        assert not FeishuBasePersonImport.objects.filter(id=import_id).exists()

    with database_org_context(org_a.id):
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                FeishuBasePersonImport.objects.filter(id=import_id).update(
                    requested_by_id=profile_b.id
                )
        person_import.refresh_from_db()
        assert person_import.requested_by_id == admin_profile.id
