"""Internal provider adapter for outbound-copy generation."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import requests

from sdr.intelligence.contracts import optional_nonnegative_int, response_output_text
from sdr.intelligence.outbound_copy_contracts import (
    OUTBOUND_COPY_SCHEMA,
    OutboundCopyProviderError,
    OutboundCopyResult,
    copy_instructions,
    validate_generated_steps,
)
from sdr.intelligence.registry import ProviderDefinition
from sdr.intelligence.safety import PreparedAIContext, prepared_context_json


class OutboundCopyClient:
    """Provider HTTP adapter; callers must supply a gateway-sanitized context."""

    def __init__(
        self,
        *,
        definition: ProviderDefinition,
        api_key: str,
        model: str,
        reasoning_effort: str,
        session=None,
    ):
        self.definition = definition
        self.api_key = api_key.strip()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.session = session or requests.Session()

    def generate(
        self,
        *,
        org_id: UUID,
        context: PreparedAIContext,
    ) -> OutboundCopyResult:
        provider = self.definition.provider
        if not self.api_key:
            raise OutboundCopyProviderError(
                f"{provider} API key is not configured.",
                code=f"{provider}_not_configured",
            )
        instructions = copy_instructions()
        serialized = prepared_context_json(context, expected_purpose="outbound_copy")
        safe_context = json.loads(serialized)
        if self.definition.protocol == "chat_completions":
            url = f"{self.definition.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"{instructions} Required JSON Schema: "
                            f"{json.dumps(OUTBOUND_COPY_SCHEMA, ensure_ascii=False)}"
                        ),
                    },
                    {"role": "user", "content": serialized},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
                "max_tokens": 6000,
            }
            if self.reasoning_effort != "none":
                payload["reasoning_effort"] = self.reasoning_effort
        else:
            url = f"{self.definition.base_url}/responses"
            payload = {
                "model": self.model,
                "instructions": instructions,
                "input": serialized,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "outbound_copy",
                        "strict": True,
                        "schema": OUTBOUND_COPY_SCHEMA,
                    }
                },
                "max_output_tokens": 6000,
                "store": False,
            }
            if provider == "openai":
                payload["reasoning"] = {"effort": self.reasoning_effort}
                payload["safety_identifier"] = hashlib.sha256(
                    f"sdr-org:{org_id}".encode()
                ).hexdigest()
            else:
                payload["thinking"] = {
                    "type": "disabled" if self.reasoning_effort == "none" else "enabled"
                }
        try:
            response = self.session.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.definition.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise OutboundCopyProviderError(
                f"{provider} outbound copy request failed.",
                code=f"{provider}_copy_request_failed",
                retryable=True,
            ) from exc
        if 300 <= response.status_code < 400:
            raise OutboundCopyProviderError(
                f"{provider} outbound copy refused an HTTP redirect.",
                code=f"{provider}_copy_redirect_blocked",
            )
        if response.status_code >= 400:
            raise OutboundCopyProviderError(
                f"{provider} outbound copy returned HTTP {response.status_code}.",
                code=f"{provider}_copy_http_error",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            body = response.json()
            if self.definition.protocol == "chat_completions":
                raw = body["choices"][0]["message"]["content"]
            else:
                raw = response_output_text(body)
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"steps"}:
                raise ValueError("Outbound copy response must contain only steps.")
            steps = validate_generated_steps(
                data.get("steps"),
                expected_count=int(safe_context["request"]["step_count"]),
            )
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise OutboundCopyProviderError(
                f"{provider} returned invalid outbound copy.",
                code=f"{provider}_copy_invalid_response",
            ) from exc
        usage = body.get("usage") or {}
        return OutboundCopyResult(
            steps=tuple(steps),
            response_id=str(body.get("id", ""))[:255],
            input_tokens=optional_nonnegative_int(
                usage.get("input_tokens", usage.get("prompt_tokens"))
            ),
            output_tokens=optional_nonnegative_int(
                usage.get("output_tokens", usage.get("completion_tokens"))
            ),
            provider=provider,
            model=self.model,
        )
