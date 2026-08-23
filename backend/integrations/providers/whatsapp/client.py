"""Version-pinned Meta WhatsApp Cloud API client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests


class WhatsAppCloudAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        error_code: str = "whatsapp_provider_error",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.error_code = error_code


class WhatsAppCloudClient:
    def __init__(
        self,
        *,
        api_version: str,
        base_url: str = "https://graph.facebook.com",
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.api_version = api_version.strip("/")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def send_template(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        recipient: str,
        template_name: str,
        language_code: str,
    ) -> Mapping[str, Any]:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
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
            message = (
                str(error.get("message", "")).strip()
                if isinstance(error, Mapping)
                else ""
            )
            provider_code = (
                str(error.get("code", "")).strip() if isinstance(error, Mapping) else ""
            )
            raise WhatsAppCloudAPIError(
                message or "WhatsApp Cloud API rejected the message",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
                error_code=(
                    f"whatsapp_provider_{provider_code}"
                    if provider_code
                    else "whatsapp_provider_error"
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
