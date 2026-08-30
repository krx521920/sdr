from __future__ import annotations

from math import ceil
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.test import override_settings

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from integrations.models import (
    FeishuBaseConnection,
    FeishuBaseSync,
    FeishuBaseSyncStatus,
)
from integrations.providers.feishu_base.client import (
    FeishuBaseAPIError,
    FeishuBaseClient,
    FeishuBaseConfigurationError,
    FeishuBaseRecord,
    MAX_FEISHU_READ_REQUESTS,
    validate_field_mapping,
)
from sdr.compliance import anonymize_intake, request_intake_deletion
from sdr.models import LeadIntake, SDRComplianceEvent
from sdr.response import schedule_post_handoff_jobs

FIELD_MAPPING = {
    "intake_id": "Intake ID",
    "company_name": "Company",
    "website": "Website",
    "research_summary": "Research",
    "qualification_score": "Score",
    "processed_at": "Processed At",
}
PROVIDER_FIELDS = [
    {"field_name": "Intake ID", "type": 1},
    {"field_name": "Company", "type": 1},
    {"field_name": "Website", "type": 15},
    {"field_name": "Research", "type": 1},
    {"field_name": "Score", "type": 2},
    {"field_name": "Processed At", "type": 5},
]


def test_feishu_person_import_lease_covers_bounded_provider_reads():
    assert (
        settings.FEISHU_PERSON_IMPORT_MAX_PROVIDER_CALLS
        == MAX_FEISHU_READ_REQUESTS
    )
    required = (
        ceil(settings.FEISHU_OPEN_API_TIMEOUT * MAX_FEISHU_READ_REQUESTS)
        + settings.FEISHU_PERSON_IMPORT_LEASE_MARGIN_SECONDS
    )
    assert settings.AUTOMATION_JOB_LEASE_SECONDS >= required


class JSONResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, json, timeout, allow_redirects):
        self.calls.append((method, url, headers, json, timeout, allow_redirects))
        return self.responses.pop(0)


@override_settings(ALLOW_UNGUARDED_PROVIDER_IO=True)
def test_feishu_client_uses_official_v1_endpoints_and_business_key_upsert():
    session = RecordingSession(
        [
            JSONResponse({"code": 0, "tenant_access_token": "tenant-token"}),
            JSONResponse({"code": 0, "data": {"items": PROVIDER_FIELDS}}),
            JSONResponse({"code": 0, "data": {"items": []}}),
            JSONResponse(
                {
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": "rec-created",
                            "fields": {"Intake ID": "intake-1"},
                        }
                    },
                }
            ),
        ]
    )
    client = FeishuBaseClient(session=session)
    token = client.tenant_access_token(app_id="cli_1", app_secret="secret")
    fields = client.list_fields(
        access_token=token,
        app_token="bascn-app",
        table_id="tbl-target",
    )
    found = client.find_record_by_field(
        access_token=token,
        app_token="bascn-app",
        table_id="tbl-target",
        field_name="Intake ID",
        value="intake-1",
    )
    created = client.create_record(
        access_token=token,
        app_token="bascn-app",
        table_id="tbl-target",
        fields={"Intake ID": "intake-1"},
    )

    assert token == "tenant-token"
    assert fields == PROVIDER_FIELDS
    assert found is None
    assert created.record_id == "rec-created"
    assert session.calls[0][0:2] == (
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
    )
    assert "Authorization" not in session.calls[0][2]
    assert session.calls[0][3] == {"app_id": "cli_1", "app_secret": "secret"}
    assert session.calls[0][5] is False
    assert session.calls[2][0:2] == (
        "POST",
        "https://open.feishu.cn/open-apis/bitable/v1/apps/bascn-app/tables/tbl-target/records/search?page_size=2",
    )
    assert session.calls[2][2]["Authorization"] == "Bearer tenant-token"
    assert session.calls[2][3]["filter"]["conditions"] == [
        {"field_name": "Intake ID", "operator": "is", "value": ["intake-1"]}
    ]


