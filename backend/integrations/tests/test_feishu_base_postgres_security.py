from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
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
    disable_test_target,
    issue_execution_approval,
)
from integrations.models import (
    ChannelExecutionApproval,
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    FeishuBaseConnection,
    FeishuBaseSync,
    FeishuBaseSyncStatus,
    OrganizationExecutionControl,
)
from integrations.providers.feishu_base.client import FeishuBaseRecord
from integrations.providers.feishu_base.sync import (
    enqueue_feishu_base_sync,
    feishu_research_sync_execution_intent,
    process_feishu_base_sync_job,
)
from sdr.compliance import anonymize_intake, ensure_intake_provenance
from sdr.models import LeadIntake, SDRProvenanceStatus

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS and Feishu Base triggers are required.",
    ),
    pytest.mark.django_db(transaction=True),
]

CHECK_VIOLATION_SQLSTATE = "23514"


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


def _digest(value):
    return sha256(value.encode()).hexdigest()


def _request(*, org, profile, channel, action, suffix):
    approval = ChannelExecutionApproval.objects.create(
        org=org,
        channel=channel,
        idempotency_key=uuid4(),
        request_hash=_digest(f"approval:{suffix}"),
        action=action,
        target_hash=_digest(f"target:{suffix}"),
        payload_hash=_digest(f"payload:{suffix}"),
        approved_by=profile,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return ExternalExecutionRequest.objects.create(
        org=org,
        channel=channel,
        action=action,
        idempotency_key=uuid4(),
        request_hash=_digest(f"request:{suffix}"),
        target_hash=approval.target_hash,
        payload_hash=approval.payload_hash,
        approval=approval,
        reserved_at=timezone.now(),
    )


def _graph(*, org, profile, suffix):
    connection_row = FeishuBaseConnection.objects.create(org=org)
    intake = LeadIntake.objects.create(
        org=org,
        source="website_form",
        source_record_id=f"feishu-pg:{suffix}",
        raw_payload={},
        normalized_payload={},
    )
    execution_request = _request(
        org=org,
        profile=profile,
        channel=ExecutionChannel.FEISHU,
        action="sync_research_result",
        suffix=suffix,
    )
    sync = FeishuBaseSync.objects.create(
        org=org,
        connection=connection_row,
        intake=intake,
        execution_request=execution_request,
        destination_sha256=_digest(f"destination:{suffix}"),
        payload_sha256=_digest(f"sync-payload:{suffix}"),
    )
    return {
        "connection": connection_row,
        "intake": intake,
        "request": execution_request,
        "sync": sync,
    }


def _raw_update(instance, column, value):
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {quote(instance._meta.db_table)} SET {quote(column)} = %s "
            f"WHERE {quote('id')} = %s",
            [value, instance.id],
        )


def _sqlstate(error):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        diagnostic = getattr(current, "diag", None)
        state = getattr(diagnostic, "sqlstate", None)
        if state:
            return state
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return None


def _assert_rejected(instance, column, value, message):
    with pytest.raises(DatabaseError) as exc_info:
        with transaction.atomic():
            _raw_update(instance, column, value)
    assert message in str(exc_info.value)
    assert _sqlstate(exc_info.value) == CHECK_VIOLATION_SQLSTATE


def test_feishu_base_sync_isolated_by_rls(org_a, org_b, admin_profile, profile_b):
    with database_org_context(org_a.id):
        own = _graph(org=org_a, profile=admin_profile, suffix="rls-a")
    with database_org_context(org_b.id):
        other = _graph(org=org_b, profile=profile_b, suffix="rls-b")

    with database_org_context(org_a.id):
        assert list(FeishuBaseSync.objects.values_list("id", flat=True)) == [
            own["sync"].id
        ]
        assert not FeishuBaseSync.objects.filter(id=other["sync"].id).exists()
    with database_org_context(org_b.id):
        assert list(FeishuBaseSync.objects.values_list("id", flat=True)) == [
            other["sync"].id
        ]
    with _empty_database_org_context():
        assert FeishuBaseSync.objects.count() == 0


