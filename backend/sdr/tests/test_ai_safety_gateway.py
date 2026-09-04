import ast
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.intelligence.contracts import AIQualification
from sdr.intelligence.gateway import ModelGateway, UnifiedAIGateway
from sdr.intelligence.safety import (
    AISafetyError,
    prepare_ai_context,
    prepared_context_json,
)
from sdr.models import (
    SDRAICallAudit,
    SDRIntelligenceSettings,
    SDROutboundCampaign,
    SDROutboundCopyDraft,
    SDROutboundProspect,
)
from sdr.outbound_copy import _draft_context
from sdr.tasks import purge_expired_ai_call_audits


def qualification_context(message="Routine business request"):
    return {
        "source": "website_form",
        "person": {
            "job_title": "VP Operations",
            "has_business_email": True,
            "has_phone": True,
            "message": message,
        },
        "company": {
            "name": "Acme",
            "website": "https://acme.example",
            "industry": "Manufacturing",
            "country": "US",
        },
        "baseline": {"score": 60, "band": "medium", "reasons": ["Buyer"]},
        "tenant_icp": {
            "description": "Manufacturers",
            "positive_signals": "Operations",
            "negative_signals": "Students",
        },
        "website_research": {
            "source_urls": ["https://acme.example"],
            "content": "Workflow software",
        },
    }


def candidate(message="Routine business request"):
    return LeadCandidate(
        org_id=None,
        source=LeadSource.WEBSITE_FORM,
        source_record_id="safe-gateway-1",
        identity=LeadIdentity(email="private@example.com", phone="+1 202 555 0198"),
        company=CompanySnapshot(name="Acme", website="https://acme.example"),
        attributes={
            "job_title": "VP Operations",
            "is_business_email": True,
            "message": message,
        },
    )


def gateway_kwargs(item):
    return {
        "org_id": item.org_id,
        "candidate": item,
        "baseline": QualificationResult(60, QualificationBand.MEDIUM),
        "research": None,
        "icp_description": "Manufacturers",
        "positive_signals": "Operations",
        "negative_signals": "Students",
    }


def test_preflight_redacts_pii_in_free_text_and_keeps_no_values():
    prepared = prepare_ai_context(
        purpose="lead_qualification",
        context=qualification_context(
            "姓名：张三; email alice+sales@example.com; phone +86 138 0013 8000"
        ),
        pii_handling="redact",
        max_chars=30000,
        max_tokens=30000,
    )

    serialized = prepared.canonical_json
    assert "张三" not in serialized
    assert "alice+sales@example.com" not in serialized
    assert "138 0013 8000" not in serialized
    assert "[REDACTED_NAME]" in serialized
    assert "[REDACTED_EMAIL]" in serialized
    assert "[REDACTED_PHONE]" in serialized
    assert prepared.pii_findings == {"email": 1, "name": 1, "phone": 1}
    assert prepared.redaction_count == 3


@pytest.mark.parametrize(
    "mutator,code",
    [
        (
            lambda context: context.update({"raw_payload": "not allowed"}),
            "ai_field_not_allowed",
        ),
        (
            lambda context: context["person"].update(
                {"message": "api_key=" + "test-sensitive-material"}
            ),
            "ai_sensitive_content_blocked",
        ),
        (
            lambda context: context["person"].update(
                {"message": '{"password":"test-sensitive-material"}'}
            ),
            "ai_sensitive_content_blocked",
        ),
        (
            lambda context: context.update({"raw_payload": {}}),
            "ai_field_not_allowed",
        ),
    ],
)
def test_preflight_fails_closed_for_unknown_fields_and_secrets(mutator, code):
    context = qualification_context()
    mutator(context)
    with pytest.raises(AISafetyError) as caught:
        prepare_ai_context(
            purpose="lead_qualification",
            context=context,
            pii_handling="redact",
            max_chars=30000,
            max_tokens=30000,
        )
    assert caught.value.code == code


