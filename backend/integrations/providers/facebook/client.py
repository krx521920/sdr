"""Small version-pinned client for the Meta Graph endpoints used by Lead Ads."""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import requests


class FacebookGraphAPIError(RuntimeError):
    def __init__(
        self, message: str, *, retryable: bool, status_code: int | None = None
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class FacebookOAuthToken:
    access_token: str
    expires_in: int | None = None


class FacebookGraphClient:
    LEAD_FIELDS = "id,created_time,ad_id,form_id,field_data,is_organic,platform"

    def __init__(
        self,
        *,
        app_id: str = "",
        app_secret: str,
        api_version: str,
        base_url: str = "https://graph.facebook.com",
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.app_id = app_id
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

    def exchange_oauth_code(
        self, *, code: str, redirect_uri: str
    ) -> FacebookOAuthToken:
        short_lived = self._oauth_token(
            {
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            }
        )
        return self._oauth_token(
            {
                "grant_type": "fb_exchange_token",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "fb_exchange_token": short_lived.access_token,
            }
        )

    def fetch_managed_pages(self, *, user_access_token: str) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(10):
            params: dict[str, Any] = {
                "fields": "id,name,access_token,tasks",
                "limit": 100,
            }
            if after:
                params["after"] = after
            payload = self._get(
                "me/accounts",
                access_token=user_access_token,
                params=params,
            )
            data = payload.get("data", [])
            if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
                raise FacebookGraphAPIError(
                    "Meta returned an invalid Page list", retryable=False
                )
            pages.extend(dict(page) for page in data if isinstance(page, Mapping))

            paging = payload.get("paging", {})
            cursors = paging.get("cursors", {}) if isinstance(paging, Mapping) else {}
            next_after = (
                str(cursors.get("after", "")).strip()
                if isinstance(cursors, Mapping)
                else ""
            )
            has_next = isinstance(paging, Mapping) and bool(paging.get("next"))
            if not has_next or not next_after or next_after in seen_cursors:
                break
            seen_cursors.add(next_after)
            after = next_after
        return pages

    def _oauth_token(self, params: Mapping[str, Any]) -> FacebookOAuthToken:
        payload = self._send_request("GET", "oauth/access_token", params=params)
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            raise FacebookGraphAPIError(
                "Meta did not return an OAuth access token", retryable=False
            )
        expires_in = payload.get("expires_in")
        try:
            parsed_expiry = int(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            parsed_expiry = None
        return FacebookOAuthToken(access_token=access_token, expires_in=parsed_expiry)

    def _get(
        self,
        object_id: str,
        *,
        access_token: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._request("GET", object_id, access_token=access_token, params=params)

    def _request(
        self,
        method: str,
        object_id: str,
        *,
        access_token: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        request_params = dict(params)
        request_params["access_token"] = access_token
        if self.app_secret:
            request_params["appsecret_proof"] = hmac.new(
                self.app_secret.encode("utf-8"),
                access_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return self._send_request(method, object_id, params=request_params)

    def _send_request(
        self, method: str, object_id: str, *, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/{self.api_version}/{object_id}",
                params=dict(params),
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
