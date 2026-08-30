from datetime import timedelta
from uuid import uuid4

import pytest
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.models import AutomationJob
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_sending,
    reconcile_stale_sending,
    resolve_unknown_execution,
)
from integrations.models import (
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    FeishuBaseConnection,
    FeishuBaseSync,
    FeishuBaseSyncStatus,
    OrganizationExecutionControl,
)
from integrations.providers.feishu_base.client import FeishuBaseAPIError, FeishuBaseRecord
from integrations.providers.feishu_base.sync import process_feishu_base_sync_job
from integrations.providers.sdr_adapters import FeishuBaseDataGovernanceAdapter
from sdr.models import LeadIntake

BASE = "/api/integrations/feishu-base"
FIELD_MAPPING = {"intake_id": "Intake ID", "company_name": "Company"}


@pytest.fixture(autouse=True)
def _http_settings():
    with override_settings(SECURE_SSL_REDIRECT=False):
        yield


def _connection(org):
    connection = FeishuBaseConnection(
        org=org,
        app_id="cli_test",
        app_token="base-private-token",
        table_id="table-private-id",
        field_mapping=FIELD_MAPPING,
        is_active=True,
    )
    connection.set_app_secret("super-secret")
    connection.full_clean()
    connection.save()
    return connection


def _intake(org, suffix="one"):
    return LeadIntake.objects.create(
        org=org,
        source="website_form",
        source_record_id=f"feishu-flow:{suffix}",
        raw_payload={"company_name": "Analytical Engines"},
        normalized_payload={"company": {"name": "Analytical Engines"}},
        status="completed",
    )


def _enable_safety(org, actor, connection):
    configure_organization_execution(
        org=org, actor=actor, enabled=True, daily_limit=20
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
        safe_label="Dedicated Feishu Base",
    )


def _approval(org, actor, target, intent):
    return issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=ExecutionChannel.FEISHU,
        action=intent["action"],
        target_hash=target.identifier_hash,
        payload_hash=intent["payload_hash"],
        units=1,
        idempotency_key=uuid4(),
    ).approval


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_first_stage_returns_exact_intents_without_network_jobs_or_quota(
    admin_client, org_a, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a)
    provider = monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    schema = admin_client.post(f"{BASE}/connection/test/", {}, format="json")
    research = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    )

    assert schema.status_code == research.status_code == 200
    assert schema.json()["intent"]["action"] == "validate_base_schema"
    assert research.json()["intent"]["action"] == "sync_research_result"
    assert schema.json()["intent"]["test_target_identifier"] == (
        f"feishu-base:{connection.id}"
    )
    assert AutomationJob.objects.filter(org=org_a).count() == 0
    assert ExternalExecutionRequest.objects.filter(org=org_a).count() == 0
    assert FeishuBaseSync.objects.filter(org=org_a).count() == 0
    assert not ChannelExecutionControl.objects.filter(org=org_a).exists()
    assert not OrganizationExecutionControl.objects.filter(org=org_a).exists()
    assert provider is None


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_second_stage_reserves_and_creates_one_attempt_job_atomically(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a)
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    first = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    )
    approval = _approval(org_a, admin_profile, target, first.json()["intent"])
    execution_key = uuid4()

    queued = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {"approval_id": str(approval.id), "idempotency_key": str(execution_key)},
        format="json",
    )

    assert queued.status_code == 202, queued.json()
    job = AutomationJob.objects.get(id=queued.json()["job_id"], org=org_a)
    request = ExternalExecutionRequest.objects.get(
        id=queued.json()["execution_request_id"], org=org_a
    )
    sync = FeishuBaseSync.objects.get(org=org_a, intake=intake)
    assert job.max_attempts == 1
    assert job.payload["execution_request_id"] == str(request.id)
    assert request.status == ExternalRequestStatus.RESERVED
    assert sync.execution_request_id == request.id
    assert sync.status == FeishuBaseSyncStatus.QUEUED
    assert ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    ).reserved_units == 1


