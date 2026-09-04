"""Deployment-owned provider registry for the SDR model gateway."""

from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    provider: str
    label: str
    protocol: str
    base_url: str
    api_key: str
    models: tuple[str, ...]
    timeout_seconds: int


def provider_registry() -> dict[str, ProviderDefinition]:
    registry = {
        "openai": ProviderDefinition(
            provider="openai",
            label="OpenAI",
            protocol="responses",
            base_url=settings.OPENAI_API_BASE_URL,
            api_key=settings.OPENAI_API_KEY,
            models=tuple(settings.OPENAI_ALLOWED_MODELS),
            timeout_seconds=settings.OPENAI_API_TIMEOUT_SECONDS,
        ),
        "doubao": ProviderDefinition(
            provider="doubao",
            label="Doubao / Volcengine Ark",
            protocol="responses",
            base_url=settings.DOUBAO_API_BASE_URL,
            api_key=settings.DOUBAO_API_KEY,
            models=tuple(settings.DOUBAO_ALLOWED_MODELS),
            timeout_seconds=settings.DOUBAO_API_TIMEOUT_SECONDS,
        ),
        "deepseek": ProviderDefinition(
            provider="deepseek",
            label="DeepSeek",
            protocol="chat_completions",
            base_url=settings.DEEPSEEK_API_BASE_URL,
            api_key=settings.DEEPSEEK_API_KEY,
            models=tuple(settings.DEEPSEEK_ALLOWED_MODELS),
            timeout_seconds=settings.DEEPSEEK_API_TIMEOUT_SECONDS,
        ),
    }
    return {
        provider: definition
        for provider, definition in registry.items()
        if provider in settings.AI_GATEWAY_ALLOWED_PROVIDERS
    }


def provider_catalog() -> dict[str, dict[str, object]]:
    return {
        provider: {
            "label": definition.label,
            "protocol": definition.protocol,
            "models": list(definition.models),
            "platform_configured": bool(definition.api_key),
        }
        for provider, definition in provider_registry().items()
    }