def test_preflight_enforces_input_limits_without_truncating():
    with pytest.raises(AISafetyError) as caught:
        prepare_ai_context(
            purpose="lead_qualification",
            context=qualification_context("x" * 4000),
            pii_handling="redact",
            max_chars=10000,
            max_tokens=100,
        )
    assert caught.value.code == "ai_token_limit_exceeded"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_preflight_rejects_nonstandard_json_numbers(value):
    context = qualification_context()
    context["baseline"]["score"] = value

    with pytest.raises(AISafetyError) as caught:
        prepare_ai_context(
            purpose="lead_qualification",
            context=context,
            pii_handling="block",
            max_chars=30000,
            max_tokens=30000,
        )
    assert caught.value.code == "ai_input_invalid"


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_json": '{"person":{"message":"alice@example.com"}}'},
        {"purpose": "outbound_copy"},
        {"input_sha256": "0" * 64},
        {"field_paths": ("raw_payload",)},
        {"pii_findings": {"email": 999}},
        {"redaction_count": 0},
        {"input_chars": 1},
        {"estimated_input_tokens": 1},
    ],
)
def test_prepared_context_attestation_rejects_dataclass_replace_tampering(changes):
    prepared = prepare_ai_context(
        purpose="lead_qualification",
        context=qualification_context("Email alice@example.com"),
        pii_handling="redact",
        max_chars=30000,
        max_tokens=30000,
    )
    assert (
        prepared_context_json(prepared, expected_purpose="lead_qualification")
        == prepared.canonical_json
    )

    tampered = replace(prepared, **changes)

    with pytest.raises(AISafetyError) as caught:
        prepared_context_json(tampered, expected_purpose="lead_qualification")
    assert caught.value.code == "ai_context_not_prepared"


@pytest.mark.parametrize(
    "message,name",
    [
        ("I am Alice Smith and need workflow help", "Alice Smith"),
        ("Please contact Alice Smith", "Alice Smith"),
        ("我是张三", "张三"),
        ("请联系张三", "张三"),
    ],
)
def test_preflight_redacts_contextual_names_in_free_text(message, name):
    prepared = prepare_ai_context(
        purpose="lead_qualification",
        context=qualification_context(message),
        pii_handling="redact",
        max_chars=30000,
        max_tokens=30000,
    )

    assert name not in prepared.canonical_json
    assert "[REDACTED_NAME]" in prepared.canonical_json
    assert prepared.pii_findings["name"] == 1


@pytest.mark.parametrize(
    "message",
    [
        "I am evaluating a workflow automation platform.",
        "I'm responsible for sales operations.",
        "Please contact our sales team for implementation details.",
        "Reach out to discuss workflow automation.",
        "我是制造业客户，正在评估自动化方案。",
        "我是负责销售运营的团队成员。",
        "请联系销售团队处理这个请求。",
        "联系人是业务部门，不是个人。",
    ],
)
def test_preflight_does_not_treat_ordinary_english_or_chinese_prose_as_a_name(
    message,
):
    prepared = prepare_ai_context(
        purpose="lead_qualification",
        context=qualification_context(message),
        pii_handling="block",
        max_chars=30000,
        max_tokens=30000,
    )

    assert prepared.pii_findings == {}
    assert prepared.redaction_count == 0
    assert message in prepared.canonical_json


@pytest.mark.django_db
@override_settings(
    OPENAI_API_KEY="platform-key",
    AI_GATEWAY_MODEL_PRICING={
        "openai:gpt-5.6-luna": {
            "input_microusd_per_million_tokens": 1_000_000,
            "output_microusd_per_million_tokens": 2_000_000,
        }
    },
)
def test_gateway_redacts_before_transport_and_writes_metadata_only_audit(org_a):
    configuration = SDRIntelligenceSettings.objects.create(org=org_a, is_enabled=True)
    captured = {}

    class FakeClient:
        def qualify(self, **kwargs):
            captured.update(json.loads(kwargs["context"].canonical_json))
            return AIQualification(
                qualification=QualificationResult(82, QualificationBand.HIGH),
                response_id="provider-response-sensitive-id",
                input_tokens=100,
                output_tokens=25,
                provider="openai",
                model="gpt-5.6-luna",
            )

    item = candidate(
        "Name: Alice Smith, alice+canary@example.com, call +1 (202) 555-0198"
    )
    item = LeadCandidate(
        org_id=org_a.id,
        source=item.source,
        source_record_id=item.source_record_id,
        identity=item.identity,
        company=item.company,
        attributes=item.attributes,
    )
    with patch(
        "sdr.intelligence.gateway._build_provider_client",
        return_value=FakeClient(),
    ):
        result = ModelGateway.for_configuration(configuration).qualify(
            **gateway_kwargs(item)
        )

    serialized = json.dumps(captured)
    assert "Alice Smith" not in serialized
    assert "alice+canary@example.com" not in serialized
    assert "555-0198" not in serialized
    assert result.qualification.score == 82
    audit = SDRAICallAudit.objects.get(org=org_a)
    assert audit.status == "completed"
    assert audit.purpose == "lead_qualification"
    assert audit.prompt_version
    assert len(audit.configuration_sha256) == 64
    assert len(audit.input_sha256) == 64
    assert audit.pii_findings == {"email": 1, "name": 1, "phone": 1}
    assert audit.redaction_count == 3
    assert audit.estimated_cost_microusd == 150
    assert audit.response_id_sha256 != "provider-response-sensitive-id"
    assert "canary" not in json.dumps(audit.pii_findings)


