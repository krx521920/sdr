"""The only tenant-aware, policy-enforcing route to external AI providers."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from common.secrets import SecretDecryptionError
from sdr.intelligence.contracts import (
    PROMPT_VERSION,
    AIQualification,
    ModelProviderError,
    build_lead_context,
)
from sdr.intelligence.deepseek_client import DeepSeekLeadQualifier
from sdr.intelligence.doubao_client import DoubaoLeadQualifier
from sdr.intelligence.openai_client import OpenAILeadQualifier
from sdr.intelligence.outbound_copy_client import OutboundCopyClient
from sdr.intelligence.registry import ProviderDefinition, provider_registry
from sdr.intelligence.safety import (
    AISafetyError,
    PreparedAIContext,
    configuration_fingerprint,
    prepare_ai_context,
    response_identifier_hash,
)
from sdr.models import (
    SDRAICallAudit,
    SDRAICallStatus,
    SDRIntelligenceSettings,
    SDRModelCredential,
)


class ModelGatewayError(ModelProviderError):
    def __init__(self, attempts: list[dict[str, Any]]):
        last = attempts[-1] if attempts else {}
        super().__init__(
            "Every configured model provider failed.",
            code=str(last.get("error_code") or "model_gateway_failed"),
            retryable=any(bool(item.get("retryable")) for item in attempts),
        )
        self.attempts = tuple(attempts)


@dataclass(frozen=True, slots=True)
class GatewayExecution:
    result: Any
    attempts: tuple[dict[str, Any], ...]
    fallback_used: bool


class UnifiedAIGateway:
    """Preflight once, reserve audit rows, then call only allow-listed routes."""

    def __init__(
        self,
        *,
        org_id,
        routes,
        configuration: SDRIntelligenceSettings | None = None,
    ):
        self.org_id = org_id
        self.routes = tuple(routes)
        self.configuration = configuration

    def execute(
        self,
        *,
        purpose: str,
        prompt_version: str,
        context: dict[str, Any],
    ) -> GatewayExecution:
        configuration = self._configuration()
        request_id = uuid.uuid4()
        config_hash = self._preflight_configuration(
            configuration,
            purpose=purpose,
            prompt_version=prompt_version,
            request_id=request_id,
        )
        try:
            prepared = prepare_ai_context(
                purpose=purpose,
                context=context,
                pii_handling=configuration.pii_handling,
                max_chars=configuration.max_ai_input_chars,
                max_tokens=configuration.max_ai_input_tokens,
            )
        except AISafetyError as exc:
            self._record_blocked(
                configuration,
                request_id=request_id,
                purpose=purpose,
                prompt_version=prompt_version,
                configuration_sha256=config_hash,
                code=exc.code,
                reason=str(exc),
            )
            raise

        registry = provider_registry()
        attempts: list[dict[str, Any]] = []
        for index, (provider, model, reasoning_effort) in enumerate(self.routes):
            definition = registry.get(provider)
            if (
                provider not in configuration.allowed_ai_providers
                or definition is None
                or model not in definition.models
                or reasoning_effort not in settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS
            ):
                error = AISafetyError(
                    "The configured AI route is not allowed.",
                    code="model_route_not_allowed",
                )
                self._record_blocked(
                    configuration,
                    request_id=request_id,
                    purpose=purpose,
                    prompt_version=prompt_version,
                    configuration_sha256=config_hash,
                    code=error.code,
                    reason=str(error),
                    provider=str(provider),
                    model=str(model),
                    route_index=index,
                    prepared=prepared,
                )
                raise error

            try:
                api_key, credential_source = self._resolve_api_key(definition)
            except ModelProviderError as exc:
                audit = self._reserve_audit(
                    configuration,
                    request_id=request_id,
                    purpose=purpose,
                    prompt_version=prompt_version,
                    configuration_sha256=config_hash,
                    provider=provider,
                    model=model,
                    credential_source="unknown",
                    route_index=index,
                    prepared=prepared,
                )
                self._finish_failure(audit, exc, latency_ms=0)
                attempts.append(self._attempt(provider, model, exc))
                continue

            audit = self._reserve_audit(
                configuration,
                request_id=request_id,
                purpose=purpose,
                prompt_version=prompt_version,
                configuration_sha256=config_hash,
                provider=provider,
                model=model,
                credential_source=credential_source,
                route_index=index,
                prepared=prepared,
            )
            started = time.monotonic()
            try:
                client = _build_provider_client(
                    purpose=purpose,
                    definition=definition,
                    api_key=api_key,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                result = _invoke_provider(
                    purpose=purpose,
                    client=client,
                    org_id=self.org_id,
                    context=prepared,
                )
            except ModelProviderError as exc:
                self._finish_failure(audit, exc, latency_ms=_elapsed_ms(started))
                attempts.append(self._attempt(provider, model, exc))
                continue
            except Exception:
                safe_error = ModelProviderError(
                    "The model provider failed unexpectedly.",
                    code="model_provider_internal_error",
                    retryable=False,
                )
                self._finish_failure(audit, safe_error, latency_ms=_elapsed_ms(started))
                attempts.append(self._attempt(provider, model, safe_error))
                continue

            self._finish_success(
                audit,
                result,
                provider=provider,
                model=model,
                fallback_used=index > 0,
                latency_ms=_elapsed_ms(started),
            )
            attempts.append(
                {
                    "provider": provider,
                    "model": model,
                    "status": "completed",
                    "credential_source": credential_source,
                }
            )
            return GatewayExecution(
                result=result,
                attempts=tuple(attempts),
                fallback_used=index > 0,
            )
        raise ModelGatewayError(attempts)

    def _configuration(self) -> SDRIntelligenceSettings:
        configuration = self.configuration
        if configuration is None:
            configuration = SDRIntelligenceSettings.objects.filter(
                org_id=self.org_id
            ).first()
        if configuration is None:
            raise AISafetyError(
                "The tenant AI policy is missing.", code="ai_policy_missing"
            )
        return configuration

    def _preflight_configuration(
        self,
        configuration,
        *,
        purpose,
        prompt_version,
        request_id,
    ) -> str:
        try:
            config_hash = configuration_fingerprint(configuration)
        except Exception as exc:
            raise AISafetyError(
                "The tenant AI policy cannot be verified.",
                code="ai_policy_invalid",
            ) from exc
        error = None
        if configuration.org_id != self.org_id:
            error = AISafetyError(
                "The AI policy belongs to another tenant.",
                code="ai_policy_org_mismatch",
            )
        elif not configuration.is_enabled:
            error = AISafetyError("Tenant AI is disabled.", code="ai_disabled")
        elif not _valid_string_list(configuration.allowed_ai_purposes):
            error = AISafetyError(
                "The tenant purpose policy is invalid.", code="ai_policy_invalid"
            )
        elif purpose not in configuration.allowed_ai_purposes:
            error = AISafetyError(
                "This AI purpose is disabled for the tenant.",
                code="ai_purpose_disabled",
            )
        elif not prompt_version or len(prompt_version) > 100:
            error = AISafetyError(
                "A valid prompt version is required.",
                code="ai_prompt_version_invalid",
            )
        elif not _valid_string_list(
            configuration.allowed_ai_providers, allow_empty=False
        ):
            error = AISafetyError(
                "The tenant provider policy is invalid.", code="ai_policy_invalid"
            )
        elif (
            not isinstance(configuration.max_ai_input_chars, int)
            or not 1000 <= configuration.max_ai_input_chars <= 200000
            or not isinstance(configuration.max_ai_input_tokens, int)
            or not 256 <= configuration.max_ai_input_tokens <= 100000
            or not isinstance(configuration.ai_audit_retention_days, int)
            or not 1 <= configuration.ai_audit_retention_days <= 3650
        ):
            error = AISafetyError(
                "The tenant AI limits are invalid.", code="ai_policy_invalid"
            )
        if error is not None:
            self._record_blocked(
                configuration,
                request_id=request_id,
                purpose=purpose,
                prompt_version=prompt_version or "invalid",
                configuration_sha256=config_hash,
                code=error.code,
                reason=str(error),
            )
            raise error
        return config_hash

    def _resolve_api_key(self, definition: ProviderDefinition) -> tuple[str, str]:
        credential = None
        if settings.AI_GATEWAY_ALLOW_TENANT_KEYS:
            credential = SDRModelCredential.objects.filter(
                org_id=self.org_id,
                provider=definition.provider,
                is_active=True,
            ).first()
        if credential:
            try:
                return credential.get_api_key(), "tenant"
            except SecretDecryptionError as exc:
                raise ModelProviderError(
                    "The tenant model credential cannot be decrypted.",
                    code=f"{definition.provider}_credential_invalid",
                ) from exc
        return definition.api_key, "platform"

    def _record_blocked(
        self,
        configuration,
        *,
        request_id,
        purpose,
        prompt_version,
        configuration_sha256,
        code,
        reason,
        provider="",
        model="",
        route_index=0,
        prepared: PreparedAIContext | None = None,
    ) -> None:
        try:
            SDRAICallAudit.objects.create(
                org_id=self.org_id,
                request_id=request_id,
                purpose=purpose[:64],
                status=SDRAICallStatus.BLOCKED,
                provider=provider[:24],
                model=model[:100],
                route_index=route_index,
                prompt_version=prompt_version[:100],
                configuration_sha256=configuration_sha256,
                input_sha256=prepared.input_sha256 if prepared else "",
                field_paths=list(prepared.field_paths) if prepared else [],
                pii_findings=prepared.pii_findings if prepared else {},
                redaction_count=prepared.redaction_count if prepared else 0,
                input_chars=prepared.input_chars if prepared else 0,
                estimated_input_tokens=(
                    prepared.estimated_input_tokens if prepared else 0
                ),
                failure_code=code[:100],
                failure_reason=_safe_failure_reason(reason),
                retention_expires_at=timezone.now()
                + timedelta(days=configuration.ai_audit_retention_days),
            )
        except Exception as exc:
            raise AISafetyError(
                "AI audit storage is unavailable.", code="ai_audit_unavailable"
            ) from exc

    def _reserve_audit(
        self,
        configuration,
        *,
        request_id,
        purpose,
        prompt_version,
        configuration_sha256,
        provider,
        model,
        credential_source,
        route_index,
        prepared,
    ):
        try:
            return SDRAICallAudit.objects.create(
                org_id=self.org_id,
                request_id=request_id,
                purpose=purpose,
                status=SDRAICallStatus.PENDING,
                provider=provider,
                model=model,
                credential_source=credential_source,
                route_index=route_index,
                prompt_version=prompt_version,
                configuration_sha256=configuration_sha256,
                input_sha256=prepared.input_sha256,
                field_paths=list(prepared.field_paths),
                pii_findings=prepared.pii_findings,
                redaction_count=prepared.redaction_count,
                input_chars=prepared.input_chars,
                estimated_input_tokens=prepared.estimated_input_tokens,
                fallback_used=route_index > 0,
                retention_expires_at=timezone.now()
                + timedelta(days=configuration.ai_audit_retention_days),
            )
        except Exception as exc:
            raise AISafetyError(
                "AI audit storage is unavailable.", code="ai_audit_unavailable"
            ) from exc

    @staticmethod
    def _finish_failure(audit, exc, *, latency_ms):
        audit.status = SDRAICallStatus.FAILED
        audit.failure_code = exc.code[:100]
        audit.failure_reason = _safe_failure_reason(str(exc))
        audit.latency_ms = latency_ms
        _save_audit(audit)

    @staticmethod
    def _finish_success(
        audit,
        result,
        *,
        provider,
        model,
        fallback_used,
        latency_ms,
    ):
        audit.status = SDRAICallStatus.COMPLETED
        audit.input_tokens = getattr(result, "input_tokens", None)
        audit.output_tokens = getattr(result, "output_tokens", None)
        audit.estimated_cost_microusd = _estimated_cost_microusd(
            provider,
            model,
            audit.input_tokens,
            audit.output_tokens,
        )
        audit.latency_ms = latency_ms
        audit.response_id_sha256 = response_identifier_hash(
            str(getattr(result, "response_id", ""))
        )
        audit.fallback_used = fallback_used
        _save_audit(audit)

    @staticmethod
    def _attempt(provider, model, exc):
        return {
            "provider": provider,
            "model": model,
            "status": "failed",
            "error_code": exc.code,
            "retryable": exc.retryable,
        }


class ModelGateway:
    """Compatibility facade for the lead-qualification use case."""

    def __init__(
        self,
        *,
        org_id,
        routes,
        configuration: SDRIntelligenceSettings | None = None,
    ):
        self.org_id = org_id
        self.routes = tuple(routes)
        self.configuration = configuration

    @classmethod
    def for_configuration(cls, configuration):
        return cls(
            org_id=configuration.org_id,
            routes=_configuration_routes(configuration),
            configuration=configuration,
        )

    def qualify(self, **kwargs) -> AIQualification:
        context = build_lead_context(
            candidate=kwargs["candidate"],
            baseline=kwargs["baseline"],
            research=kwargs.get("research"),
            icp_description=kwargs.get("icp_description", ""),
            positive_signals=kwargs.get("positive_signals", ""),
            negative_signals=kwargs.get("negative_signals", ""),
            sales_feedback_calibration=kwargs.get("sales_feedback_calibration"),
        )
        execution = UnifiedAIGateway(
            org_id=self.org_id,
            routes=self.routes,
            configuration=self.configuration,
        ).execute(
            purpose="lead_qualification",
            prompt_version=PROMPT_VERSION,
            context=context,
        )
        return replace(
            execution.result,
            attempts=execution.attempts,
            gateway_fallback_used=execution.fallback_used,
        )


def _configuration_routes(configuration):
    routes = [
        (
            configuration.provider,
            configuration.model,
            configuration.reasoning_effort,
        )
    ]
    fallback = (
        configuration.fallback_provider,
        configuration.fallback_model,
        configuration.fallback_reasoning_effort,
    )
    if fallback[0] and fallback[1] and fallback != routes[0]:
        routes.append(fallback)
    return tuple(routes)


def _build_provider_client(
    *,
    purpose: str,
    definition: ProviderDefinition,
    api_key: str,
    model: str,
    reasoning_effort: str,
):
    """Private adapter boundary reached only after tenant policy and audit reserve."""

    if purpose == "lead_qualification":
        client_class = {
            "openai": OpenAILeadQualifier,
            "doubao": DoubaoLeadQualifier,
            "deepseek": DeepSeekLeadQualifier,
        }[definition.provider]
        return client_class(
            api_key=api_key,
            model=model,
            reasoning_effort=reasoning_effort,
            base_url=definition.base_url,
            timeout_seconds=definition.timeout_seconds,
        )
    elif purpose == "outbound_copy":
        return OutboundCopyClient(
            definition=definition,
            api_key=api_key,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    else:
        raise AISafetyError(
            "No provider adapter is registered for this AI purpose.",
            code="ai_purpose_adapter_missing",
        )


def _invoke_provider(*, purpose: str, client, org_id, context: PreparedAIContext):
    if purpose == "lead_qualification":
        return client.qualify(org_id=org_id, context=context)
    if purpose == "outbound_copy":
        return client.generate(org_id=org_id, context=context)
    raise AISafetyError(
        "No provider invocation is registered for this AI purpose.",
        code="ai_purpose_adapter_missing",
    )


def _elapsed_ms(started: float) -> int:
    return max(0, min(2_147_483_647, round((time.monotonic() - started) * 1000)))


def _valid_string_list(value, *, allow_empty=True) -> bool:
    return bool(
        (allow_empty or value)
        and isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and len(set(value)) == len(value)
    )


def _safe_failure_reason(value: str) -> str:
    cleaned = " ".join(str(value).split())
    lowered = cleaned.lower()
    if any(token in lowered for token in ("api key", "password", "bearer ", "secret=")):
        return "AI request failed; sensitive details were suppressed."
    return cleaned[:500]


def _estimated_cost_microusd(provider, model, input_tokens, output_tokens):
    pricing = getattr(settings, "AI_GATEWAY_MODEL_PRICING", {})
    if not isinstance(pricing, dict):
        return None
    rate = pricing.get(f"{provider}:{model}")
    if not isinstance(rate, dict) or input_tokens is None or output_tokens is None:
        return None
    try:
        input_rate = int(rate["input_microusd_per_million_tokens"])
        output_rate = int(rate["output_microusd_per_million_tokens"])
        total = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        return max(0, round(total))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _save_audit(audit) -> None:
    try:
        audit.save()
    except Exception as exc:
        raise AISafetyError(
            "AI audit storage is unavailable.", code="ai_audit_unavailable"
        ) from exc
