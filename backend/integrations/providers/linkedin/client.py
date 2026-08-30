"""Client for LinkedIn's restricted Invitations API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import requests

from integrations.execution_safety import (
    ExecutionSafetyError,
    assert_provider_io_authorized,
)


class LinkedInInvitationsAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        error_code: str = "linkedin_provider_error",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class LinkedInInvitationResponse:
    invitation_id: str
    snapshot: Mapping[str, Any]


class LinkedInInvitationsClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.linkedin.com",
        timeout: float = 10.0,
        session: requests.Session | None = None,
        org=None,
        execution_request_id=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.org = org
        self.execution_request_id = execution_request_id

    def send_email_invitation(
        self,
        *,
        access_token: str,
        recipient_email: str,
        message_body: str = "",
    ) -> LinkedInInvitationResponse:
        payload: dict[str, Any] = {
            "invitee": f"urn:li:email:{recipient_email.strip().lower()}"
        }
        if message_body.strip():
            payload["message"] = {
                "com.linkedin.invitations.InvitationMessage": {
                    "body": message_body.strip()
                }
            }
        try:
            assert_provider_io_authorized(
                org=self.org,
                channel="linkedin",
                action="send_invitation",
                execution_request_id=self.execution_request_id,
            )
        except ExecutionSafetyError as exc:
            raise LinkedInInvitationsAPIError(
                exc.detail, retryable=False, error_code=exc.code
            ) from exc
        try:
            response = self.session.post(
                f"{self.base_url}/v2/invitations",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-Restli-Protocol-Version": "2.0.0",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LinkedInInvitationsAPIError(
                "LinkedIn Invitations API request failed",
                retryable=True,
                error_code="linkedin_transport_error",
            ) from exc

        response_payload = _response_payload(response)
        if response.status_code >= 400:
            message = _provider_error_message(response_payload)
            error_code = (
                "linkedin_partner_access_required"
                if response.status_code in {401, 403}
                else f"linkedin_provider_http_{response.status_code}"
            )
            raise LinkedInInvitationsAPIError(
                message or "LinkedIn rejected the invitation",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
                error_code=error_code,
            )

        invitation_id = str(response.headers.get("x-linkedin-id", "")).strip()
        if not invitation_id and isinstance(response_payload, Mapping):
            invitation_id = str(response_payload.get("id", "")).strip()
        if not invitation_id:
            raise LinkedInInvitationsAPIError(
                "LinkedIn did not return the invitation id",
                retryable=False,
                status_code=response.status_code,
                error_code="linkedin_invitation_id_missing",
            )
        snapshot = (
            dict(response_payload)
            if isinstance(response_payload, Mapping)
            else {"accepted": True}
        )
        snapshot["accepted"] = True
        return LinkedInInvitationResponse(
            invitation_id=invitation_id[:255],
            snapshot=snapshot,
        )


def _response_payload(response) -> Mapping[str, Any] | None:
    if not getattr(response, "content", b""):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _provider_error_message(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return ""
    return str(payload.get("message") or payload.get("error_description") or "").strip()
