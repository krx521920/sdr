import json
from uuid import uuid4

import pytest

from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.intelligence.contracts import build_lead_context
from sdr.intelligence.openai_client import (
    OpenAILeadQualifier,
    OpenAIQualificationError,
)
from sdr.intelligence.safety import AISafetyError, prepare_ai_context


class FakeResponse:
    def __init__(self, result, *, status_code=200):
        self.result = result
        self.status_code = status_code

    def json(self):
        return {
            "id": "resp_test_123",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(self.result)}
                    ],
                }
            ],
            "usage": {"input_tokens": 321, "output_tokens": 87},
        }


class FakeSession:
    def __init__(self, result, *, status_code=200):
        self.result = result
        self.status_code = status_code
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse(self.result, status_code=self.status_code)


def lead_candidate():
    return LeadCandidate(
        org_id=uuid4(),
        source=LeadSource.WEBSITE_FORM,
        source_record_id="lead-1",
        identity=LeadIdentity(email="buyer@acme.example"),
        company=CompanySnapshot(name="Acme", website="https://acme.example"),
        attributes={"job_title": "VP Operations", "is_business_email": True},
    )


def structured_result(**overrides):
    result = {
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
    result.update(overrides)
    return result


def prepared_qualification_context(candidate):
    return prepare_ai_context(
        purpose="lead_qualification",
        context=build_lead_context(
            candidate=candidate,
            baseline=QualificationResult(55, QualificationBand.MEDIUM),
            research=None,
            icp_description="Manufacturing companies",
            positive_signals="Operations leader",
            negative_signals="Students",
        ),
        pii_handling="redact",
        max_chars=30000,
        max_tokens=30000,
    )


def test_openai_qualifier_uses_responses_structured_output():
    session = FakeSession(structured_result())
    client = OpenAILeadQualifier(
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=session,
    )
    candidate = lead_candidate()

    result = client.qualify(
        org_id=candidate.org_id,
        context=prepared_qualification_context(candidate),
    )

    url, request = session.request
    payload = request["json"]
    assert url == "https://api.openai.com/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["allow_redirects"] is False
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert len(payload["safety_identifier"]) == 64
    assert result.qualification.score == 86
    assert result.qualification.band == QualificationBand.HIGH
    assert result.qualification.metadata["company_size"] == "mid_market"
    assert result.response_id == "resp_test_123"
    assert result.input_tokens == 321


def test_openai_qualifier_rejects_inconsistent_band():
    client = OpenAILeadQualifier(
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=FakeSession(structured_result(band="low")),
    )
    candidate = lead_candidate()

    with pytest.raises(OpenAIQualificationError) as caught:
        client.qualify(
            org_id=candidate.org_id,
            context=prepared_qualification_context(candidate),
        )

    assert caught.value.code == "openai_invalid_response"


def test_openai_qualifier_rejects_values_outside_the_shared_contract():
    client = OpenAILeadQualifier(
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=FakeSession(structured_result(company_size="huge")),
    )
    candidate = lead_candidate()

    with pytest.raises(OpenAIQualificationError) as caught:
        client.qualify(
            org_id=candidate.org_id,
            context=prepared_qualification_context(candidate),
        )

    assert caught.value.code == "openai_invalid_response"


def test_openai_adapter_rejects_unattested_raw_context_before_http():
    session = FakeSession(structured_result())
    client = OpenAILeadQualifier(
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=session,
    )

    with pytest.raises(AISafetyError) as caught:
        client.qualify(
            org_id=uuid4(),
            context={"person": {"message": "raw input"}},
        )

    assert caught.value.code == "ai_context_not_prepared"
    assert session.request is None


def test_openai_adapter_blocks_redirect_without_following_it():
    session = FakeSession(structured_result(), status_code=302)
    client = OpenAILeadQualifier(
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=session,
    )
    item = lead_candidate()

    with pytest.raises(OpenAIQualificationError) as caught:
        client.qualify(
            org_id=item.org_id,
            context=prepared_qualification_context(item),
        )

    assert caught.value.code == "openai_redirect_blocked"
    assert session.request[1]["allow_redirects"] is False