def test_feishu_base_sync_trigger_rejects_cross_org_references(
    org_a, org_b, admin_profile, profile_b
):
    with database_org_context(org_a.id):
        own = _graph(org=org_a, profile=admin_profile, suffix="guard-a")
    with database_org_context(org_b.id):
        other = _graph(org=org_b, profile=profile_b, suffix="guard-b")

    cases = (
        ("connection_id", other["connection"].id, "connection organization mismatch"),
        ("intake_id", other["intake"].id, "intake organization mismatch"),
        ("execution_request_id", other["request"].id, "execution request mismatch"),
        ("org_id", org_b.id, "connection organization mismatch"),
    )
    with database_org_context(org_a.id):
        for column, value, message in cases:
            _assert_rejected(own["sync"], column, value, message)


def test_feishu_base_sync_trigger_binds_channel_and_action(org_a, admin_profile):
    with database_org_context(org_a.id):
        graph = _graph(org=org_a, profile=admin_profile, suffix="binding")
        wrong_channel = _request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.EMAIL,
            action="sync_research_result",
            suffix="wrong-channel",
        )
        wrong_action = _request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.FEISHU,
            action="validate_base_schema",
            suffix="wrong-action",
        )
        delete_request = _request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.FEISHU,
            action="delete_research_record",
            suffix="delete-action",
        )

        _assert_rejected(
            graph["sync"],
            "execution_request_id",
            wrong_channel.id,
            "execution request mismatch",
        )
        _assert_rejected(
            graph["sync"],
            "execution_request_id",
            wrong_action.id,
            "execution request mismatch",
        )
        _raw_update(graph["sync"], "execution_request_id", delete_request.id)


