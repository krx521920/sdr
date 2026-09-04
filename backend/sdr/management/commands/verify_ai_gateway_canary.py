"""Run a deliberately synthetic, single-provider AI gateway canary."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from automation.tenant_context import database_org_context
from common.models import Org
from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.intelligence.contracts import (
    PROMPT_VERSION,
    ModelProviderError,
    build_lead_context,
)
from sdr.intelligence.gateway import UnifiedAIGateway
from sdr.intelligence.registry import provider_registry
from sdr.intelligence.safety import prepare_ai_context
from sdr.models import SDRAICallAudit, SDRIntelligenceSettings, SDRModelCredential

CONFIRMATION_OPTION = "--confirm-real-provider-call"
CANARY_PURPOSE = "lead_qualification"
CANARY_MARKER = "SDR_AI_CANARY_PUBLIC_SYNTHETIC_V1"


class Command(BaseCommand):
    help = (
        "Validate one AI gateway route with synthetic, non-PII data. The default "
        "is a local dry-run; a real provider call requires explicit confirmation."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--org-id",
            type=uuid.UUID,
            required=True,
            help="Dedicated test organization UUID.",
        )
        parser.add_argument(
            "--provider",
            choices=("openai", "doubao", "deepseek"),
            required=True,
        )
        parser.add_argument(
            "--model",
            default="",
            help="Allowed deployment model; defaults to the tenant model or first allowed model.",
        )
        parser.add_argument(
            CONFIRMATION_OPTION,
            action="store_true",
            dest="confirm_real_provider_call",
            help="Explicitly authorize exactly one external model request.",
        )

    def handle(self, *args, **options):
        org_id = uuid.UUID(str(options["org_id"]))
        provider = options["provider"]
        org = Org.objects.filter(id=org_id, is_active=True).first()
        if org is None:
            raise CommandError("canary_status=blocked code=test_org_not_found")

        registry = provider_registry()
        definition = registry.get(provider)
        if definition is None:
            raise CommandError(
                "canary_status=blocked code=provider_not_deployment_allowed"
            )

        with database_org_context(org_id):
            configuration = SDRIntelligenceSettings.objects.filter(
                org_id=org_id
            ).first()
            if configuration is None:
                raise CommandError("canary_status=blocked code=tenant_policy_missing")
            self._validate_tenant_policy(configuration, provider)
            model = self._select_model(
                configuration,
                definition.models,
                provider=provider,
                requested=options["model"],
            )
            reasoning_effort = self._select_reasoning_effort(
                configuration,
                provider=provider,
            )
            tenant_credential = bool(
                settings.AI_GATEWAY_ALLOW_TENANT_KEYS
                and SDRModelCredential.objects.filter(
                    org_id=org_id,
                    provider=provider,
                    is_active=True,
                ).exists()
            )

        credential_configured = tenant_credential or bool(definition.api_key)
        if not credential_configured:
            raise CommandError("canary_status=blocked code=provider_credential_missing")

        context = self._synthetic_context(org_id)
        prepared = prepare_ai_context(
            purpose=CANARY_PURPOSE,
            context=context,
            pii_handling=configuration.pii_handling,
            max_chars=configuration.max_ai_input_chars,
            max_tokens=configuration.max_ai_input_tokens,
        )
        if prepared.pii_findings or prepared.redaction_count:
            raise CommandError("canary_status=blocked code=synthetic_input_not_clean")

        self.stdout.write(
            "canary_preflight=passed "
            f"provider={provider} model={model} purpose={CANARY_PURPOSE} "
            f"input_chars={prepared.input_chars} "
            f"estimated_input_tokens={prepared.estimated_input_tokens}"
        )
        if not options["confirm_real_provider_call"]:
            self.stdout.write(
                self.style.SUCCESS(
                    "canary_status=dry_run_ready external_requests=0 "
                    f"confirmation_required={CONFIRMATION_OPTION}"
                )
            )
            return

        try:
            with database_org_context(org_id):
                execution = UnifiedAIGateway(
                    org_id=org_id,
                    routes=((provider, model, reasoning_effort),),
                    configuration=configuration,
                ).execute(
                    purpose=CANARY_PURPOSE,
                    prompt_version=PROMPT_VERSION,
                    context=context,
                )
        except ModelProviderError as exc:
            raise CommandError(
                f"canary_status=failed code={exc.code} external_requests_at_most=1"
            ) from None

        if execution.fallback_used or len(execution.attempts) != 1:
            raise CommandError("canary_status=failed code=unexpected_route_count")

        with database_org_context(org_id):
            audits = list(
                SDRAICallAudit.objects.filter(
                    org_id=org_id,
                    purpose=CANARY_PURPOSE,
                    provider=provider,
                    model=model,
                    input_sha256=prepared.input_sha256,
                ).order_by("-created_at")[:2]
            )
        if len(audits) != 1:
            raise CommandError("canary_status=failed code=audit_cardinality_invalid")
        audit = audits[0]
        if (
            audit.status != "completed"
            or audit.prompt_version != PROMPT_VERSION
            or audit.pii_findings
            or audit.redaction_count
            or len(audit.configuration_sha256) != 64
            or len(audit.input_sha256) != 64
            or len(audit.response_id_sha256) != 64
            or audit.failure_code
        ):
            raise CommandError("canary_status=failed code=audit_validation_failed")

        self.stdout.write(
            self.style.SUCCESS(
                "canary_status=succeeded external_requests=1 "
                f"request_id={audit.request_id} provider={audit.provider} "
                f"model={audit.model} purpose={audit.purpose} "
                f"prompt_version={audit.prompt_version} "
                f"input_tokens={audit.input_tokens if audit.input_tokens is not None else 'unknown'} "
                f"output_tokens={audit.output_tokens if audit.output_tokens is not None else 'unknown'} "
                f"latency_ms={audit.latency_ms if audit.latency_ms is not None else 'unknown'} "
                f"estimated_cost_microusd={audit.estimated_cost_microusd if audit.estimated_cost_microusd is not None else 'unknown'}"
            )
        )

    @staticmethod
    def _validate_tenant_policy(configuration, provider):
        if not configuration.is_enabled or not configuration.ai_scoring_enabled:
            raise CommandError("canary_status=blocked code=tenant_ai_disabled")
        if configuration.pii_handling != "block":
            raise CommandError("canary_status=blocked code=pii_policy_must_block")
        if CANARY_PURPOSE not in configuration.allowed_ai_purposes:
            raise CommandError("canary_status=blocked code=purpose_not_tenant_allowed")
        if provider not in configuration.allowed_ai_providers:
            raise CommandError("canary_status=blocked code=provider_not_tenant_allowed")

    @staticmethod
    def _select_model(configuration, allowed_models, *, provider, requested):
        model = requested.strip()
        if not model and configuration.provider == provider:
            model = configuration.model
        if not model:
            model = allowed_models[0] if allowed_models else ""
        if model not in allowed_models:
            raise CommandError(
                "canary_status=blocked code=model_not_deployment_allowed"
            )
        return model

    @staticmethod
    def _select_reasoning_effort(configuration, *, provider):
        if configuration.provider == provider:
            effort = configuration.reasoning_effort
        elif configuration.fallback_provider == provider:
            effort = configuration.fallback_reasoning_effort
        else:
            allowed = settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS
            effort = "low" if "low" in allowed else (allowed[0] if allowed else "")
        if effort not in settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS:
            raise CommandError(
                "canary_status=blocked code=reasoning_effort_not_deployment_allowed"
            )
        return effort

    @staticmethod
    def _synthetic_context(org_id):
        nonce = uuid.uuid4().hex
        candidate = LeadCandidate(
            org_id=org_id,
            source=LeadSource.MANUAL,
            source_record_id=f"ai-canary-{nonce}",
            identity=LeadIdentity(),
            company=CompanySnapshot(
                name="Synthetic Workflow Systems",
                industry="B2B software",
                country="Test region",
            ),
            attributes={
                "job_title": "Operations role",
                "is_business_email": False,
                "message": (
                    f"{CANARY_MARKER} nonce={nonce}. Evaluate a fictional "
                    "workflow automation use case."
                ),
            },
        )
        return build_lead_context(
            candidate=candidate,
            baseline=QualificationResult(
                50,
                QualificationBand.MEDIUM,
                reasons=("synthetic canary fixture",),
            ),
            research=None,
            icp_description="Synthetic B2B workflow evaluation only.",
            positive_signals="Fictional operations workflow need.",
            negative_signals="No real person or company data.",
        )
