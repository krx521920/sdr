from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from sdr.domain import QualificationBand, QualificationResult
from sdr.intelligence.contracts import AIQualification
from sdr.models import SDRAICallAudit, SDRIntelligenceSettings


@pytest.mark.django_db
@override_settings(OPENAI_API_KEY="platform-test-key")
def test_ai_canary_defaults_to_a_network_free_dry_run(org_a):
    SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        pii_handling="block",
    )
    stdout = StringIO()

    with patch("sdr.intelligence.gateway._build_provider_client") as factory:
        call_command(
            "verify_ai_gateway_canary",
            org_id=str(org_a.id),
            provider="openai",
            stdout=stdout,
        )

    assert factory.call_count == 0
    assert not SDRAICallAudit.objects.filter(org=org_a).exists()
    output = stdout.getvalue()
    assert "canary_status=dry_run_ready" in output
    assert "external_requests=0" in output
    assert "platform-test-key" not in output


@pytest.mark.django_db
@override_settings(OPENAI_API_KEY="platform-test-key")
def test_ai_canary_real_confirmation_writes_one_safe_audit(org_a):
    SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        pii_handling="block",
    )
    stdout = StringIO()

    class FakeClient:
        def qualify(self, **kwargs):
            serialized = kwargs["context"].canonical_json
            assert "@" not in serialized
            assert "+86" not in serialized
            return AIQualification(
                qualification=QualificationResult(75, QualificationBand.HIGH),
                response_id="synthetic-provider-response",
                input_tokens=120,
                output_tokens=30,
                provider="openai",
                model="gpt-5.6-luna",
            )

    with patch(
        "sdr.intelligence.gateway._build_provider_client",
        return_value=FakeClient(),
    ):
        call_command(
            "verify_ai_gateway_canary",
            org_id=str(org_a.id),
            provider="openai",
            confirm_real_provider_call=True,
            stdout=stdout,
        )

    audit = SDRAICallAudit.objects.get(org=org_a)
    assert audit.status == "completed"
    assert audit.pii_findings == {}
    assert audit.redaction_count == 0
    assert audit.input_tokens == 120
    output = stdout.getvalue()
    assert "canary_status=succeeded" in output
    assert "external_requests=1" in output
    assert "synthetic-provider-response" not in output
    assert "platform-test-key" not in output


@pytest.mark.django_db
def test_ai_canary_blocks_when_no_provider_credential_exists(org_a):
    SDRIntelligenceSettings.objects.create(
        org=org_a,
        is_enabled=True,
        pii_handling="block",
    )

    with pytest.raises(CommandError, match="provider_credential_missing"):
        call_command(
            "verify_ai_gateway_canary",
            org_id=str(org_a.id),
            provider="openai",
        )
