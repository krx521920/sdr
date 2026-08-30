"""Small official OpenAPI client for Feishu Base record upserts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from integrations.execution_safety import (
    ExecutionSafetyError,
    assert_provider_io_authorized,
)

RETRYABLE_FEISHU_CODES = frozenset(
    {
        1254290,  # rate limited
        1254291,  # concurrent write conflict
        1254607,  # data not ready
        1255040,  # request timeout
    }
)

FEISHU_VALIDATE_SCHEMA_ACTION = "validate_base_schema"
FEISHU_SYNC_RESEARCH_ACTION = "sync_research_result"
FEISHU_DELETE_RESEARCH_ACTION = "delete_research_record"
FEISHU_IMPORT_PERSON_ACTION = "import_person_records"
FEISHU_EXECUTION_ACTIONS = frozenset(
    {
        FEISHU_VALIDATE_SCHEMA_ACTION,
        FEISHU_SYNC_RESEARCH_ACTION,
        FEISHU_DELETE_RESEARCH_ACTION,
        FEISHU_IMPORT_PERSON_ACTION,
    }
)
MAX_FEISHU_READ_REQUESTS = 20
MAX_FEISHU_FIELD_PAGES = 10
MAX_FEISHU_FIELDS = 1000
MAX_FEISHU_RECORD_PAGES = 10
MAX_FEISHU_IMPORT_RECORDS = 500
_PROVIDER_ERROR_MESSAGE = "Feishu OpenAPI request did not complete successfully."
ALLOWED_FEISHU_API_HOSTS = frozenset({"open.feishu.cn", "open.larksuite.com"})

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
        mutation_attempted: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.error_code = error_code
        self.provider_code = provider_code
        self.status_code = status_code
        self.mutation_attempted = mutation_attempted


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
        org=None,
        execution_request_id=None,
        execution_action: str = "",
    ):
        self.base_url = _validated_base_url(base_url)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.org = org
        self.execution_request_id = execution_request_id
        self.execution_action = execution_action
        self._read_request_count = 0
        self._mutation_started = False
        self._record_snapshot_started = False

    def for_execution(self, *, org, action: str, execution_request_id):
        """Return a bounded client tied to one exact durable execution request."""

        if action not in FEISHU_EXECUTION_ACTIONS:
            raise FeishuBaseConfigurationError(
                "Unsupported Feishu Base execution action."
            )
        return FeishuBaseClient(
            base_url=self.base_url,
            timeout=self.timeout,
            session=self.session,
            org=org,
            execution_request_id=execution_request_id,
            execution_action=action,
        )

    def tenant_access_token(self, *, app_id: str, app_secret: str) -> str:
        data = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            operation="authenticate",
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
        page_count = 0
        while path:
            page_count += 1
            if page_count > MAX_FEISHU_FIELD_PAGES:
                raise FeishuBaseAPIError(
                    "Feishu Base field schema exceeds the supported limit.",
                    retryable=False,
                    error_code="feishu_field_page_limit_exceeded",
                )
            data = self._request(
                "GET",
                path,
                access_token=access_token,
                operation="list_fields",
            )
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise FeishuBaseAPIError(
                    "Feishu returned an invalid field list.",
                    retryable=False,
                    error_code="feishu_invalid_field_response",
                )
            items.extend(item for item in page_items if isinstance(item, dict))
            if len(items) > MAX_FEISHU_FIELDS:
                raise FeishuBaseAPIError(
                    "Feishu Base field schema exceeds the supported limit.",
                    retryable=False,
                    error_code="feishu_field_limit_exceeded",
                )
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
            operation="find_record",
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

    def list_records(
        self,
        *,
        access_token: str,
        app_token: str,
        table_id: str,
        limit: int,
    ) -> list[FeishuBaseRecord]:
        """Read a bounded Base snapshot without persisting raw record ids."""

        if not isinstance(limit, int) or not 1 <= limit <= MAX_FEISHU_IMPORT_RECORDS:
            raise FeishuBaseConfigurationError(
                "Feishu Base import limit is invalid."
            )
        if self._record_snapshot_started:
            raise FeishuBaseAPIError(
                "A Feishu Base import execution can read only one record snapshot.",
                retryable=False,
                error_code="feishu_record_snapshot_reused",
            )
        # Burn before the first request so transport failure cannot authorize a
        # second snapshot in the same worker process.
        self._record_snapshot_started = True
        path = self._table_path(app_token, table_id) + (
            f"/records?page_size={min(100, limit)}"
        )
        records: list[FeishuBaseRecord] = []
        page_count = 0
        while path and len(records) < limit:
            page_count += 1
            if page_count > MAX_FEISHU_RECORD_PAGES:
                raise FeishuBaseAPIError(
                    "Feishu Base records exceed the supported page limit.",
                    retryable=False,
                    error_code="feishu_record_page_limit_exceeded",
                )
            data = self._request(
                "GET",
                path,
                access_token=access_token,
                operation="list_records",
            )
            items = data.get("items") or []
            if not isinstance(items, list):
                raise FeishuBaseAPIError(
                    "Feishu returned an invalid record list.",
                    retryable=False,
                    error_code="feishu_invalid_record_response",
                )
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                record_id = str(item.get("record_id") or "").strip()
                if not record_id or len(record_id) > 255:
                    raise FeishuBaseAPIError(
                        "Feishu returned an invalid record id.",
                        retryable=False,
                        error_code="feishu_record_id_missing",
                    )
                fields = item.get("fields")
                if not isinstance(fields, Mapping):
                    fields = {}
                records.append(
                    FeishuBaseRecord(record_id=record_id, fields=dict(fields))
                )
                if len(records) >= limit:
                    break
            page_token = str(data.get("page_token") or "").strip()
            remaining = limit - len(records)
            path = (
                self._table_path(app_token, table_id)
                + "/records"
                + f"?page_size={min(100, remaining)}"
                + f"&page_token={quote(page_token, safe='')}"
                if remaining > 0 and data.get("has_more") and page_token
                else ""
            )
        return records

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
            operation="create_record",
            mutation=True,
        )
        return _record_from_data(data, mutation_attempted=True)

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
            operation="update_record",
            mutation=True,
        )
        return _record_from_data(data, mutation_attempted=True)

    def delete_record(
        self,
        *,
        access_token: str,
        app_token: str,
        table_id: str,
        record_id: str,
    ) -> None:
        self._request(
            "DELETE",
            self._table_path(app_token, table_id)
            + f"/records/{quote(record_id, safe='')}",
            access_token=access_token,
            operation="delete_record",
            mutation=True,
        )

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
        operation: str,
        mutation: bool = False,
    ) -> Mapping[str, Any]:
        self._authorize_operation(operation=operation, mutation=mutation)
        try:
            assert_provider_io_authorized(
                org=self.org,
                channel="feishu",
                action=self.execution_action,
                execution_request_id=self.execution_request_id,
            )
        except ExecutionSafetyError as exc:
            raise FeishuBaseAPIError(
                exc.detail, retryable=False, error_code=exc.code
            ) from exc
        # Burn the in-memory capability before entering requests. A timeout after
        # a mutation has an unknown outcome and must never permit a second write.
        if mutation:
            self._mutation_started = True
        else:
            self._read_request_count += 1
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
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise FeishuBaseAPIError(
                _PROVIDER_ERROR_MESSAGE,
                retryable=True,
                error_code="feishu_transport_error",
                mutation_attempted=mutation,
            ) from exc
        if 300 <= response.status_code < 400:
            raise FeishuBaseAPIError(
                _PROVIDER_ERROR_MESSAGE,
                retryable=False,
                error_code="feishu_http_redirect",
                status_code=response.status_code,
                mutation_attempted=mutation,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, Mapping) else {}
        provider_code = _integer_or_none(payload.get("code"))
        if response.status_code >= 400 or provider_code not in (None, 0):
            retryable = (
                response.status_code == 429
                or response.status_code >= 500
                or provider_code in RETRYABLE_FEISHU_CODES
            )
            if response.status_code >= 400:
                error_code = f"feishu_http_{response.status_code}"
            elif provider_code in RETRYABLE_FEISHU_CODES:
                error_code = "feishu_provider_retryable"
            else:
                error_code = "feishu_provider_error"
            raise FeishuBaseAPIError(
                _PROVIDER_ERROR_MESSAGE,
                retryable=retryable,
                error_code=error_code,
                provider_code=provider_code,
                status_code=response.status_code,
                mutation_attempted=mutation,
            )
        data = payload.get("data")
        if isinstance(data, Mapping):
            return data
        # The tenant-token endpoint returns its token at the top level.
        return payload

    def _authorize_operation(self, *, operation: str, mutation: bool) -> None:
        allowed = {
            FEISHU_VALIDATE_SCHEMA_ACTION: {"authenticate", "list_fields"},
            FEISHU_SYNC_RESEARCH_ACTION: {
                "authenticate",
                "list_fields",
                "find_record",
                "create_record",
                "update_record",
            },
            FEISHU_DELETE_RESEARCH_ACTION: {"authenticate", "delete_record"},
            FEISHU_IMPORT_PERSON_ACTION: {
                "authenticate",
                "list_fields",
                "list_records",
            },
        }
        if self.execution_action and operation not in allowed.get(
            self.execution_action, set()
        ):
            raise FeishuBaseAPIError(
                "Feishu Base execution action does not match the provider call.",
                retryable=False,
                error_code="feishu_execution_action_mismatch",
            )
        if mutation and self._mutation_started:
            raise FeishuBaseAPIError(
                "A Feishu Base execution request can authorize only one mutation.",
                retryable=False,
                error_code="feishu_execution_request_reused",
            )
        if not mutation and self._read_request_count >= MAX_FEISHU_READ_REQUESTS:
            raise FeishuBaseAPIError(
                "Feishu Base execution exceeded the provider read limit.",
                retryable=False,
                error_code="feishu_read_limit_exceeded",
            )


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


def _record_from_data(
    data: Mapping[str, Any], *, mutation_attempted: bool = False
) -> FeishuBaseRecord:
    item = data.get("record") if isinstance(data.get("record"), Mapping) else data
    record_id = str(item.get("record_id") or "").strip()
    if not record_id:
        raise FeishuBaseAPIError(
            "Feishu did not return the saved record id.",
            retryable=False,
            error_code="feishu_record_id_missing",
            mutation_attempted=mutation_attempted,
        )
    fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
    return FeishuBaseRecord(record_id=record_id, fields=dict(fields))


def feishu_base_target_identifier(connection_id) -> str:
    """Opaque internal target used for approvals; never a provider record id."""

    if not connection_id:
        raise ValueError("Feishu Base connection id is required")
    return f"feishu-base:{connection_id}"


def _validated_base_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise FeishuBaseConfigurationError(
            "The Feishu OpenAPI base URL is invalid."
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in ALLOWED_FEISHU_API_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise FeishuBaseConfigurationError(
            "The Feishu OpenAPI base URL must use an approved HTTPS host."
        )
    return f"https://{(parsed.hostname or '').lower()}" + (
        ":443" if port == 443 else ""
    )


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