class _TransportFailureClient:
    def for_execution(self, **kwargs):
        return self

    def tenant_access_token(self, **kwargs):
        raise FeishuBaseAPIError(
            "secret provider response",
            retryable=True,
            error_code="feishu_transport_error",
        )


class _DefiniteFailureClient(_TransportFailureClient):
    def tenant_access_token(self, **kwargs):
        raise FeishuBaseAPIError(
            "secret provider response",
            retryable=False,
            error_code="feishu_provider_403",
            status_code=403,
        )


class _RedirectFailureClient(_TransportFailureClient):
    def tenant_access_token(self, **kwargs):
        raise FeishuBaseAPIError(
            "redirect body must remain private",
            retryable=False,
            error_code="feishu_http_redirect",
            status_code=307,
        )


class _SuccessfulClient:
    def __init__(self):
        self.action = ""
        self.created = 0
        self.deleted = 0

    def for_execution(self, *, action, **kwargs):
        self.action = action
        return self

    def tenant_access_token(self, **kwargs):
        return "tenant-token"

    def list_fields(self, **kwargs):
        return [
            {"field_name": "Intake ID", "type": 1},
            {"field_name": "Company", "type": 1},
        ]

    def find_record_by_field(self, **kwargs):
        return None

    def create_record(self, **kwargs):
        self.created += 1
        return FeishuBaseRecord(
            record_id="rec-private-provider-identifier",
            fields=kwargs["fields"],
        )

    def delete_record(self, **kwargs):
        assert kwargs["record_id"] == "rec-private-provider-identifier"
        self.deleted += 1


class _MutationRedirectClient(_SuccessfulClient):
    def create_record(self, **kwargs):
        del kwargs
        raise FeishuBaseAPIError(
            "redirect body must remain private",
            retryable=False,
            error_code="feishu_http_redirect",
            status_code=308,
            mutation_attempted=True,
        )


class _MutationTransportClient(_SuccessfulClient):
    def create_record(self, **kwargs):
        del kwargs
        raise FeishuBaseAPIError(
            "transport body must remain private",
            retryable=True,
            error_code="feishu_transport_error",
            mutation_attempted=True,
        )


class _MutationRejectedClient(_SuccessfulClient):
    def create_record(self, **kwargs):
        del kwargs
        raise FeishuBaseAPIError(
            "rejection body must remain private",
            retryable=False,
            error_code="feishu_http_400",
            status_code=400,
            mutation_attempted=True,
        )


class _MutationMalformedSuccessClient(_SuccessfulClient):
    def create_record(self, **kwargs):
        del kwargs
        raise FeishuBaseAPIError(
            "provider response had no durable record id",
            retryable=False,
            error_code="feishu_record_id_missing",
            status_code=200,
            mutation_attempted=True,
        )