class _ConcurrentFeishuClient:
    def __init__(self):
        self.mutation_count = 0
        self.lock = Lock()
        self.started = Event()
        self.release = Event()

    def for_execution(self, **kwargs):
        del kwargs
        return self

    def tenant_access_token(self, **kwargs):
        del kwargs
        return "tenant-token"

    def list_fields(self, **kwargs):
        del kwargs
        return [{"field_name": "Intake ID", "type": 1}]

    def find_record_by_field(self, **kwargs):
        del kwargs
        return None

    def create_record(self, **kwargs):
        del kwargs
        with self.lock:
            self.mutation_count += 1
        self.started.set()
        assert self.release.wait(timeout=10), "concurrent Feishu test was not released"
        return FeishuBaseRecord(record_id="rec-concurrent-private", fields={})


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_two_postgres_workers_claim_one_feishu_request_for_one_mutation(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        connection_row = FeishuBaseConnection(
            org=org_a,
            app_id="cli_concurrent",
            app_token="base-concurrent",
            table_id="table-concurrent",
            field_mapping={"intake_id": "Intake ID"},
            is_active=True,
        )
        connection_row.set_app_secret("concurrent-secret")
        connection_row.save()
        intake = LeadIntake.objects.create(
            org=org_a,
            source="website_form",
            source_record_id="feishu-concurrent",
            raw_payload={},
            normalized_payload={},
            status="completed",
        )
        intent = feishu_research_sync_execution_intent(
            intake=intake,
            connection=connection_row,
        )
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=10,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        target = add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            identifier=intent.test_target_identifier,
            safe_label="Concurrent Feishu target",
        )
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=intent.channel,
            action=intent.action,
            target_hash=target.identifier_hash,
            payload_hash=intent.payload_hash,
            units=1,
        ).approval
        monkeypatch.setattr(
            "integrations.providers.feishu_base.sync.dispatch_job",
            lambda job: False,
        )
        job = enqueue_feishu_base_sync(
            intake=intake,
            approval_id=approval.id,
            idempotency_key=uuid4(),
        )

    client = _ConcurrentFeishuClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client",
        lambda: client,
    )
    start = Barrier(2)

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                start.wait(timeout=10)
                try:
                    process_feishu_base_sync_job(job.payload)
                except PermanentJobError as exc:
                    return exc.code
                return "succeeded"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        assert client.started.wait(timeout=10), "no worker reached Feishu mutation"
        done, _pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
        assert done, "the competing worker did not observe the claimed request"
        client.release.set()
        results = [future.result(timeout=10) for future in futures]

    assert client.mutation_count == 1
    assert sorted(results) == ["feishu_execution_not_replayable", "succeeded"]
    with database_org_context(org_a.id):
        request = ExternalExecutionRequest.objects.get(
            id=job.payload["execution_request_id"]
        )
        sync = FeishuBaseSync.objects.get(intake=intake)
        assert request.status == "delivered"
        assert sync.status == FeishuBaseSyncStatus.SUCCEEDED
        assert sync.get_record_id() == "rec-concurrent-private"


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_disabling_test_target_waits_for_in_flight_feishu_mutation(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        connection_row = FeishuBaseConnection(
            org=org_a,
            app_id="cli_target_lock",
            app_token="base-target-lock",
            table_id="table-target-lock",
            field_mapping={"intake_id": "Intake ID"},
            is_active=True,
        )
        connection_row.set_app_secret("target-lock-secret")
        connection_row.save()
        intake = LeadIntake.objects.create(
            org=org_a,
            source="website_form",
            source_record_id="feishu-target-lock",
            raw_payload={},
            normalized_payload={},
            status="completed",
        )
        intent = feishu_research_sync_execution_intent(
            intake=intake,
            connection=connection_row,
        )
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=10,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        target = add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            identifier=intent.test_target_identifier,
            safe_label="Target lock test",
        )
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=intent.channel,
            action=intent.action,
            target_hash=target.identifier_hash,
            payload_hash=intent.payload_hash,
            units=1,
        ).approval
        monkeypatch.setattr(
            "integrations.providers.feishu_base.sync.dispatch_job",
            lambda job: False,
        )
        job = enqueue_feishu_base_sync(
            intake=intake,
            approval_id=approval.id,
            idempotency_key=uuid4(),
        )

    client = _ConcurrentFeishuClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client",
        lambda: client,
    )

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                return process_feishu_base_sync_job(job.payload)
        finally:
            close_old_connections()

    disable_started = Event()

    def disable_target():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                disable_started.set()
                disable_test_target(
                    org=org_a,
                    actor=admin_profile,
                    target_id=target.id,
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker_future = executor.submit(worker)
        assert client.started.wait(timeout=10), "worker never entered Feishu mutation"
        disable_future = executor.submit(disable_target)
        assert disable_started.wait(timeout=10)
        done, _pending = wait([disable_future], timeout=1)
        assert not done, "test target disable bypassed the mutation row lock"
        client.release.set()
        result = worker_future.result(timeout=10)
        disable_future.result(timeout=10)

    assert result["status"] == "succeeded"
    assert client.mutation_count == 1
    with database_org_context(org_a.id):
        target.refresh_from_db()
        request = ExternalExecutionRequest.objects.get(
            id=job.payload["execution_request_id"]
        )
        assert target.is_active is False
        assert request.status == "delivered"


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_retention_due_before_worker_refunds_without_feishu_mutation(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        connection_row = FeishuBaseConnection(
            org=org_a,
            app_id="cli_retention",
            app_token="base-retention",
            table_id="table-retention",
            field_mapping={"intake_id": "Intake ID"},
            is_active=True,
        )
        connection_row.set_app_secret("retention-secret")
        connection_row.save()
        intake = LeadIntake.objects.create(
            org=org_a,
            source="website_form",
            source_record_id="feishu-retention",
            raw_payload={},
            normalized_payload={},
            status="completed",
        )
        provenance = ensure_intake_provenance(intake=intake)
        intent = feishu_research_sync_execution_intent(
            intake=intake,
            connection=connection_row,
        )
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=10,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        target = add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            identifier=intent.test_target_identifier,
            safe_label="Retention test target",
        )
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=intent.channel,
            action=intent.action,
            target_hash=target.identifier_hash,
            payload_hash=intent.payload_hash,
            units=1,
        ).approval
        monkeypatch.setattr(
            "integrations.providers.feishu_base.sync.dispatch_job",
            lambda job: False,
        )
        job = enqueue_feishu_base_sync(
            intake=intake,
            approval_id=approval.id,
            idempotency_key=uuid4(),
        )
        provenance.status = SDRProvenanceStatus.RETENTION_DUE
        provenance.save(update_fields=["status", "updated_at"])

        client = _ConcurrentFeishuClient()
        monkeypatch.setattr(
            "integrations.providers.feishu_base.sync._client",
            lambda: client,
        )
        with pytest.raises(PermanentJobError) as exc_info:
            process_feishu_base_sync_job(job.payload)

        assert exc_info.value.code == "data_retention_due"
        request = ExternalExecutionRequest.objects.get(
            id=job.payload["execution_request_id"]
        )
        sync = FeishuBaseSync.objects.get(intake=intake)
        channel = ChannelExecutionControl.objects.get(
            org=org_a,
            channel=ExecutionChannel.FEISHU,
        )
        org_control = OrganizationExecutionControl.objects.get(org=org_a)
        assert client.mutation_count == 0
        assert request.status == "failed"
        assert sync.status == FeishuBaseSyncStatus.FAILED
        assert channel.reserved_units == channel.consumed_units == 0
        assert org_control.reserved_units == org_control.consumed_units == 0


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_anonymize_waits_for_mutation_then_marks_remote_erasure_pending(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        connection_row = FeishuBaseConnection(
            org=org_a,
            app_id="cli_anonymize_lock",
            app_token="base-anonymize-lock",
            table_id="table-anonymize-lock",
            field_mapping={"intake_id": "Intake ID"},
            is_active=True,
        )
        connection_row.set_app_secret("anonymize-lock-secret")
        connection_row.save()
        intake = LeadIntake.objects.create(
            org=org_a,
            source="website_form",
            source_record_id="feishu-anonymize-lock",
            raw_payload={},
            normalized_payload={},
            status="completed",
        )
        ensure_intake_provenance(intake=intake)
        intent = feishu_research_sync_execution_intent(
            intake=intake,
            connection=connection_row,
        )
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=10,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        target = add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.FEISHU,
            identifier=intent.test_target_identifier,
            safe_label="Anonymize lock target",
        )
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=intent.channel,
            action=intent.action,
            target_hash=target.identifier_hash,
            payload_hash=intent.payload_hash,
            units=1,
        ).approval
        monkeypatch.setattr(
            "integrations.providers.feishu_base.sync.dispatch_job",
            lambda job: False,
        )
        job = enqueue_feishu_base_sync(
            intake=intake,
            approval_id=approval.id,
            idempotency_key=uuid4(),
        )

    client = _ConcurrentFeishuClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client",
        lambda: client,
    )

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                return process_feishu_base_sync_job(job.payload)
        finally:
            close_old_connections()

    anonymize_started = Event()

    def anonymizer():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                anonymize_started.set()
                anonymize_intake(intake)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker_future = executor.submit(worker)
        assert client.started.wait(timeout=10), "worker never entered Feishu mutation"
        anonymize_future = executor.submit(anonymizer)
        assert anonymize_started.wait(timeout=10)
        done, _pending = wait([anonymize_future], timeout=1)
        assert not done, "anonymization bypassed the intake mutation lock"
        client.release.set()
        result = worker_future.result(timeout=10)
        anonymize_future.result(timeout=10)

    assert result["status"] == "succeeded"
    assert client.mutation_count == 1
    with database_org_context(org_a.id):
        sync = FeishuBaseSync.objects.get(intake=intake)
        assert sync.status == FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING
        assert sync.has_remote_record is True
