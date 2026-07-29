"""Tenant-aware model routing with provider failover and safe credentials."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from django.conf import settings

from common.secrets import SecretDecryptionError
from sdr.intelligence.contracts import AIQualification, ModelProviderError
from sdr.intelligence.deepseek_client import DeepSeekLeadQualifier
from sdr.intelligence.doubao_client import DoubaoLeadQualifier
from sdr.intelligence.openai_client import OpenAILeadQualifier
from sdr.intelligence.registry import ProviderDefinition, provider_registry
from sdr.models import SDRModelCredential


class ModelGatewayError(ModelProviderError):
    def __init__(self, attempts: list[dict[str, Any]]):
        last = attempts[-1] if attempts else {}
        super().__init__(
            "Every configured model provider failed.",
            code=str(last.get("error_code") or "model_gateway_failed"),
            retryable=any(bool(item.get("retryable")) for item in attempts),
        )
        self.attempts = tuple(attempts)


class ModelGateway:
    def __init__(self, *, org_id, routes, client_factory=None):
        self.org_id = org_id
        self.routes = routes
        self.client_factory = client_factory or self._build_client

    @classmethod
    def for_configuration(cls, configuration, *, client_factory=None):
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
        return cls(
            org_id=configuration.org_id,
            routes=tuple(routes),
            client_factory=client_factory,
        )

    def qualify(self, **kwargs) -> AIQualification:
        attempts: list[dict[str, Any]] = []
        registry = provider_registry()
        for index, (provider, model, reasoning_effort) in enumerate(self.routes):
            definition = registry.get(provider)
            if definition is None or model not in definition.models:
                attempts.append(
                    {
                        "provider": provider,
                        "model": model,
                        "status": "failed",
                        "error_code": "model_route_not_allowed",
                        "retryable": False,
                    }
                )
                continue
            try:
                api_key, credential_source = self._resolve_api_key(definition)
                client = self.client_factory(
                    definition=definition,
                    api_key=api_key,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                result = client.qualify(**kwargs)
            except ModelProviderError as exc:
                attempts.append(
                    {
                        "provider": provider,
                        "model": model,
                        "status": "failed",
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                    }
                )
                continue
            attempts.append(
                {
                    "provider": provider,
                    "model": model,
                    "status": "completed",
                    "credential_source": credential_source,
                }
            )
            return replace(
                result,
                attempts=tuple(attempts),
                gateway_fallback_used=index > 0,
            )
        raise ModelGatewayError(attempts)

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

    @staticmethod
    def _build_client(
        *,
        definition: ProviderDefinition,
        api_key: str,
        model: str,
        reasoning_effort: str,
    ):
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