class _SnapshotChangingClient(_SuccessfulClient):
    def __init__(self, connection_id):
        super().__init__()
        self.connection_id = connection_id

    def find_record_by_field(self, **kwargs):
        del kwargs
        FeishuBaseConnection.objects.filter(id=self.connection_id).update(
            field_mapping={"intake_id": "Changed Intake ID"}
        )
        return None


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
@pytest.mark.parametrize(
    ("client_type", "request_status", "sync_status", "consumed"),
    [
        (
            _TransportFailureClient,
            ExternalRequestStatus.FAILED,
            FeishuBaseSyncStatus.FAILED,
            0,
        ),
        (
            _DefiniteFailureClient,
            ExternalRequestStatus.FAILED,
            FeishuBaseSyncStatus.FAILED,
            0,
        ),
        (
            _RedirectFailureClient,
            ExternalRequestStatus.FAILED,
            FeishuBaseSyncStatus.FAILED,
            0,
        ),
        (
            _MutationRedirectClient,
            ExternalRequestStatus.UNKNOWN,
            FeishuBaseSyncStatus.UNKNOWN,
            1,
        ),
        (
            _MutationTransportClient,
            ExternalRequestStatus.UNKNOWN,
            FeishuBaseSyncStatus.UNKNOWN,
            1,
        ),
        (
            _MutationRejectedClient,
            ExternalRequestStatus.FAILED,
            FeishuBaseSyncStatus.FAILED,
            0,
        ),
        (
            _MutationMalformedSuccessClient,
            ExternalRequestStatus.UNKNOWN,
            FeishuBaseSyncStatus.UNKNOWN,
            1,
        ),
    ],
)
def test_provider_outcome_is_unknown_or_definitely_refunded_without_retry(
    admin_client,
    org_a,
    admin_profile,
    monkeypatch,
    client_type,
    request_status,
    sync_status,
    consumed,
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix=request_status)
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    intent = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    ).json()["intent"]
    approval = _approval(org_a, admin_profile, target, intent)
    queued = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {"approval_id": str(approval.id), "idempotency_key": str(uuid4())},
        format="json",
    )
    job = AutomationJob.objects.get(id=queued.json()["job_id"])
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client", client_type
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_feishu_base_sync_job(job.payload)

    assert "secret provider response" not in str(exc_info.value)
    request = ExternalExecutionRequest.objects.get(
        id=job.payload["execution_request_id"]
    )
    sync = FeishuBaseSync.objects.get(intake=intake)
    control = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert request.status == request_status
    assert sync.status == sync_status
    assert job.max_attempts == 1
    assert control.reserved_units == 0
    assert control.consumed_units == consumed


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_successful_sync_and_delete_each_use_a_new_one_attempt_execution(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="success")
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    client = _SuccessfulClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client", lambda: client
    )

    sync_intent = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    ).json()["intent"]
    sync_approval = _approval(org_a, admin_profile, target, sync_intent)
    sync_queued = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {
            "approval_id": str(sync_approval.id),
            "idempotency_key": str(uuid4()),
        },
        format="json",
    )
    sync_job = AutomationJob.objects.get(id=sync_queued.json()["job_id"])
    sync_result = process_feishu_base_sync_job(sync_job.payload)
    sync = FeishuBaseSync.objects.get(intake=intake)
    sync_request = ExternalExecutionRequest.objects.get(
        id=sync_job.payload["execution_request_id"]
    )

    assert sync_result["status"] == "succeeded"
    assert sync.status == FeishuBaseSyncStatus.SUCCEEDED
    assert sync.get_record_id() == "rec-private-provider-identifier"
    assert "rec-private-provider-identifier" not in sync.record_id_ciphertext
    assert "rec-private-provider-identifier" not in str(sync_job.payload)
    assert sync_request.status == ExternalRequestStatus.DELIVERED
    assert sync_job.max_attempts == 1
    assert client.created == 1

    delete_intent = admin_client.post(
        f"{BASE}/syncs/{sync.id}/delete/", {}, format="json"
    ).json()["intent"]
    delete_approval = _approval(org_a, admin_profile, target, delete_intent)
    delete_queued = admin_client.post(
        f"{BASE}/syncs/{sync.id}/delete/",
        {
            "approval_id": str(delete_approval.id),
            "idempotency_key": str(uuid4()),
        },
        format="json",
    )
    delete_job = AutomationJob.objects.get(id=delete_queued.json()["job_id"])
    delete_result = process_feishu_base_sync_job(delete_job.payload)
    sync.refresh_from_db()
    delete_request = ExternalExecutionRequest.objects.get(
        id=delete_job.payload["execution_request_id"]
    )

    assert delete_result["status"] == FeishuBaseSyncStatus.EXTERNAL_ERASURE_COMPLETED
    assert sync.status == FeishuBaseSyncStatus.EXTERNAL_ERASURE_COMPLETED
    assert sync.has_remote_record is False
    assert delete_request.status == ExternalRequestStatus.DELIVERED
    assert delete_job.max_attempts == 1
    assert "rec-private-provider-identifier" not in str(delete_job.payload)
    assert "rec-private-provider-identifier" not in str(delete_result)
    assert client.deleted == 1


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_safe_sync_list_and_delete_intent_never_expose_remote_id(admin_client, org_a):
    connection = _connection(org_a)
    intake = _intake(org_a)
    sync = FeishuBaseSync(
        org=org_a,
        connection=connection,
        intake=intake,
        status=FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING,
        destination_sha256="d" * 64,
        payload_sha256="p" * 64,
    )
    remote_id = "rec-private-provider-identifier"
    sync.set_record_id(remote_id)
    sync.save()

    listing = admin_client.get(f"{BASE}/syncs/")
    intent = admin_client.post(
        f"{BASE}/syncs/{sync.id}/delete/", {}, format="json"
    )
    bodies = f"{listing.json()} {intent.json()}"

    assert listing.status_code == intent.status_code == 200
    assert intent.json()["intent"]["action"] == "delete_research_record"
    assert listing.json()["results"][0]["can_delete"] is True
    assert remote_id not in bodies
    assert sync.record_id_hash not in bodies
    assert "record_id" not in bodies
    assert AutomationJob.objects.filter(org=org_a).count() == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_connection_destination_can_change_only_before_remote_or_unknown_state(
    admin_client, org_a
):
    connection = _connection(org_a)
    changed = admin_client.put(
        f"{BASE}/connection/",
        {"app_token": "replacement-before-sync"},
        format="json",
    )
    assert changed.status_code == 200
    connection.refresh_from_db()
    assert connection.app_token == "replacement-before-sync"
    assert "replacement-before-sync" not in str(changed.json())

    intake = _intake(org_a, suffix="destination-lock")
    sync = FeishuBaseSync(
        org=org_a,
        connection=connection,
        intake=intake,
        status=FeishuBaseSyncStatus.SUCCEEDED,
        destination_sha256="d" * 64,
        payload_sha256="p" * 64,
    )
    sync.set_record_id("rec-locked-private")
    sync.save()
    rotated = admin_client.put(
        f"{BASE}/connection/",
        {"app_secret": "rotated-secret-with-remote-record"},
        format="json",
    )
    assert rotated.status_code == 200
    connection.refresh_from_db()
    assert connection.get_app_secret() == "rotated-secret-with-remote-record"
    assert connection.app_secret_hint == ""
    assert "rotated-secret-with-remote-record" not in str(rotated.json())
    assert "app_secret_hint" not in rotated.json()
    assert "app_token_hint" not in rotated.json()
    assert "table_id_hint" not in rotated.json()

    blocked = admin_client.put(
        f"{BASE}/connection/",
        {"table_id": "forbidden-new-table"},
        format="json",
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "feishu_destination_locked"
    connection.refresh_from_db()
    assert connection.table_id == "table-private-id"
    assert "forbidden-new-table" not in str(blocked.json())

    sync.clear_record_id()
    sync.status = FeishuBaseSyncStatus.UNKNOWN
    sync.save(
        update_fields=[
            "record_id_ciphertext",
            "record_id_hash",
            "record_safe_label",
            "status",
            "updated_at",
        ]
    )
    blocked_unknown = admin_client.put(
        f"{BASE}/connection/",
        {"app_id": "forbidden-while-unknown"},
        format="json",
    )
    assert blocked_unknown.status_code == 409
    connection.refresh_from_db()
    assert connection.app_id == "cli_test"


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_second_approval_cannot_overwrite_in_flight_sync_binding(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="binding-conflict")
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    intent = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    ).json()["intent"]
    first_approval = _approval(org_a, admin_profile, target, intent)
    first = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {
            "approval_id": str(first_approval.id),
            "idempotency_key": str(uuid4()),
        },
        format="json",
    )
    sync = FeishuBaseSync.objects.get(intake=intake)
    first_request_id = sync.execution_request_id

    second_approval = _approval(org_a, admin_profile, target, intent)
    blocked = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {
            "approval_id": str(second_approval.id),
            "idempotency_key": str(uuid4()),
        },
        format="json",
    )

    assert first.status_code == 202
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "feishu_execution_in_flight"
    sync.refresh_from_db()
    second_approval.refresh_from_db()
    assert sync.execution_request_id == first_request_id
    assert second_approval.consumed_at is None
    assert ExternalExecutionRequest.objects.filter(org=org_a).count() == 1
    assert AutomationJob.objects.filter(org=org_a).count() == 1
    assert ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    ).reserved_units == 1


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_snapshot_change_before_mutation_is_failed_and_refunded(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="snapshot-refund")
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    intent = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    ).json()["intent"]
    approval = _approval(org_a, admin_profile, target, intent)
    queued = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {"approval_id": str(approval.id), "idempotency_key": str(uuid4())},
        format="json",
    )
    job = AutomationJob.objects.get(id=queued.json()["job_id"])
    client = _SnapshotChangingClient(connection.id)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client", lambda: client
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_feishu_base_sync_job(job.payload)

    assert exc_info.value.code == "feishu_execution_snapshot_changed"
    request = ExternalExecutionRequest.objects.get(
        id=job.payload["execution_request_id"]
    )
    sync = FeishuBaseSync.objects.get(intake=intake)
    channel = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    org_control = OrganizationExecutionControl.objects.get(org=org_a)
    assert client.created == 0
    assert request.status == ExternalRequestStatus.FAILED
    assert sync.status == FeishuBaseSyncStatus.FAILED
    assert channel.reserved_units == channel.consumed_units == 0
    assert org_control.reserved_units == org_control.consumed_units == 0


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_post_mutation_double_local_failure_never_refunds_or_replays(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="double-local-failure")
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    intent = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    ).json()["intent"]
    approval = _approval(org_a, admin_profile, target, intent)
    queued = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {"approval_id": str(approval.id), "idempotency_key": str(uuid4())},
        format="json",
    )
    job = AutomationJob.objects.get(id=queued.json()["job_id"])
    client = _SuccessfulClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client", lambda: client
    )
    original_save = FeishuBaseSync.save

    def fail_success_projection(instance, *args, **kwargs):
        if instance.status == FeishuBaseSyncStatus.SUCCEEDED:
            raise RuntimeError("local success projection unavailable")
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(FeishuBaseSync, "save", fail_success_projection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._mark_unknown",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("unknown projection unavailable")
        ),
    )
    release_calls = []
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._release_request",
        lambda **kwargs: release_calls.append(kwargs),
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_feishu_base_sync_job(job.payload)

    assert exc_info.value.code == "feishu_local_persistence_uncertain"
    request = ExternalExecutionRequest.objects.get(
        id=job.payload["execution_request_id"]
    )
    channel = ChannelExecutionControl.objects.get(
        org=org_a, channel=ExecutionChannel.FEISHU
    )
    assert client.created == 1
    assert release_calls == []
    assert request.status == ExternalRequestStatus.SENDING
    assert channel.reserved_units == 1
    assert channel.consumed_units == 0

    with pytest.raises(PermanentJobError) as replay_error:
        process_feishu_base_sync_job(job.payload)
    assert replay_error.value.code == "feishu_execution_not_replayable"
    assert client.created == 1
    assert release_calls == []