@override_settings(ALLOW_UNGUARDED_PROVIDER_IO=True)
def test_feishu_client_classifies_write_conflicts_as_retryable():
    session = RecordingSession(
        [JSONResponse({"code": 1254291, "msg": "Write conflict"})]
    )
    client = FeishuBaseClient(session=session)

    with pytest.raises(FeishuBaseAPIError) as captured:
        client.list_fields(
            access_token="tenant-token",
            app_token="bascn-app",
            table_id="tbl-target",
        )

    assert captured.value.retryable is True
    assert captured.value.error_code == "feishu_provider_retryable"
    assert (
        str(captured.value) == "Feishu OpenAPI request did not complete successfully."
    )
    assert "Write conflict" not in str(captured.value)


def test_feishu_mapping_rejects_missing_and_wrong_field_types():
    with pytest.raises(FeishuBaseConfigurationError) as captured:
        validate_field_mapping(
            {
                "intake_id": "Missing ID",
                "qualification_score": "Company",
            },
            PROVIDER_FIELDS,
        )

    assert captured.value.missing_fields == ["Missing ID"]
    assert captured.value.type_mismatches == [
        {
            "key": "qualification_score",
            "field_name": "Company",
            "actual_type": 1,
            "expected_types": [2],
        }
    ]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_feishu_connection_encrypts_secret_allows_inbound_only_and_is_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
    org_a,
    monkeypatch,
):
    inbound_only = admin_client.put(
        "/api/integrations/feishu-base/connection/",
        {
            "app_id": "cli_test",
            "app_secret": "super-secret",
            "app_token": "bascn-app",
            "table_id": "tbl-target",
            "field_mapping": {},
            "is_active": True,
        },
        format="json",
    )
    assert inbound_only.status_code == 200, inbound_only.json()
    assert inbound_only.json()["field_mapping"] == {}

    configured = admin_client.put(
        "/api/integrations/feishu-base/connection/",
        {
            "app_id": "cli_test",
            "app_secret": "super-secret",
            "app_token": "bascn-app",
            "table_id": "tbl-target",
            "field_mapping": FIELD_MAPPING,
            "is_active": True,
        },
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    connection = FeishuBaseConnection.objects.get(org=org_a)
    assert connection.app_secret_ciphertext != "super-secret"
    assert connection.get_app_secret() == "super-secret"
    assert configured.json()["app_secret_configured"] is True
    assert "app_secret" not in configured.json()
    assert (
        user_client.get("/api/integrations/feishu-base/connection/").status_code == 403
    )
    assert (
        org_b_client.get("/api/integrations/feishu-base/connection/").json()["id"]
        is None
    )

    tested = admin_client.post(
        "/api/integrations/feishu-base/connection/test/",
        {},
        format="json",
    )
    assert tested.status_code == 200, tested.json()
    assert tested.json()["approval_required"] is True
    assert tested.json()["intent"]["action"] == "validate_base_schema"
    assert not AutomationJob.objects.filter(org=org_a).exists()
    connection.refresh_from_db()
    assert connection.last_validated_at is None
    assert "app_token" not in configured.json()
    assert "table_id" not in configured.json()


class FakeSyncClient:
    def __init__(self):
        self.record = None
        self.created_fields = None
        self.updated_fields = None

    def tenant_access_token(self, **kwargs):
        assert kwargs == {"app_id": "cli_test", "app_secret": "super-secret"}
        return "tenant-token"

    def list_fields(self, **kwargs):
        return PROVIDER_FIELDS

    def find_record_by_field(self, **kwargs):
        assert kwargs["field_name"] == "Intake ID"
        return self.record

    def create_record(self, **kwargs):
        self.created_fields = kwargs["fields"]
        self.record = FeishuBaseRecord(
            record_id="rec-sdr-1",
            fields=dict(self.created_fields),
        )
        return self.record

    def update_record(self, **kwargs):
        assert kwargs["record_id"] == "rec-sdr-1"
        self.updated_fields = kwargs["fields"]
        self.record = FeishuBaseRecord(
            record_id="rec-sdr-1",
            fields=dict(self.updated_fields),
        )
        return self.record


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_completed_research_requires_explicit_feishu_approval_before_job_creation(
    admin_client,
    org_a,
    monkeypatch,
):
    configured = admin_client.put(
        "/api/integrations/feishu-base/connection/",
        {
            "app_id": "cli_test",
            "app_secret": "super-secret",
            "app_token": "bascn-app",
            "table_id": "tbl-target",
            "field_mapping": FIELD_MAPPING,
            "is_active": True,
        },
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    accepted = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "feishu-base-intake-1",
            "first_name": "Ada",
            "email": "ada@example.com",
            "company_name": "Analytical Engines Ltd",
            "website": "https://analytical.example",
        },
        format="json",
    )
    assert accepted.status_code == 202, accepted.json()
    intake_job = AutomationJob.objects.get(id=accepted.json()["job_id"])
    run_automation_job.run(str(intake_job.id), str(org_a.id))
    intake = LeadIntake.objects.select_related("inspection").get(
        id=accepted.json()["intake_id"]
    )
    assert not FeishuBaseSync.objects.filter(intake=intake).exists()
    assert not AutomationJob.objects.filter(
        org=org_a,
        name="integrations.feishu_base_sync",
    ).exists()
    intent = admin_client.post(
        f"/api/integrations/feishu-base/intakes/{intake.id}/sync/",
        {},
        format="json",
    )
    assert intent.status_code == 200
    assert intent.json()["approval_required"] is True
    assert intent.json()["intent"]["action"] == "sync_research_result"
    assert not FeishuBaseSync.objects.filter(intake=intake).exists()


