import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.models import AutomationJob
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    reconcile_stale_reserved,
    reconcile_stale_sending,
    resolve_unknown_execution,
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
from integrations.providers.feishu_base.client import (
    FEISHU_IMPORT_PERSON_ACTION,
    FeishuBaseAPIError,
    FeishuBaseRecord,
)
from integrations.providers.feishu_base.person_import import (
    process_feishu_base_person_import_job,
)
from matching.models import Evidence, Person, PersonImportBatch

BASE = "/api/integrations/feishu-base/person-imports"
MAPPING = {
    "display_name": "Name",
    "email": "Email",
    "current_title": "Title",
    "observed_at": "Observed",
}


@pytest.fixture(autouse=True)
def _http_settings():
    with override_settings(SECURE_SSL_REDIRECT=False):
        yield


def _connection(org, *, outbound_mapping=None):
    connection = FeishuBaseConnection(
        org=org,
        app_id="cli_import",
        app_token="base-private-token",
        table_id="table-private-id",
        field_mapping=outbound_mapping or {},
        is_active=True,
    )
    connection.set_app_secret("super-secret")
    connection.full_clean()
    connection.save()
    return connection


def _enable_safety(org, actor, connection):
    configure_organization_execution(
        org=org,
        actor=actor,
        enabled=True,
        daily_limit=20,
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
    return add_test_target(
        org=org,
        actor=actor,
        channel=ExecutionChannel.FEISHU,
        identifier=f"feishu-base:{connection.id}",
        safe_label="Dedicated import Base",
    )


def _queue(admin_client, org, actor, monkeypatch):
    connection = _connection(org)
    target = _enable_safety(org, actor, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import.dispatch_job",
        lambda job: False,
    )
    first = admin_client.post(
        f"{BASE}/preview/",
        {"mapping": MAPPING, "limit": 2},
        format="json",
    )
    assert first.status_code == 200, first.json()
    intent = first.json()["intent"]
    approval = issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=ExecutionChannel.FEISHU,
        action=intent["action"],
        target_hash=target.identifier_hash,
        payload_hash=intent["payload_hash"],
        units=1,
        idempotency_key=uuid4(),
    ).approval
    second = admin_client.post(
        f"{BASE}/preview/",
        {
            "mapping": MAPPING,
            "limit": 2,
            "approval_id": str(approval.id),
            "idempotency_key": str(uuid4()),
        },
        format="json",
    )
    assert second.status_code == 202, second.json()
    return connection, second


