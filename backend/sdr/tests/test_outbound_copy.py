import json

import pytest
from django.test import override_settings

from automation.models import AutomationJob
from sdr.intelligence.registry import ProviderDefinition
from sdr.models import (
    SDRIntelligenceSettings,
    SDRNurtureSequence,
    SDROutboundCampaign,
    SDROutboundCopyDraft,
)
from sdr.outbound_copy import (
    OutboundCopyClient,
    OutboundCopyProviderError,
    OutboundCopyResult,
    process_outbound_copy_job,
    validate_generated_steps,
)


def copy_steps(count=2):
    return [
        {
            "position": position,
            "delay_days": 0 if position == 1 else 3,
            "subject_a": f"A practical idea {position}",
            "opening_a": "Hi {{ first_name }}, I noticed your operations role.",
            "body_a": (
                "Hi {{ first_name }}, I noticed your operations role at "
                "{{ company_name }}. We help teams reduce manual handoffs. "
                "Would a short comparison be useful?"
            ),
            "cta_a": "Would a short comparison be useful?",
            "subject_b": f"Reducing manual handoffs {position}",
            "opening_b": "Hi {{ first_name }}, a quick workflow question.",
            "body_b": (
                "Hi {{ first_name }}, a quick workflow question for "
                "{{ company_name }}. Is reducing manual handoffs a priority? "
                "Happy to send a concise example."
            ),
            "cta_b": "May I send a concise example?",
            "rationale": "Tests a benefit-led angle against a problem-led angle.",
        }
        for position in range(1, count + 1)
    ]


class FakeResponse:
    status_code = 200

    def __init__(self, steps, *, extra_output=None):
        self.steps = steps
        self.extra_output = extra_output or {}

    def json(self):
        output = {"steps": self.steps, **self.extra_output}
        return {
            "id": "resp_copy_1",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(output),
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 500, "output_tokens": 300},
        }


class FakeSession:
    def __init__(self, steps, *, extra_output=None):
        self.steps = steps
        self.extra_output = extra_output
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse(self.steps, extra_output=self.extra_output)


def test_openai_copy_client_uses_strict_schema_and_non_stored_response():
    session = FakeSession(copy_steps())
    definition = ProviderDefinition(
        provider="openai",
        label="OpenAI",
        protocol="responses",
        base_url="https://api.openai.com/v1",
        api_key="",
        models=("gpt-5.6-luna",),
        timeout_seconds=30,
    )
    client = OutboundCopyClient(
        definition=definition,
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=session,
    )

    result = client.generate(
        org_id="00000000-0000-0000-0000-000000000001",
        context={"request": {"step_count": 2}},
    )

    url, request = session.request
    payload = request["json"]
    assert url == "https://api.openai.com/v1/responses"
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert len(payload["safety_identifier"]) == 64
    assert len(result.steps) == 2
    assert result.input_tokens == 500


def test_generated_copy_rejects_unknown_template_variables():
    steps = copy_steps(1)
    steps[0]["body_a"] = "Hello {{ account_name }}"

    with pytest.raises(ValueError, match="Unknown template variable"):
        validate_generated_steps(steps, expected_count=1)


def test_copy_client_rejects_unexpected_root_fields():
    definition = ProviderDefinition(
        provider="openai",
        label="OpenAI",
        protocol="responses",
        base_url="https://api.openai.com/v1",
        api_key="",
        models=("gpt-5.6-luna",),
        timeout_seconds=30,
    )
    client = OutboundCopyClient(
        definition=definition,
        api_key="test-key",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        session=FakeSession(copy_steps(), extra_output={"campaign_action": "launch"}),
    )

    with pytest.raises(OutboundCopyProviderError, match="invalid outbound copy"):
        client.generate(
            org_id="00000000-0000-0000-0000-000000000001",
            context={"request": {"step_count": 2}},
        )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_copy_api_queues_generation_and_requires_human_apply(
    admin_client,
    admin_profile,
    org_a,
    monkeypatch,
):
    campaign = SDROutboundCampaign.objects.create(
        org=org_a,
        name="AI copy campaign",
        description="Industrial workflow platform",
        icp_description="Manufacturing operations leaders",
        channels=["email"],
    )
    generated = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/copy-drafts/",
        {
            "language": "English",
            "tone": "concise and consultative",
            "offering_summary": "Workflow automation for industrial sales teams.",
            "value_proposition": "Reduce manual lead handoffs.",
            "proof_points": "No unsupported claims supplied.",
            "cta_goal": "Ask permission to share a short workflow comparison.",
            "step_count": 2,
        },
        format="json",
    )
    assert generated.status_code == 202, generated.json()
    draft = SDROutboundCopyDraft.objects.get(id=generated.json()["id"])
    assert draft.status == "pending"
    assert campaign.sequence_id is None
    assert AutomationJob.objects.filter(
        id=generated.json()["job_id"],
        name="sdr.generate_outbound_copy",
    ).exists()

    SDRIntelligenceSettings.objects.create(org=org_a, is_enabled=True)

    class FakeGateway:
        def generate(self, *, context):
            assert context["campaign"]["icp_description"] == (
                "Manufacturing operations leaders"
            )
            return OutboundCopyResult(
                steps=tuple(copy_steps()),
                response_id="resp-copy-test",
                input_tokens=120,
                output_tokens=80,
                provider="openai",
                model="gpt-5.6-luna",
                attempts=(
                    {
                        "provider": "openai",
                        "model": "gpt-5.6-luna",
                        "status": "completed",
                        "credential_source": "platform",
                    },
                ),
            )

    monkeypatch.setattr(
        "sdr.outbound_copy.OutboundCopyGateway.for_configuration",
        lambda configuration: FakeGateway(),
    )
    result = process_outbound_copy_job(
        {"org_id": str(org_a.id), "draft_id": str(draft.id)}
    )
    assert result["status"] == "ready"
    draft.refresh_from_db()
    assert draft.status == "ready"
    assert draft.provider == "openai"
    campaign.refresh_from_db()
    assert campaign.sequence_id is None

    edited_steps = copy_steps()
    edited_steps[0]["subject_a"] = "Human-reviewed subject"
    edited = admin_client.patch(
        f"/api/sdr/outbound/copy-drafts/{draft.id}/",
        {"generated_steps": edited_steps},
        format="json",
    )
    assert edited.status_code == 200, edited.json()
    assert edited.json()["reviewed_by_name"]

    applied = admin_client.post(
        f"/api/sdr/outbound/copy-drafts/{draft.id}/action/",
        {"action": "apply"},
        format="json",
    )
    assert applied.status_code == 200, applied.json()
    sequence = SDRNurtureSequence.objects.get(id=applied.json()["sequence_id"])
    assert sequence.is_active is False
    assert sequence.sources == ["outbound"]
    assert sequence.steps.count() == 2
    first = sequence.steps.get(position=1)
    assert first.subject_a == "Human-reviewed subject"
    assert first.variant_b_percent == 50
    draft.refresh_from_db()
    assert draft.status == "applied"
    assert draft.reviewed_by == admin_profile
    campaign.refresh_from_db()
    assert campaign.sequence == sequence
