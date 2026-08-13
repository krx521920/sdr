from __future__ import annotations

from unittest.mock import Mock

import pytest
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
    validate_field_mapping,
)
from sdr.models import LeadInspection, LeadIntake
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

    def request(self, method, url, *, headers, json, timeout):
        self.calls.append((method, url, headers, json, timeout))
        return self.responses.pop(0)


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
    assert session.calls[2][0:2] == (
        "POST",
        "https://open.feishu.cn/open-apis/bitable/v1/apps/bascn-app/tables/tbl-target/records/search?page_size=2",
    )
    assert session.calls[2][2]["Authorization"] == "Bearer tenant-token"
    assert session.calls[2][3]["filter"]["conditions"] == [
        {"field_name": "Intake ID", "operator": "is", "value": ["intake-1"]}
    ]


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
    assert captured.value.error_code == "feishu_provider_1254291"


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
def test_feishu_connection_encrypts_secret_validates_mapping_and_is_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
    org_a,
    monkeypatch,
):
    missing_mapping = admin_client.put(
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
    assert missing_mapping.status_code == 400

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

    fake = Mock()
    fake.tenant_access_token.return_value = "tenant-token"
    fake.list_fields.return_value = PROVIDER_FIELDS
    monkeypatch.setattr(
        "integrations.api.views.FeishuBaseClient",
        lambda **kwargs: fake,
    )
    tested = admin_client.post(
        "/api/integrations/feishu-base/connection/test/",
        format="json",
    )
    assert tested.status_code == 200, tested.json()
    assert tested.json()["valid"] is True
    assert tested.json()["field_count"] == len(PROVIDER_FIELDS)
    connection.refresh_from_db()
    assert connection.last_validated_at is not None


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
def test_completed_research_is_durably_created_then_updated_in_feishu_base(
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
    sync = FeishuBaseSync.objects.get(intake=intake)
    sync_job = AutomationJob.objects.get(name="feishu_base.sync_research_result")
    assert sync.status == FeishuBaseSyncStatus.QUEUED

    fake = FakeSyncClient()
    monkeypatch.setattr(
        "integrations.providers.feishu_base.sync._client",
        lambda: fake,
    )
    result = run_automation_job.run(str(sync_job.id), str(org_a.id))
    assert result["status"] == "succeeded"
    sync.refresh_from_db()
    assert sync.status == FeishuBaseSyncStatus.SUCCEEDED
    assert sync.record_id == "rec-sdr-1"
    assert fake.created_fields["Intake ID"] == str(intake.id)
    assert fake.created_fields["Company"] == "Analytical Engines Ltd"
    assert fake.created_fields["Website"] == {
        "link": "https://analytical.example",
        "text": "https://analytical.example",
    }
    assert isinstance(fake.created_fields["Score"], int)
    assert isinstance(fake.created_fields["Processed At"], int)

    LeadInspection.objects.create(
        org=org_a,
        intake=intake,
        status="completed",
        research_summary="Updated verified company research.",
        qualification_score=intake.qualification_score,
        qualification_band=intake.qualification_band,
    )
    scheduled = schedule_post_handoff_jobs(intake)
    replacement_job = next(
        job
        for job in scheduled
        if job.name == "feishu_base.sync_research_result" and job.id != sync_job.id
    )
    rerun = run_automation_job.run(str(replacement_job.id), str(org_a.id))
    assert rerun["status"] == "succeeded"
    assert fake.updated_fields["Research"] == "Updated verified company research."
    assert FeishuBaseSync.objects.filter(intake=intake).count() == 1
