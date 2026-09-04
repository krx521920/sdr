import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import override_settings

from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.intelligence.contracts import (
    AIQualification,
    ModelProviderError,
    build_lead_context,
)
from sdr.intelligence.deepseek_client import DeepSeekLeadQualifier
from sdr.intelligence.doubao_client import DoubaoLeadQualifier
from sdr.intelligence.gateway import ModelGateway
from sdr.intelligence.safety import prepare_ai_context
from sdr.models import SDRAICallAudit, SDRIntelligenceSettings, SDRModelCredential


def lead_candidate():
    return LeadCandidate(
        org_id=uuid4(),
        source=LeadSource.WEBSITE_FORM,
        source_record_id="gateway-lead-1",
        identity=LeadIdentity(email="buyer@acme.example"),
        company=CompanySnapshot(name="Acme", website="https://acme.example"),
        attributes={"job_title": "VP Operations", "is_business_email": True},
    )


def structured_result():
    return {
        "score": 86,
        "band": "high",
        "reasons": ["Operations buyer", "Strong workflow need"],
        "company_summary": "Industrial workflow software company.",
        "industry": "Industrial software",
        "company_size": "mid_market",
        "business_model": "B2B SaaS",
        "fit_signals": ["Operations team", "Automation use case"],
        "disqualifying_reason": None,
    }


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse(self.body)


def qualification_kwargs(candidate):
    return {
        "org_id": candidate.org_id,
        "candidate": candidate,
        "baseline": QualificationResult(55, QualificationBand.MEDIUM),
        "research": None,
        "icp_description": "Manufacturing companies",
        "positive_signals": "Operations leader",
        "negative_signals": "Students",
    }


def prepared_qualification_context(candidate):
    kwargs = qualification_kwargs(candidate)
    return prepare_ai_context(
        purpose="lead_qualification",
        context=build_lead_context(
            candidate=kwargs["candidate"],
            baseline=kwargs["baseline"],
            research=kwargs["research"],
            icp_description=kwargs["icp_description"],
            positive_signals=kwargs["positive_signals"],
            negative_signals=kwargs["negative_signals"],
        ),
        pii_handling="redact",
        max_chars=30000,
        max_tokens=30000,
    )


def test_deepseek_adapter_uses_chat_completions_json_output():
    session = FakeSession(
        {
            "id": "chatcmpl_deepseek_1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(structured_result()),
                    }
                }
            ],
            "usage": {"prompt_tokens": 210, "completion_tokens": 48},
        }
    )
    client = DeepSeekLeadQualifier(
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        reasoning_effort="low",
        base_url="https://api.deepseek.com",
        session=session,
    )

    item = lead_candidate()
    result = client.qualify(
        org_id=item.org_id,
        context=prepared_qualification_context(item),
    )

    url, request = session.request
    payload = request["json"]
    assert url == "https://api.deepseek.com/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer deepseek-key"
    assert request["allow_redirects"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert "required output JSON Schema" in payload["messages"][0]["content"]
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-flash"
    assert result.qualification.score == 86
    assert result.input_tokens == 210


def test_doubao_adapter_uses_responses_strict_schema():
    session = FakeSession(
        {
            "id": "resp_doubao_1",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(structured_result())}
                    ],
                }
            ],
            "usage": {"input_tokens": 190, "output_tokens": 51},
        }
    )
    client = DoubaoLeadQualifier(
        api_key="doubao-key",
        model="doubao-seed-2-0-lite-260215",
        reasoning_effort="none",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        session=session,
    )

    item = lead_candidate()
    result = client.qualify(
        org_id=item.org_id,
        context=prepared_qualification_context(item),
    )

    url, request = session.request
    payload = request["json"]
    assert url == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert request["allow_redirects"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["thinking"] == {"type": "disabled"}
    assert result.provider == "doubao"
    assert result.qualification.band == QualificationBand.HIGH
    assert result.output_tokens == 51


@pytest.mark.django_db
@override_settings(OPENAI_API_KEY="primary-key", DEEPSEEK_API_KEY="fallback-key")
def test_gateway_fails_over_and_records_attempts(org_a):
    configuration = SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        fallback_provider="deepseek",
        fallback_model="deepseek-v4-flash",
    )

    class PrimaryClient:
        def qualify(self, **kwargs):
            raise ModelProviderError(
                "primary unavailable",
                code="openai_request_failed",
                retryable=True,
            )

    class FallbackClient:
        def qualify(self, **kwargs):
            return AIQualification(
                qualification=QualificationResult(86, QualificationBand.HIGH),
                response_id="fallback-response",
                input_tokens=20,
                output_tokens=8,
                provider="deepseek",
                model="deepseek-v4-flash",
            )

    def client_factory(*, definition, **kwargs):
        return PrimaryClient() if definition.provider == "openai" else FallbackClient()

    gateway = ModelGateway(
        org_id=org_a.id,
        routes=(
            ("openai", "gpt-5.6-luna", "low"),
            ("deepseek", "deepseek-v4-flash", "low"),
        ),
        configuration=configuration,
    )

    with patch(
        "sdr.intelligence.gateway._build_provider_client",
        side_effect=client_factory,
    ):
        result = gateway.qualify(**qualification_kwargs(lead_candidate()))

    assert result.provider == "deepseek"
    assert result.gateway_fallback_used is True
    assert [attempt["status"] for attempt in result.attempts] == [
        "failed",
        "completed",
    ]
    assert result.attempts[0]["error_code"] == "openai_request_failed"
    assert result.attempts[1]["credential_source"] == "platform"
    audits = list(SDRAICallAudit.objects.filter(org=org_a).order_by("route_index"))
    assert [audit.status for audit in audits] == ["failed", "completed"]
    assert audits[0].request_id == audits[1].request_id
    assert audits[0].fallback_used is False
    assert audits[1].fallback_used is True


@pytest.mark.django_db
@override_settings(DEEPSEEK_API_KEY="platform-key")
def test_gateway_prefers_tenant_credential(org_a):
    configuration = SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    credential = SDRModelCredential(org=org_a, provider="deepseek")
    credential.set_api_key("tenant-key")
    credential.save()
    captured = {}

    class FakeClient:
        def qualify(self, **kwargs):
            return AIQualification(
                qualification=QualificationResult(75, QualificationBand.HIGH),
                response_id="tenant-response",
                input_tokens=None,
                output_tokens=None,
                provider="deepseek",
                model="deepseek-v4-flash",
            )

    def client_factory(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    with patch(
        "sdr.intelligence.gateway._build_provider_client",
        side_effect=client_factory,
    ):
        result = ModelGateway(
            org_id=org_a.id,
            routes=(("deepseek", "deepseek-v4-flash", "low"),),
            configuration=configuration,
        ).qualify(**qualification_kwargs(lead_candidate()))

    assert captured["api_key"] == "tenant-key"
    assert result.attempts[0]["credential_source"] == "tenant"