class _SuccessfulImportClient:
    def __init__(self):
        self.execution_request_id = None
        self.list_calls = 0

    def for_execution(self, *, action, execution_request_id, **kwargs):
        assert action == FEISHU_IMPORT_PERSON_ACTION
        self.execution_request_id = execution_request_id
        return self

    def _assert_reading(self):
        request = ExternalExecutionRequest.objects.get(id=self.execution_request_id)
        person_import = FeishuBasePersonImport.objects.get(
            execution_request_id=self.execution_request_id
        )
        assert request.status == ExternalRequestStatus.SENDING
        assert person_import.status == FeishuBasePersonImportStatus.READING

    def tenant_access_token(self, **kwargs):
        self._assert_reading()
        return "access-token"

    def list_fields(self, **kwargs):
        self._assert_reading()
        return [
            {"field_name": "Name", "type": 1},
            {"field_name": "Email", "type": 1},
            {"field_name": "Title", "type": 3},
            {"field_name": "Observed", "type": 5},
        ]

    def list_records(self, **kwargs):
        self.list_calls += 1
        self._assert_reading()
        return [
            FeishuBaseRecord(
                record_id="raw-provider-record-must-not-persist",
                fields={
                    "Name": "Ada Lovelace",
                    "Email": "ada@example.com",
                    "Title": "Engineer",
                    "Observed": 1_785_000_000_000,
                },
            )
        ]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_first_stage_is_zero_io_and_inbound_only_connection_can_be_enabled(
    admin_client, org_a, monkeypatch
):
    connection = _connection(org_a)
    provider = monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    response = admin_client.post(
        f"{BASE}/preview/",
        {"mapping": MAPPING, "limit": 100},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["intent"]["action"] == FEISHU_IMPORT_PERSON_ACTION
    assert response.json()["intent"]["test_target_identifier"] == (
        f"feishu-base:{connection.id}"
    )
    assert not FeishuBasePersonImport.objects.exists()
    assert not AutomationJob.objects.exists()
    assert not ExternalExecutionRequest.objects.exists()
    assert provider is None


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_outbound_actions_remain_fail_closed_without_intake_mapping(
    admin_client, org_a
):
    _connection(org_a)
    response = admin_client.post(
        "/api/integrations/feishu-base/connection/test/", {}, format="json"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "feishu_outbound_mapping_incomplete"


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_second_stage_has_safe_projection_and_minimal_job_payload(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    body = response.json()
    serialized = json.dumps(body).lower()
    assert set(body) == {
        "id",
        "status",
        "job_id",
        "job_status",
        "status_url",
        "batch_id",
        "error_code",
        "total_count",
        "ready_count",
        "invalid_count",
        "created_at",
        "completed_at",
        "replayed",
    }
    assert "mapping" not in serialized
    assert "payload_hash" not in serialized
    assert "target_hash" not in serialized
    person_import = FeishuBasePersonImport.objects.get(id=body["id"])
    assert person_import.requested_by == admin_profile
    assert person_import.get_mapping() == MAPPING
    assert person_import.mapping_ciphertext and "Name" not in person_import.mapping_ciphertext
    assert person_import.automation_job.max_attempts == 1
    assert person_import.automation_job.payload == {
        "import_id": str(person_import.id),
        "execution_request_id": str(person_import.execution_request_id),
    }


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_success_reads_while_sending_and_creates_preview_only(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    client = _SuccessfulImportClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    result = process_feishu_base_person_import_job(person_import.automation_job.payload)

    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    batch = PersonImportBatch.objects.get(id=result["batch_id"])
    record = batch.records.get()
    assert client.list_calls == 1
    assert person_import.status == FeishuBasePersonImportStatus.PREVIEWED
    assert person_import.execution_request.status == ExternalRequestStatus.DELIVERED
    assert person_import.import_batch == batch
    assert batch.status == "previewed"
    assert batch.source == "feishu"
    assert batch.requested_by == admin_profile
    assert record.source_record_id != "raw-provider-record-must-not-persist"
    assert len(record.source_record_id) == 64
    assert not Person.objects.exists()
    assert not Evidence.objects.exists()
    persisted = json.dumps(
        {
            "batch_source": batch.source_namespace,
            "record_id": record.source_record_id,
            "job": person_import.automation_job.payload,
        }
    )
    assert "raw-provider-record-must-not-persist" not in persisted


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_empty_provider_snapshot_creates_zero_row_preview(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])

    class EmptyImportClient(_SuccessfulImportClient):
        def list_records(self, **kwargs):
            self.list_calls += 1
            self._assert_reading()
            return []

    client = EmptyImportClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    result = process_feishu_base_person_import_job(person_import.automation_job.payload)

    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    batch = PersonImportBatch.objects.get(id=result["batch_id"])
    assert client.list_calls == 1
    assert person_import.status == FeishuBasePersonImportStatus.PREVIEWED
    assert person_import.execution_request.status == ExternalRequestStatus.DELIVERED
    assert batch.status == "previewed"
    assert batch.total_count == batch.ready_count == batch.invalid_count == 0
    assert not batch.records.exists()


class _ReadFailureClient(_SuccessfulImportClient):
    def list_records(self, **kwargs):
        super().list_records(**kwargs)
        raise FeishuBaseAPIError(
            "provider secret detail",
            retryable=True,
            error_code="feishu_transport_error",
        )


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_record_read_failure_is_unknown_consumed_and_not_replayable(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    client = _ReadFailureClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_feishu_base_person_import_job(person_import.automation_job.payload)
    assert exc_info.value.code == "feishu_transport_error"
    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert person_import.status == FeishuBasePersonImportStatus.UNKNOWN
    assert person_import.execution_request.status == ExternalRequestStatus.UNKNOWN
    assert control.reserved_units == 0
    assert control.consumed_units == 1
    with pytest.raises(PermanentJobError) as replay:
        process_feishu_base_person_import_job(person_import.automation_job.payload)
    assert replay.value.code == "feishu_execution_not_replayable"
    assert client.list_calls == 1


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_local_preview_failure_is_unknown_and_unknown_resolution_never_replays(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    client = _SuccessfulImportClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    def fail_preview(**kwargs):
        assert ExternalExecutionRequest.objects.get(
            id=person_import.execution_request_id
        ).status == ExternalRequestStatus.SENDING
        assert FeishuBasePersonImport.objects.get(
            id=person_import.id
        ).status == FeishuBasePersonImportStatus.READING
        raise RuntimeError("local persistence")

    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import.preview_provider_person_import",
        fail_preview,
    )
    with pytest.raises(PermanentJobError):
        process_feishu_base_person_import_job(person_import.automation_job.payload)
    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    assert person_import.status == FeishuBasePersonImportStatus.UNKNOWN
    assert person_import.execution_request.status == ExternalRequestStatus.UNKNOWN
    assert client.list_calls == 1

    resolve_unknown_execution(
        org=org_a,
        actor=admin_profile,
        request_id=person_import.execution_request_id,
        outcome="failed_consumed",
    )
    person_import.refresh_from_db()
    assert person_import.status == FeishuBasePersonImportStatus.FAILED
    assert person_import.error_code == "confirmed_read_failed_consumed"
    assert client.list_calls == 1
    assert not PersonImportBatch.objects.exists()


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_hard_crash_rolls_back_to_sending_until_stale_reconciliation(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    client = _SuccessfulImportClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    def crash_preview(**kwargs):
        assert ExternalExecutionRequest.objects.get(
            id=person_import.execution_request_id
        ).status == ExternalRequestStatus.SENDING
        raise SystemExit("simulated worker crash")

    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import.preview_provider_person_import",
        crash_preview,
    )

    with pytest.raises(SystemExit):
        process_feishu_base_person_import_job(person_import.automation_job.payload)

    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert client.list_calls == 1
    assert person_import.status == FeishuBasePersonImportStatus.READING
    assert person_import.execution_request.status == ExternalRequestStatus.SENDING
    assert control.reserved_units == 1
    assert control.consumed_units == 0
    assert not PersonImportBatch.objects.exists()

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
    assert client.list_calls == 1


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_final_channel_revocation_refunds_before_record_read(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])

    class RevokingClient(_SuccessfulImportClient):
        def list_fields(self, **kwargs):
            fields = super().list_fields(**kwargs)
            ChannelExecutionControl.objects.filter(
                org=org_a, channel=ExecutionChannel.FEISHU
            ).update(enabled=False)
            return fields

    client = RevokingClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )
    with pytest.raises(PermanentJobError) as exc_info:
        process_feishu_base_person_import_job(person_import.automation_job.payload)
    assert exc_info.value.code == "channel_disabled"
    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    assert client.list_calls == 0
    assert person_import.status == FeishuBasePersonImportStatus.FAILED
    assert person_import.execution_request.status == ExternalRequestStatus.FAILED


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_list_fields_failure_refunds_and_fails_both_ledgers_atomically(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])

    class FieldsFailureClient(_SuccessfulImportClient):
        def list_fields(self, **kwargs):
            self._assert_reading()
            raise FeishuBaseAPIError(
                "provider schema detail",
                retryable=True,
                error_code="feishu_fields_failed",
            )

    client = FieldsFailureClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_feishu_base_person_import_job(person_import.automation_job.payload)

    assert exc_info.value.code == "feishu_fields_failed"
    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert person_import.status == FeishuBasePersonImportStatus.FAILED
    assert person_import.execution_request.status == ExternalRequestStatus.FAILED
    assert person_import.error_code == "feishu_fields_failed"
    assert control.reserved_units == 0
    assert control.consumed_units == 0
    assert client.list_calls == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_pre_read_projection_failure_rolls_back_refund_and_request_failure(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    original_save = FeishuBasePersonImport.save

    class ProjectionFailureClient(_SuccessfulImportClient):
        def list_fields(self, **kwargs):
            self._assert_reading()

            def fail_failed_projection(instance, *args, **kwargs):
                if instance.status == FeishuBasePersonImportStatus.FAILED:
                    raise RuntimeError("ledger projection failed")
                return original_save(instance, *args, **kwargs)

            monkeypatch.setattr(
                FeishuBasePersonImport,
                "save",
                fail_failed_projection,
            )
            raise FeishuBaseAPIError(
                "provider schema detail",
                retryable=True,
                error_code="feishu_fields_failed",
            )

    client = ProjectionFailureClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.person_import._client", lambda: client
    )

    with pytest.raises(RuntimeError, match="ledger projection failed"):
        process_feishu_base_person_import_job(person_import.automation_job.payload)

    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert person_import.status == FeishuBasePersonImportStatus.READING
    assert person_import.execution_request.status == ExternalRequestStatus.SENDING
    assert control.reserved_units == 1
    assert control.consumed_units == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_stale_reservation_refund_projects_queued_import_failed(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])

    reconciled = reconcile_stale_reserved(
        org=org_a,
        older_than=timezone.now() + timedelta(minutes=1),
    )

    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert person_import.execution_request_id in reconciled
    assert person_import.status == FeishuBasePersonImportStatus.FAILED
    assert person_import.execution_request.status == ExternalRequestStatus.FAILED
    assert person_import.error_code == "stale_reservation_released"
    assert control.reserved_units == 0
    assert control.consumed_units == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_stale_reservation_state_mismatch_rolls_back_refund(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    person_import.status = FeishuBasePersonImportStatus.READING
    person_import.save(update_fields=["status", "updated_at"])

    reconciled = reconcile_stale_reserved(
        org=org_a,
        older_than=timezone.now() + timedelta(minutes=1),
    )

    person_import.refresh_from_db()
    person_import.execution_request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert reconciled == []
    assert person_import.status == FeishuBasePersonImportStatus.READING
    assert person_import.execution_request.status == ExternalRequestStatus.RESERVED
    assert control.reserved_units == 1
    assert control.consumed_units == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_stale_reservation_projection_failure_rolls_back_refund(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
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
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert person_import.status == FeishuBasePersonImportStatus.QUEUED
    assert person_import.execution_request.status == ExternalRequestStatus.RESERVED
    assert control.reserved_units == 1
    assert control.consumed_units == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_stale_sending_projects_import_unknown_without_provider_replay(
    admin_client, org_a, admin_profile, monkeypatch
):
    _connection_row, response = _queue(
        admin_client, org_a, admin_profile, monkeypatch
    )
    person_import = FeishuBasePersonImport.objects.get(id=response.json()["id"])
    request = person_import.execution_request
    request.status = ExternalRequestStatus.SENDING
    request.sending_at = person_import.created_at
    request.save(update_fields=["status", "sending_at", "updated_at"])
    person_import.status = FeishuBasePersonImportStatus.READING
    person_import.save(update_fields=["status", "updated_at"])

    reconciled = reconcile_stale_sending(
        org=org_a,
        older_than=person_import.created_at,
    )

    person_import.refresh_from_db()
    request.refresh_from_db()
    assert request.id in reconciled
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert person_import.status == FeishuBasePersonImportStatus.UNKNOWN
    assert not PersonImportBatch.objects.exists()