@pytest.mark.django_db
@override_settings(OPENAI_API_KEY="platform-key")
def test_tenant_switch_blocks_before_client_creation_and_is_audited(org_a):
    configuration = SDRIntelligenceSettings.objects.create(org=org_a, is_enabled=False)
    item = candidate()
    item = LeadCandidate(
        org_id=org_a.id,
        source=item.source,
        source_record_id=item.source_record_id,
        identity=item.identity,
        company=item.company,
        attributes=item.attributes,
    )

    with patch("sdr.intelligence.gateway._build_provider_client") as factory:
        with pytest.raises(AISafetyError) as caught:
            ModelGateway.for_configuration(configuration).qualify(
                **gateway_kwargs(item)
            )

    assert caught.value.code == "ai_disabled"
    factory.assert_not_called()
    audit = SDRAICallAudit.objects.get(org=org_a)
    assert audit.status == "blocked"
    assert audit.failure_code == "ai_disabled"


@pytest.mark.django_db
def test_pii_block_policy_never_constructs_a_provider_client(org_a):
    configuration = SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        pii_handling="block",
    )
    raw_context = qualification_context(
        "Email alice+blocked@example.com or call +1 202 555 0198"
    )

    with patch("sdr.intelligence.gateway._build_provider_client") as factory:
        with pytest.raises(AISafetyError) as caught:
            UnifiedAIGateway(
                org_id=org_a.id,
                routes=(("openai", "gpt-5.6-luna", "low"),),
                configuration=configuration,
            ).execute(
                purpose="lead_qualification",
                prompt_version="lead-qualification-v-test",
                context=raw_context,
            )

    assert caught.value.code == "ai_pii_blocked"
    factory.assert_not_called()
    audit = SDRAICallAudit.objects.get(org=org_a)
    assert audit.status == "blocked"
    assert audit.failure_code == "ai_pii_blocked"
    assert "alice+blocked@example.com" not in json.dumps(
        {
            "failure_reason": audit.failure_reason,
            "pii_findings": audit.pii_findings,
            "field_paths": audit.field_paths,
        }
    )


@pytest.mark.django_db
def test_outbound_copy_context_never_contains_raw_prospect_notes(org_a):
    campaign = SDROutboundCampaign.objects.create(
        org=org_a,
        name="Campaign",
        channels=["email"],
    )
    SDROutboundProspect.objects.create(
        org=org_a,
        campaign=campaign,
        email="person@example.com",
        company_name="Acme",
        job_title="VP Operations",
        industry="Manufacturing",
        country="US",
        notes="CANARY_PRIVATE_NOTE alice@example.com +86 13800138000",
        dedupe_key="a" * 64,
    )
    draft = SDROutboundCopyDraft.objects.create(
        org=org_a,
        campaign=campaign,
        offering_summary="Workflow automation",
        value_proposition="Faster handoffs",
        cta_goal="Ask for a short call",
    )

    context = _draft_context(draft)

    assert "notes" not in json.dumps(context)
    assert "CANARY_PRIVATE_NOTE" not in json.dumps(context)
    assert context["audience"]["prospect_count"] == 1
    assert context["audience"]["job_titles"] == ["VP Operations"]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls")
def test_ai_audit_api_is_admin_only_and_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
    org_a,
):
    SDRAICallAudit.objects.create(
        org=org_a,
        purpose="lead_qualification",
        status="blocked",
        prompt_version="lead-v1",
        configuration_sha256="a" * 64,
        failure_code="ai_pii_blocked",
        retention_expires_at=timezone.now() + timedelta(days=90),
    )

    denied = user_client.get("/api/sdr/intelligence/ai-audits/")
    visible = admin_client.get("/api/sdr/intelligence/ai-audits/")
    isolated = org_b_client.get("/api/sdr/intelligence/ai-audits/")

    assert denied.status_code == 403
    assert visible.status_code == 200
    assert len(visible.json()) == 1
    assert visible.json()[0]["failure_code"] == "ai_pii_blocked"
    assert isolated.status_code == 200
    assert isolated.json() == []


