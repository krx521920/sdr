"""DeepSeek Chat Completions adapter for structured lead qualification."""

from __future__ import annotations

import json
from uuid import UUID

import requests

from sdr.intelligence.contracts import (
    AIQualification,
    ModelProviderError,
    json_schema_prompt,
    optional_nonnegative_int,
    qualification_instructions,
    validate_result,
)
from sdr.intelligence.safety import PreparedAIContext, prepared_context_json


class DeepSeekQualificationError(ModelProviderError):
    pass


class DeepSeekLeadQualifier:
    provider = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: str,
        base_url: str,
        timeout_seconds: int = 30,
        session=None,
    ):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.reasoning_effort = reasoning_effort
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def qualify(
        self,
        *,
        org_id: UUID,
        context: PreparedAIContext,
    ) -> AIQualification:
        if not self.api_key:
            raise DeepSeekQualificationError(
                "DeepSeek API key is not configured.",
                code="deepseek_not_configured",
            )
        serialized_context = prepared_context_json(
            context, expected_purpose="lead_qualification"
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{qualification_instructions()} {json_schema_prompt()}",
                },
                {
                    "role": "user",
                    "content": serialized_context,
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {
                "type": "disabled" if self.reasoning_effort == "none" else "enabled"
            },
            "stream": False,
            "max_tokens": 1200,
        }
        if self.reasoning_effort != "none":
            payload["reasoning_effort"] = self.reasoning_effort
        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise DeepSeekQualificationError(
                "DeepSeek qualification request failed.",
                code="deepseek_request_failed",
                retryable=True,
            ) from exc
        if 300 <= response.status_code < 400:
            raise DeepSeekQualificationError(
                "DeepSeek qualification refused an HTTP redirect.",
                code="deepseek_redirect_blocked",
            )
        if response.status_code >= 400:
            raise DeepSeekQualificationError(
                f"DeepSeek qualification returned HTTP {response.status_code}.",
                code="deepseek_http_error",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            qualification = validate_result(
                result,
                provider=self.provider,
                model=self.model,
            )
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise DeepSeekQualificationError(
                "DeepSeek qualification returned an invalid structured result.",
                code="deepseek_invalid_response",
            ) from exc
        usage = body.get("usage") or {}
        return AIQualification(
            qualification=qualification,
            response_id=str(body.get("id", ""))[:255],
            input_tokens=optional_nonnegative_int(
                usage.get("prompt_tokens", usage.get("input_tokens"))
            ),
            output_tokens=optional_nonnegative_int(
                usage.get("completion_tokens", usage.get("output_tokens"))
            ),
            provider=self.provider,
            model=self.model,
        )