@pytest.mark.django_db
def test_deletion_request_stops_queued_feishu_base_export(org_a, monkeypatch):
    connection = FeishuBaseConnection(
        org=org_a,
        app_id="cli_test",
        app_token="bascn-app",
        table_id="tbl-target",
        field_mapping=FIELD_MAPPING,
        is_active=True,
    )
    connection.set_app_secret("super-secret")
    connection.full_clean()
    connection.save()
    intake = LeadIntake.objects.create(
        org=org_a,
        source="website_form",
        source_record_id="feishu-base-deletion-request",
        raw_payload={
            "email": "ada@example.com",
            "company_name": "Analytical Engines Ltd",
        },
        normalized_payload={
            "identity": {"email": "ada@example.com"},
            "company": {"name": "Analytical Engines Ltd"},
        },
        status="completed",
    )
    scheduled = schedule_post_handoff_jobs(intake)
    assert all(job.name != "integrations.feishu_base_sync" for job in scheduled)
    assert all(job.name != "feishu_base.sync_research_result" for job in scheduled)
    request_intake_deletion(intake)
    provider_client = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client",
        provider_client,
    )

    assert not FeishuBaseSync.objects.filter(intake=intake).exists()
    provider_client.assert_not_called()
    scheduled_after_deletion = schedule_post_handoff_jobs(intake)
    assert all(
        job.name not in {"integrations.feishu_base_sync", "feishu_base.sync_research_result"}
        for job in scheduled_after_deletion
    )


@pytest.mark.django_db
def test_anonymize_marks_existing_feishu_row_pending_external_erasure(org_a):
    connection = FeishuBaseConnection(
        org=org_a,
        app_id="cli_test",
        app_token="bascn-app",
        table_id="tbl-target",
        field_mapping=FIELD_MAPPING,
        is_active=True,
    )
    connection.set_app_secret("super-secret")
    connection.full_clean()
    connection.save()
    intake = LeadIntake.objects.create(
        org=org_a,
        source="website_form",
        source_record_id="feishu-base-external-erasure",
        raw_payload={"email": "subject@example.com"},
        normalized_payload={"identity": {"email": "subject@example.com"}},
        status="completed",
    )
    sync = FeishuBaseSync(
        org=org_a,
        connection=connection,
        intake=intake,
        status=FeishuBaseSyncStatus.SUCCEEDED,
        destination_sha256="d" * 64,
        payload_sha256="p" * 64,
        synced_field_names=["Email", "Research"],
    )
    sync.set_record_id("rec-private-data")
    sync.save()

    anonymize_intake(intake)

    sync.refresh_from_db()
    assert sync.status == FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING
    assert sync.get_record_id() == "rec-private-data"
    assert sync.synced_field_names == []
    assert sync.error_code == "pending_external_erasure"
    event = SDRComplianceEvent.objects.get(
        org=org_a,
        event_type="anonymized",
    )
    assert event.snapshot["provider_governance"]["feishu_pending_external_erasure"] == 1
