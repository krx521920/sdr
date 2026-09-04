import pytest
from django.test import override_settings

from sdr.models import LeadInspection, LeadIntake, SDRModelCredential


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls", OPENAI_API_KEY="platform-key")
def test_intelligence_settings_are_admin_only_and_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
):
    denied = user_client.get("/api/sdr/intelligence/settings/")
    initial = admin_client.get("/api/sdr/intelligence/settings/")
    updated = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {
            "is_enabled": True,
            "model": "gpt-5.6-luna",
            "icp_description": "Industrial companies with operations teams",
            "max_research_pages": 3,
        },
        format="json",
    )
    blocked_model = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {"model": "gpt-unapproved"},
        format="json",
    )
    blocked_effort = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {"reasoning_effort": "max"},
        format="json",
    )
    other_tenant = org_b_client.get("/api/sdr/intelligence/settings/")

    assert denied.status_code == 403
    assert initial.status_code == 200
    assert initial.json()["is_enabled"] is False
    assert initial.json()["provider"] == "openai"
    assert initial.json()["model"] == "gpt-5.6-luna"
    assert initial.json()["reasoning_effort"] == "low"
    assert initial.json()["openai_configured"] is True
    assert initial.json()["allowed_models"] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert set(initial.json()["provider_catalog"]) == {
        "openai",
        "doubao",
        "deepseek",
    }
    assert initial.json()["provider_catalog"]["openai"]["credential_source"] == (
        "platform"
    )
    assert updated.status_code == 200
    assert updated.json()["is_enabled"] is True
    assert updated.json()["max_research_pages"] == 3
    assert blocked_model.status_code == 400
    assert blocked_effort.status_code == 400
    assert other_tenant.status_code == 200
    assert other_tenant.json()["is_enabled"] is False


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="sdr.tests.urls",
    OPENAI_API_KEY="platform-key",
    AI_GATEWAY_ALLOWED_REASONING_EFFORTS=("medium",),
)
def test_intelligence_defaults_and_updates_stay_inside_reasoning_allowlist(
    admin_client,
):
    initial = admin_client.get("/api/sdr/intelligence/settings/")
    blocked = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {"reasoning_effort": "low"},
        format="json",
    )

    assert initial.status_code == 200
    assert initial.json()["reasoning_effort"] == "medium"
    assert initial.json()["allowed_reasoning_efforts"] == ["medium"]
    assert blocked.status_code == 400
    assert "reasoning_effort" in blocked.json()


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="sdr.tests.urls",
    DEEPSEEK_API_KEY="",
    AI_GATEWAY_ALLOW_TENANT_KEYS=True,
)
def test_tenant_can_store_and_clear_an_encrypted_provider_key(
    admin_client,
    org_b_client,
    org_a,
):
    stored = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "deepseek_api_key": "tenant-deepseek-secret",
        },
        format="json",
    )

    assert stored.status_code == 200
    body = stored.json()
    assert "deepseek_api_key" not in body
    assert body["provider_catalog"]["deepseek"]["configured"] is True
    assert body["provider_catalog"]["deepseek"]["credential_source"] == "tenant"
    assert body["provider_catalog"]["deepseek"]["key_hint"] == "k-secret"

    credential = SDRModelCredential.objects.get(org=org_a, provider="deepseek")
    assert "tenant-deepseek-secret" not in credential.api_key_ciphertext
    assert credential.get_api_key() == "tenant-deepseek-secret"

    isolated = org_b_client.get("/api/sdr/intelligence/settings/")
    assert isolated.status_code == 200
    assert isolated.json()["provider_catalog"]["deepseek"]["configured"] is False

    cleared = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {"clear_deepseek_api_key": True},
        format="json",
    )
    assert cleared.status_code == 200
    assert not SDRModelCredential.objects.filter(
        org=org_a,
        provider="deepseek",
    ).exists()


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="sdr.tests.urls",
    AI_GATEWAY_ALLOW_TENANT_KEYS=False,
)
def test_tenant_key_updates_can_be_disabled(admin_client):
    response = admin_client.patch(
        "/api/sdr/intelligence/settings/",
        {"doubao_api_key": "not-allowed"},
        format="json",
    )

    assert response.status_code == 400
    assert "api_keys" in response.json()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls")
def test_inspection_list_and_detail_do_not_cross_tenants(
    admin_client,
    org_b_client,
    org_a,
):
    intake = LeadIntake.objects.create(
        org=org_a,
        source="api",
        source_record_id="inspection-api-1",
        normalized_payload={"company": {"name": "Acme"}},
    )
    inspection = LeadInspection.objects.create(
        org=org_a,
        intake=intake,
        status="partial",
        qualification_score=58,
        qualification_band="medium",
        used_fallback=True,
        error_code="openai_request_failed",
    )

    listed = admin_client.get("/api/sdr/intelligence/inspections/")
    hidden_list = org_b_client.get("/api/sdr/intelligence/inspections/")
    detail = admin_client.get(f"/api/sdr/intelligence/inspections/{inspection.id}/")
    hidden_detail = org_b_client.get(
        f"/api/sdr/intelligence/inspections/{inspection.id}/"
    )

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["results"][0]["company_name"] == "Acme"
    assert hidden_list.status_code == 200
    assert hidden_list.json()["count"] == 0
    assert detail.status_code == 200
    assert detail.json()["error_code"] == "openai_request_failed"
    assert hidden_detail.status_code == 404
