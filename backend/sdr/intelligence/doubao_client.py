"""Volcengine Ark Responses API adapter for Doubao models."""

from __future__ import annotations

import json
from uuid import UUID

import requests

from sdr.domain import LeadCandidate, QualificationResult
from sdr.intelligence.contracts import (
    OUTPUT_SCHEMA,
    AIQualification,
    ModelProviderError,
    build_lead_context,
    optional_nonnegative_int,
    qualification_instructions,
    response_output_text,
    validate_result,
)
from sdr.intelligence.research import ResearchResult


class DoubaoQualificationError(ModelProviderError):
    pass


class DoubaoLeadQualifier:
    provider = "doubao"

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
        candidate: LeadCandidate,
        baseline: QualificationResult,
        research: ResearchResult | None,
        icp_description: str,
        positive_signals: str,
        negative_signals: str,
    ) -> AIQualification:
        if not self.api_key:
            raise DoubaoQualificationError(
                "Doubao API key is not configured.",
                code="doubao_not_configured",
            )
        context = build_lead_context(
            candidate=candidate,
            baseline=baseline,
            research=research,
            icp_description=icp_description,
            positive_signals=positive_signals,
            negative_signals=negative_signals,
        )
        payload = {
            "model": self.model,
            "instructions": qualification_instructions(),
            "input": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            "thinking": {
                "type": "disabled" if self.reasoning_effort == "none" else "enabled"
            },
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "lead_qualification",
                    "strict": True,
                    "schema": OUTPUT_SCHEMA,
                }
            },
            "max_output_tokens": 1200,
            "store": False,
        }
        try:
            response = self.session.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise DoubaoQualificationError(
                "Doubao qualification request failed.",
                code="doubao_request_failed",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise DoubaoQualificationError(
                f"Doubao qualification returned HTTP {response.status_code}.",
                code="doubao_http_error",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            body = response.json()
            result = json.loads(response_output_text(body))
            qualification = validate_result(
                result,
                provider=self.provider,
                model=self.model,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DoubaoQualificationError(
                "Doubao qualification returned an invalid structured result.",
                code="doubao_invalid_response",
            ) from exc
        usage = body.get("usage") or {}
        return AIQualification(
            qualification=qualification,
            response_id=str(body.get("id", ""))[:255],
            input_tokens=optional_nonnegative_int(usage.get("input_tokens")),
            output_tokens=optional_nonnegative_int(usage.get("output_tokens")),
            provider=self.provider,
            model=self.model,
        )
