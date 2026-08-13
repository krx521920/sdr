"""Small official OpenAPI client for Feishu Base record upserts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

RETRYABLE_FEISHU_CODES = frozenset(
    {
        1254290,  # rate limited
        1254291,  # concurrent write conflict
        1254607,  # data not ready
        1255040,  # request timeout
    }
)

# Feishu Bitable field types used by this exporter. Computed/system/attachment
# fields are deliberately not accepted as write targets.
TEXT = 1
NUMBER = 2
SINGLE_SELECT = 3
DATE_TIME = 5
PHONE = 13
URL = 15

EXPECTED_FIELD_TYPES: dict[str, frozenset[int]] = {
    "intake_id": frozenset({TEXT}),
    "source": frozenset({TEXT, SINGLE_SELECT}),
    "qualification_score": frozenset({NUMBER}),
    "qualification_band": frozenset({TEXT, SINGLE_SELECT}),
    "inspection_status": frozenset({TEXT, SINGLE_SELECT}),
    "phone": frozenset({TEXT, PHONE}),
    "linkedin_url": frozenset({TEXT, URL}),
    "website": frozenset({TEXT, URL}),
    "processed_at": frozenset({DATE_TIME}),
}
DEFAULT_EXPECTED_FIELD_TYPES = frozenset({TEXT})


class FeishuBaseAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        error_code: str = "feishu_base_provider_error",
        provider_code: int | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.error_code = error_code
        self.provider_code = provider_code
        self.status_code = status_code


class FeishuBaseConfigurationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        missing_fields: list[str] | None = None,
        type_mismatches: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.missing_fields = missing_fields or []
        self.type_mismatches = type_mismatches or []


@dataclass(frozen=True, slots=True)
class FeishuBaseRecord:
    record_id: str
    fields: Mapping[str, Any]


class FeishuBaseClient:
    def __init__(
        self,
        *,
        base_url: str = "https://open.feishu.cn",
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def tenant_access_token(self, *, app_id: str, app_secret: str) -> str:
        data = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        token = str(data.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuBaseAPIError(
                "Feishu did not return a tenant access token.",
                retryable=False,
                error_code="feishu_access_token_missing",
            )
        return token

    def list_fields(
        self,
        *,
        access_token: str,
        app_token: str,
        table_id: str,
    ) -> list[dict[str, Any]]:
        path = self._table_path(app_token, table_id) + "/fields?page_size=100"
        items: list[dict[str, Any]] = []
        while path:
            data = self._request("GET", path, access_token=access_token)
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise FeishuBaseAPIError(
                    "Feishu returned an invalid field list.",
                    retryable=False,
                    error_code="feishu_invalid_field_response",
                )
            items.extend(item for item in page_items if isinstance(item, dict))
            page_token = str(data.get("page_token") or "").strip()
            path = (
                self._table_path(app_token, table_id)
                + f"/fields?page_size=100&page_token={quote(page_token, safe='')}"
                if data.get("has_more") and page_token
                else ""
            )
        return items

    def find_record_by_field(
        self,
        *,
        access_token: str,
        app_token: str,
        table_id: str,
        field_name: str,
        value: str,
    ) -> FeishuBaseRecord | None:
        data = self._request(
            "POST",
            self._table_path(app_token, table_id) + "/records/search?page_size=2",
            access_token=access_token,
            json={
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_name,
                            "operator": "is",
                            "value": [value],
                        }
                    ],
                }
            },
        )
        items = data.get("items") or []
        if not isinstance(items, list):
            raise FeishuBaseAPIError(
                "Feishu returned an invalid record search result.",
                retryable=False,
                error_code="feishu_invalid_search_response",
            )
        if len(items) > 1:
            raise FeishuBaseConfigurationError(
                f'Feishu Base contains duplicate values in business key field "{field_name}".'
            )
        if not items:
            return None
        item = items[0]
        record_id = str(item.get("record_id") or "").strip()
        if not record_id:
            raise FeishuBaseAPIError(
                "Feishu did not return a record id.",
                retryable=False,
                error_code="feishu_record_id_missing",
            )
        fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
        return FeishuBaseRecord(record_id=record_id, fields=dict(fields))

    def create_record(
        self,
        *,
        access_token: str,
        app_token: str,
        table_id: str,
        fields: Mapping[str, Any],
    ) -> FeishuBaseRecord:
        data = self._request(
            "POST",
            self._table_path(app_token, table_id) + "/records",
            access_token=access_token,
            json={"fields": dict(fields)},
        )
        return _record_from_data(data)

    def update_record(
        self,
        *,
        access_token: str,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: Mapping[str, Any],
    ) -> FeishuBaseRecord:
        data = self._request(
            "PUT",
            self._table_path(app_token, table_id)
            + f"/records/{quote(record_id, safe='')}",
            access_token=access_token,
            json={"fields": dict(fields)},
        )
        return _record_from_data(data)

    def _table_path(self, app_token: str, table_id: str) -> str:
        return (
            f"/open-apis/bitable/v1/apps/{quote(app_token, safe='')}"
            f"/tables/{quote(table_id, safe='')}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str = "",
        json: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=dict(json) if json is not None else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise FeishuBaseAPIError(
                "Feishu OpenAPI request failed.",
                retryable=True,
                error_code="feishu_transport_error",
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, Mapping) else {}
        provider_code = _integer_or_none(payload.get("code"))
        if response.status_code >= 400 or provider_code not in (None, 0):
            message = str(payload.get("msg") or payload.get("message") or "").strip()
            retryable = (
                response.status_code == 429
                or response.status_code >= 500
                or provider_code in RETRYABLE_FEISHU_CODES
            )
            code_suffix = (
                provider_code if provider_code is not None else response.status_code
            )
            raise FeishuBaseAPIError(
                message or "Feishu rejected the request.",
                retryable=retryable,
                error_code=f"feishu_provider_{code_suffix}",
                provider_code=provider_code,
                status_code=response.status_code,
            )
        data = payload.get("data")
        if isinstance(data, Mapping):
            return data
        # The tenant-token endpoint returns its token at the top level.
        return payload


def validate_field_mapping(
    mapping: Mapping[str, str],
    provider_fields: list[Mapping[str, Any]],
) -> None:
    by_name = {
        str(item.get("field_name") or "").strip(): item
        for item in provider_fields
        if str(item.get("field_name") or "").strip()
    }
    missing = sorted({name for name in mapping.values() if name not in by_name})
    mismatches: list[dict[str, Any]] = []
    for key, field_name in mapping.items():
        item = by_name.get(field_name)
        if item is None:
            continue
        field_type = _integer_or_none(item.get("type"))
        expected = EXPECTED_FIELD_TYPES.get(key, DEFAULT_EXPECTED_FIELD_TYPES)
        if field_type not in expected:
            mismatches.append(
                {
                    "key": key,
                    "field_name": field_name,
                    "actual_type": field_type,
                    "expected_types": sorted(expected),
                }
            )
    if missing or mismatches:
        raise FeishuBaseConfigurationError(
            "The Feishu Base field mapping does not match the target table.",
            missing_fields=missing,
            type_mismatches=mismatches,
        )


def _record_from_data(data: Mapping[str, Any]) -> FeishuBaseRecord:
    item = data.get("record") if isinstance(data.get("record"), Mapping) else data
    record_id = str(item.get("record_id") or "").strip()
    if not record_id:
        raise FeishuBaseAPIError(
            "Feishu did not return the saved record id.",
            retryable=False,
            error_code="feishu_record_id_missing",
        )
    fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
    return FeishuBaseRecord(record_id=record_id, fields=dict(fields))


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
