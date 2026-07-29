"""Small version-pinned client for the Meta Graph endpoints used by Lead Ads."""

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import requests


class FacebookGraphAPIError(RuntimeError):
    def __init__(
        self, message: str, *, retryable: bool, status_code: int | None = None
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class FacebookGraphClient:
    LEAD_FIELDS = "id,created_time,ad_id,form_id,field_data,is_organic,platform"

    def __init__(
        self,
        *,
        app_secret: str,
        api_version: str,
        base_url: str = "https://graph.facebook.com",
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.app_secret = app_secret
        self.api_version = api_version.strip("/")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch_lead(self, *, leadgen_id: str, access_token: str) -> Mapping[str, Any]:
        return self._get(
            leadgen_id,
            access_token=access_token,
            params={"fields": self.LEAD_FIELDS},
        )

    def fetch_page_identity(self, *, access_token: str) -> Mapping[str, Any]:
        return self._get("me", access_token=access_token, params={"fields": "id,name"})

    def subscribe_page(self, *, page_id: str, access_token: str) -> None:
        self._request(
            "POST",
            f"{page_id}/subscribed_apps",
            access_token=access_token,
            params={"subscribed_fields": "leadgen"},
        )

    def _get(
        self,
        object_id: str,
        *,
        access_token: str,
        params: Mapping[str, str],
    ) -> Mapping[str, Any]:
        return self._request("GET", object_id, access_token=access_token, params=params)

    def _request(
        self,
        method: str,
        object_id: str,
        *,
        access_token: str,
        params: Mapping[str, str],
    ) -> Mapping[str, Any]:
        request_params = dict(params)
        request_params["access_token"] = access_token
        if self.app_secret:
            request_params["appsecret_proof"] = hmac.new(
                self.app_secret.encode("utf-8"),
                access_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/{self.api_version}/{object_id}",
                params=request_params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise FacebookGraphAPIError(
                "Meta Graph request failed", retryable=True
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise FacebookGraphAPIError(
                "Meta Graph returned a non-JSON response",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            ) from exc
        if response.status_code >= 400 or not isinstance(payload, Mapping):
            api_error = payload.get("error", {}) if isinstance(payload, Mapping) else {}
            message = (
                api_error.get("message")
                if isinstance(api_error, Mapping)
                else "Meta Graph request failed"
            )
            raise FacebookGraphAPIError(
                str(message or "Meta Graph request failed"),
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
            )
        return payload