@pytest.mark.django_db
def test_anonymization_preserves_unknown_sync_for_manual_reconciliation(org_a):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="unknown-governance")
    sync = FeishuBaseSync.objects.create(
        org=org_a,
        connection=connection,
        intake=intake,
        status=FeishuBaseSyncStatus.UNKNOWN,
        destination_sha256="d" * 64,
        payload_sha256="p" * 64,
        error_code="feishu_outcome_unknown",
    )

    result = FeishuBaseDataGovernanceAdapter().anonymize_intake_data(
        org_id=org_a.id,
        intake_id=intake.id,
        marker="redacted",
    )

    sync.refresh_from_db()
    assert sync.status == FeishuBaseSyncStatus.UNKNOWN
    assert sync.error_code == "feishu_outcome_unknown"
    assert result["feishu_manual_reconciliation_required"] == 1
    assert result["feishu_local_sync_skipped"] == 0


@pytest.mark.django_db
def test_hard_delete_cannot_remove_feishu_remote_erasure_ledger(org_a):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="protected-ledger")
    sync = FeishuBaseSync(
        org=org_a,
        connection=connection,
        intake=intake,
        status=FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING,
        destination_sha256="d" * 64,
        payload_sha256="p" * 64,
    )
    sync.set_record_id("rec-protected-ledger")
    sync.save()

    with pytest.raises(ProtectedError):
        intake.delete()

    assert LeadIntake.objects.filter(id=intake.id).exists()
    assert FeishuBaseSync.objects.filter(id=sync.id).exists()


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_stale_research_sync_projects_unknown_and_can_resolve_failed(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="stale-sync")
    target = _enable_safety(org_a, admin_profile, connection)
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    intent = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/", {}, format="json"
    ).json()["intent"]
    approval = _approval(org_a, admin_profile, target, intent)
    queued = admin_client.post(
        f"{BASE}/intakes/{intake.id}/sync/",
        {"approval_id": str(approval.id), "idempotency_key": str(uuid4())},
        format="json",
    )
    request = ExternalExecutionRequest.objects.get(
        id=queued.json()["execution_request_id"]
    )
    mark_execution_sending(org=org_a, request_id=request.id)
    ExternalExecutionRequest.objects.filter(id=request.id).update(
        sending_at=timezone.now() - timedelta(hours=1)
    )

    reconciled = reconcile_stale_sending(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=30),
    )

    request.refresh_from_db()
    sync = FeishuBaseSync.objects.get(intake=intake)
    assert reconciled == [request.id]
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert sync.status == FeishuBaseSyncStatus.UNKNOWN
    assert sync.error_code == "stale_feishu_outcome_unknown"

    resolve_unknown_execution(
        org=org_a,
        actor=admin_profile,
        request_id=request.id,
        outcome="failed_consumed",
    )
    sync.refresh_from_db()
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    assert sync.status == FeishuBaseSyncStatus.FAILED


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_stale_delete_projects_unknown_and_delivered_resolution_clears_record(
    admin_client, org_a, admin_profile, monkeypatch
):
    connection = _connection(org_a)
    intake = _intake(org_a, suffix="stale-delete")
    target = _enable_safety(org_a, admin_profile, connection)
    sync = FeishuBaseSync(
        org=org_a,
        connection=connection,
        intake=intake,
        status=FeishuBaseSyncStatus.SUCCEEDED,
        destination_sha256="d" * 64,
        payload_sha256="p" * 64,
    )
    sync.set_record_id("rec-stale-delete-private")
    sync.save()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync.dispatch_job", lambda job: False
    )
    intent = admin_client.post(
        f"{BASE}/syncs/{sync.id}/delete/", {}, format="json"
    ).json()["intent"]
    approval = _approval(org_a, admin_profile, target, intent)
    queued = admin_client.post(
        f"{BASE}/syncs/{sync.id}/delete/",
        {"approval_id": str(approval.id), "idempotency_key": str(uuid4())},
        format="json",
    )
    request = ExternalExecutionRequest.objects.get(
        id=queued.json()["execution_request_id"]
    )
    mark_execution_sending(org=org_a, request_id=request.id)
    ExternalExecutionRequest.objects.filter(id=request.id).update(
        sending_at=timezone.now() - timedelta(hours=1)
    )

    reconcile_stale_sending(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=30),
    )
    sync.refresh_from_db()
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert sync.status == FeishuBaseSyncStatus.UNKNOWN
    assert sync.has_remote_record is True

    resolve_unknown_execution(
        org=org_a,
        actor=admin_profile,
        request_id=request.id,
        outcome="delivered",
    )
    sync.refresh_from_db()
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.DELIVERED
    assert sync.status == FeishuBaseSyncStatus.EXTERNAL_ERASURE_COMPLETED
    assert sync.has_remote_record is False
