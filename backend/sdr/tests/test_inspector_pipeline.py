import pytest
from django.test import override_settings

from sdr.domain import QualificationBand, QualificationResult
from sdr.intelligence.contracts import AIQualification
from sdr.intelligence.research import ResearchResult
from sdr.models import LeadInspection, SDRIntelligenceSettings


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    OPENAI_API_KEY="test-key",
)
def test_inspector_enriches_scores_and_audits_real_intake(
    admin_client,
    org_a,
    monkeypatch,
):
    SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        icp_description="Mid-market industrial operations teams",
    )
    monkeypatch.setattr(
        "sdr.intelligence.service.WebsiteResearcher.research",
        lambda self, website_url, **kwargs: ResearchResult(
            website_url=website_url,
            source_urls=(website_url, f"{website_url}/about"),
            summary="Factory workflow platform",
            facts={"page_title": "Acme"},
            content_sha256="a" * 64,
            model_context="Acme helps manufacturing operations automate workflows.",
        ),
    )
    monkeypatch.setattr(
        "sdr.intelligence.gateway.OpenAILeadQualifier.qualify",
        lambda self, **kwargs: AIQualification(
            qualification=QualificationResult(
                score=91,
                band=QualificationBand.HIGH,
                reasons=("ICP industry match", "Operations buyer"),
                model_version="openai:gpt-5.6-luna:lead-qualification-v1",
                metadata={
                    "company_summary": "Manufacturing workflow platform.",
                    "industry": "Industrial software",
                    "company_size": "mid_market",
                    "business_model": "B2B SaaS",
                    "fit_signals": ["Manufacturing", "Operations"],
                    "disqualifying_reason": None,
                },
            ),
            response_id="resp_inspector_1",
            input_tokens=400,
            output_tokens=90,
        ),
    )

    response = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "inspected-1",
            "email": "buyer@acme.example",
            "company_name": "Acme",
            "website": "https://acme.example",
            "job_title": "VP Operations",
        },
        format="json",
    )

    inspection = LeadInspection.objects.select_related("intake__crm_lead").get()
    lead = inspection.intake.crm_lead
    assert response.status_code == 201
    assert response.json()["qualification_score"] == 91
    assert response.json()["qualification_band"] == "high"
    assert inspection.status == "completed"
    assert inspection.provider == "openai"
    assert inspection.fallback_kind == ""
    assert inspection.provider_attempts == [
        {
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "status": "completed",
            "credential_source": "platform",
        }
    ]
    assert inspection.provider_response_id == "resp_inspector_1"
    assert len(inspection.configuration_sha256) == 64
    assert inspection.research_facts["company_size"] == "mid_market"
    assert inspection.source_urls == [
        "https://acme.example",
        "https://acme.example/about",
    ]
    assert lead.custom_fields["sdr"]["metadata"]["industry"] == "Industrial software"
    assert (
        lead.custom_fields["sdr"]["attributes"]["company_research"]["content_sha256"]
        == "a" * 64
    )


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    OPENAI_API_KEY="",
)
def test_missing_openai_key_falls_back_without_losing_lead(admin_client, org_a):
    SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        research_enabled=False,
        ai_scoring_enabled=True,
    )

    response = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "inspected-fallback",
            "email": "buyer@example.com",
            "company_name": "Fallback Co",
        },
        format="json",
    )

    inspection = LeadInspection.objects.select_related("intake").get()
    assert response.status_code == 201
    assert response.json()["lead_id"] is not None
    assert inspection.status == "partial"
    assert inspection.provider == "rules"
    assert inspection.used_fallback is True
    assert inspection.fallback_kind == "rules"
    assert inspection.provider_attempts[0]["provider"] == "openai"
    assert inspection.provider_attempts[0]["status"] == "failed"
    assert inspection.error_code == "openai_not_configured"
    assert inspection.qualification_score == response.json()["qualification_score"]