@pytest.mark.django_db
def test_ai_audit_retention_recovers_abandoned_and_purges_expired_rows(org_a):
    common = {
        "org": org_a,
        "purpose": "lead_qualification",
        "prompt_version": "lead-v1",
        "configuration_sha256": "a" * 64,
    }
    expired = SDRAICallAudit.objects.create(
        **common,
        status="completed",
        retention_expires_at=timezone.now() - timedelta(minutes=1),
    )
    future = SDRAICallAudit.objects.create(
        **common,
        status="completed",
        retention_expires_at=timezone.now() + timedelta(days=1),
    )
    pending = SDRAICallAudit.objects.create(
        **common,
        status="pending",
        retention_expires_at=timezone.now() - timedelta(minutes=1),
    )
    abandoned = SDRAICallAudit.objects.create(
        **common,
        status="pending",
        retention_expires_at=timezone.now() + timedelta(days=1),
    )
    SDRAICallAudit.objects.filter(id=abandoned.id).update(
        created_at=timezone.now() - timedelta(hours=2)
    )

    result = purge_expired_ai_call_audits.run()

    assert result == {"deleted": 1, "abandoned": 1}
    assert not SDRAICallAudit.objects.filter(id=expired.id).exists()
    assert SDRAICallAudit.objects.filter(id=future.id).exists()
    assert SDRAICallAudit.objects.filter(id=pending.id).exists()
    abandoned.refresh_from_db()
    assert abandoned.status == "failed"
    assert abandoned.failure_code == "ai_attempt_abandoned"


_ADAPTER_MODULES = {
    "sdr.intelligence.openai_client",
    "sdr.intelligence.doubao_client",
    "sdr.intelligence.deepseek_client",
    "sdr.intelligence.outbound_copy_client",
}
_PROVIDER_SETTINGS = {
    "OPENAI_API_KEY",
    "OPENAI_API_BASE_URL",
    "DOUBAO_API_KEY",
    "DOUBAO_API_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_BASE_URL",
}
_PROVIDER_HOSTS = (
    "api.openai.com",
    "ark.cn-beijing.volces.com",
    "api.deepseek.com",
)


def _module_imports(node, current_module):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        package = current_module.split(".")[:-1]
        keep = max(0, len(package) - node.level + 1)
        prefix = package[:keep]
        if node.module:
            prefix.extend(node.module.split("."))
        base = ".".join(prefix)
    else:
        base = node.module or ""
    imports = [base] if base else []
    imports.extend(
        f"{base}.{alias.name}" if base else alias.name
        for alias in node.names
        if alias.name != "*"
    )
    return imports


def _joined_string_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _joined_string_literal(node.left)
        right = _joined_string_literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _model_boundary_violations(source, module_name):
    tree = ast.parse(source)
    violations = []
    gateway_module = "sdr.intelligence.gateway"
    safety_transport_modules = _ADAPTER_MODULES | {
        gateway_module,
        "sdr.intelligence.safety",
    }
    canary_module = "sdr.management.commands.verify_ai_gateway_canary"
    for node in ast.walk(tree):
        imported = _module_imports(node, module_name)
        if module_name != gateway_module and any(
            name in _ADAPTER_MODULES for name in imported
        ):
            violations.append("provider_adapter_import")
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if module_name != gateway_module and names & {
                "_build_provider_client",
                "_invoke_provider",
            }:
                violations.append("private_gateway_builder")
            if module_name not in safety_transport_modules and names & {
                "PreparedAIContext",
                "prepared_context_json",
            }:
                violations.append("prepared_context_transport")
            if (
                module_name
                not in {gateway_module, canary_module, "sdr.intelligence.safety"}
                and "prepare_ai_context" in names
            ):
                violations.append("direct_context_preflight")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
            and node.attr in _PROVIDER_SETTINGS
            and module_name != "sdr.intelligence.registry"
        ):
            violations.append("direct_provider_setting")
        literal = _joined_string_literal(node)
        if (
            literal
            and any(host in literal for host in _PROVIDER_HOSTS)
            and module_name
            not in {"crm.settings", "sdr.intelligence.registry", *_ADAPTER_MODULES}
        ):
            violations.append("provider_host_literal")
    return sorted(set(violations))


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            "from sdr.intelligence.openai_client import OpenAILeadQualifier",
            "provider_adapter_import",
        ),
        (
            "from sdr.intelligence import openai_client as model_client",
            "provider_adapter_import",
        ),
        (
            "from sdr.intelligence.gateway import _build_provider_client",
            "private_gateway_builder",
        ),
        ("secret = settings.OPENAI_API_KEY", "direct_provider_setting"),
        (
            'endpoint = "https://api." + "openai.com/v1/responses"',
            "provider_host_literal",
        ),
    ],
)
def test_model_boundary_detector_rejects_known_bypasses(source, expected):
    assert expected in _model_boundary_violations(source, "sdr.business")


def test_business_modules_cannot_bypass_the_model_gateway():
    root = Path(__file__).resolve().parents[2]
    skipped_parts = {
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "htmlcov",
        "static",
        "staticfiles",
        "media",
        "tests",
    }
    offenders = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if skipped_parts.intersection(relative.parts):
            continue
        module_name = ".".join(relative.with_suffix("").parts)
        violations = _model_boundary_violations(
            path.read_text(encoding="utf-8"), module_name
        )
        if violations:
            offenders[str(relative)] = violations
    assert offenders == {}
