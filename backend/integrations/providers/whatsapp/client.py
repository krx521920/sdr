"""Version-pinned Meta WhatsApp Cloud API client."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import requests

from integrations.execution_safety import (
    ExecutionSafetyError,
    assert_provider_io_authorized,
    hash_target_identifier,
)

WHATSAPP_RECIPIENT_RE = re.compile(r"^[0-9]{8,15}$")
WHATSAPP_RECIPIENT_SEPARATORS = re.compile(r"[\s()+.\-]")


def whatsapp_provider_execution_hashes(
    *,
    org,
    message_id: UUID,
    campaign_id: UUID,
    campaign_run: int,
    phone_number_id: str,
    recipient: str,
    template_name: str,
    language_code: str,
) -> tuple[str, str]:
    """Hash the exact non-secret arguments used to build one Meta request."""

    normalized_recipient = WHATSAPP_RECIPIENT_SEPARATORS.sub(
        "", str(recipient or "").strip()
    )
    if not WHATSAPP_RECIPIENT_RE.fullmatch(normalized_recipient):
        raise ValueError("WhatsApp recipient must contain 8-15 international phone digits.")
    scope = {
        "schema_version": 1,
        "message_id": str(message_id),
        "campaign_id": str(campaign_id),
        "campaign_run": int(campaign_run),
        "phone_number_id": str(phone_number_id),
        "recipient": normalized_recipient,
        "template_name": str(template_name),
        "template_language": str(language_code),
    }
    payload_hash = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    target_hash = hash_target_identifier(
        org=org,
        channel="whatsapp",
        identifier=normalized_recipient,
    )
    return target_hash, payload_hash


class WhatsAppCloudAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        error_code: str = "whatsapp_provider_error",
        outcome_known: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.error_code = error_code
        self.outcome_known = outcome_known


class WhatsAppCloudClient:
    def __init__(
        self,
        *,
        api_version: str,
        base_url: str = "https://graph.facebook.com",
        timeout: float = 10.0,
        session: requests.Session | None = None,
        org=None,
        execution_request_id=None,
    ):
        self.api_version = api_version.strip("/")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.org = org
        self.execution_request_id = execution_request_id

    def send_template(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        recipient: str,
        template_name: str,
        language_code: str,
        message_id: UUID | None = None,
        campaign_id: UUID | None = None,
        campaign_run: int | None = None,
    ) -> Mapping[str, Any]:
        normalized_recipient = WHATSAPP_RECIPIENT_SEPARATORS.sub(
            "", str(recipient or "").strip()
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalized_recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        try:
            exact_scope = {}
            if self.org is not None and self.execution_request_id is not None:
                if message_id is None or campaign_id is None or campaign_run is None:
                    raise ExecutionSafetyError(
                        code="execution_scope_mismatch",
                        detail=(
                            "The provider call no longer matches its approved "
                            "execution scope."
                        ),
                        status_code=403,
                    )
                target_hash, payload_hash = whatsapp_provider_execution_hashes(
                    org=self.org,
                    message_id=message_id,
                    campaign_id=campaign_id,
                    campaign_run=campaign_run,
                    phone_number_id=phone_number_id,
                    recipient=normalized_recipient,
                    template_name=template_name,
                    language_code=language_code,
                )
                exact_scope = {
                    "target_hash": target_hash,
                    "payload_hash": payload_hash,
                    "units": 1,
                    "idempotency_key": message_id,
                }
            assert_provider_io_authorized(
                org=self.org,
                channel="whatsapp",
                action="send_message",
                execution_request_id=self.execution_request_id,
                **exact_scope,
            )
        except (ExecutionSafetyError, TypeError, ValueError) as exc:
            code = (
                exc.code
                if isinstance(exc, ExecutionSafetyError)
                else "execution_scope_mismatch"
            )
            detail = (
                exc.detail
                if isinstance(exc, ExecutionSafetyError)
                else "The provider call no longer matches its approved execution scope."
            )
            raise WhatsAppCloudAPIError(
                detail,
                retryable=False,
                error_code=code,
                outcome_known=True,
            ) from exc
        try:
            response = self.session.post(
                (f"{self.base_url}/{self.api_version}/{phone_number_id}/messages"),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WhatsAppCloudAPIError(
                "WhatsApp Cloud API request failed",
                retryable=True,
                error_code="whatsapp_transport_error",
            ) from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise WhatsAppCloudAPIError(
                "WhatsApp Cloud API returned a non-JSON response",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
                error_code="whatsapp_invalid_response",
            ) from exc
        if response.status_code >= 400 or not isinstance(response_payload, Mapping):
            error = (
                response_payload.get("error", {})
                if isinstance(response_payload, Mapping)
                else {}
            )
            provider_code = (
                str(error.get("code", "")).strip() if isinstance(error, Mapping) else ""
            )
            if not provider_code.isdigit() or len(provider_code) > 16:
                provider_code = ""
            raise WhatsAppCloudAPIError(
                "WhatsApp Cloud API rejected the message",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
                error_code=(
                    f"whatsapp_provider_{provider_code}"
                    if provider_code
                    else "whatsapp_provider_error"
                ),
                outcome_known=(
                    bool(error)
                    and 400 <= response.status_code < 500
                    and response.status_code not in {408, 429}
                ),
            )

        messages = response_payload.get("messages", [])
        message_id = ""
        if isinstance(messages, list) and messages and isinstance(messages[0], Mapping):
            message_id = str(messages[0].get("id", "")).strip()
        if not message_id:
            raise WhatsAppCloudAPIError(
                "WhatsApp Cloud API did not return a message id",
                retryable=False,
                status_code=response.status_code,
                error_code="whatsapp_message_id_missing",
            )
        return response_payload
