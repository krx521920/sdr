"""Runtime ports registered by concrete provider applications at startup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


class ProviderAdapterUnavailable(LookupError):
    pass


class ProviderAdapterError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class ExecutionSafetyError(ValueError):
    """SDR-owned failure contract for the external-execution safety port."""

    def __init__(self, *, code: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class ExecutionChannel:
    """Stable channel values shared with an execution-safety adapter."""

    EMAIL = "email"
    WHATSAPP = "whatsapp"
    LINKEDIN = "linkedin"
    FEISHU = "feishu"
    APOLLO = "apollo"
    FACEBOOK = "facebook"
    WECHAT = "wechat"
    WECOM = "wecom"


class ExternalRequestStatus:
    """Stable execution-state values consumed by the SDR application."""

    RESERVED = "reserved"
    SENDING = "sending"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


APOLLO_SEARCH_ACTION = "search_people"
APOLLO_ENRICH_ACTION = "enrich_person"


class ExternalExecutionRequestPort(Protocol):
    id: UUID
    org: Any
    org_id: UUID
    channel: str
    action: str
    idempotency_key: UUID
    target_hash: str
    payload_hash: str
    units: int
    status: str

    def refresh_from_db(self) -> None: ...


@dataclass(frozen=True)
class ExecutionReservation:
    request: ExternalExecutionRequestPort
    replayed: bool


class ExecutionSafetyAdapterPort(Protocol):
    def assert_provider_io_authorized(
        self,
        *,
        org: Any | None,
        channel: str,
        action: str,
        execution_request_id: UUID | None,
    ) -> None: ...

    def hash_target_identifier(
        self,
        *,
        org: Any,
        channel: str,
        identifier: str,
    ) -> str: ...

    def reserve_execution(
        self,
        *,
        org: Any,
        channel: str,
        action: str,
        target_hash: str,
        payload_hash: str,
        units: int,
        approval_id: UUID,
        idempotency_key: UUID,
    ) -> ExecutionReservation: ...

    def get_request(
        self,
        *,
        request_id: UUID,
        org: Any | None = None,
        org_id: UUID | None = None,
        status: str | None = None,
        for_update: bool = False,
        include_org: bool = False,
    ) -> ExternalExecutionRequestPort | None: ...

    def mark_sending(
        self,
        *,
        org: Any,
        request_id: UUID,
        expected_status: str | None = None,
    ) -> ExternalExecutionRequestPort: ...

    def mark_provider_accepted(
        self,
        *,
        org: Any,
        request_id: UUID,
        local_state_uncertain: bool = False,
    ) -> ExternalExecutionRequestPort: ...

    def release(
        self,
        *,
        org: Any,
        request_id: UUID,
        error_code: str,
        expected_status: str | None = None,
    ) -> ExternalExecutionRequestPort: ...

    def mark_delivered(
        self,
        *,
        org: Any,
        request_id: UUID,
    ) -> ExternalExecutionRequestPort: ...

    def reconcile_stale_reserved(
        self,
        *,
        org: Any,
        older_than: datetime,
        limit: int = 100,
    ) -> list[UUID]: ...

    def reconcile_stale_sending(
        self,
        *,
        org: Any,
        older_than: datetime,
        limit: int = 100,
    ) -> list[UUID]: ...


class ProspectSourceClientPort(Protocol):
    def for_execution(
        self,
        *,
        org: Any,
        action: str,
        execution_request_id: UUID,
    ) -> "ProspectSourceClientPort": ...

    def search_people(
        self,
        *,
        filters: Mapping[str, Any],
        page: int,
        per_page: int,
    ) -> Mapping[str, Any]: ...

    def enrich_person(self, *, person_id: str) -> Mapping[str, Any] | None: ...


class ProspectSourceAdapterPort(Protocol):
    def is_ready(self, *, org_id: UUID) -> bool: ...

    def client_for(self, *, org_id: UUID) -> ProspectSourceClientPort | None: ...

    def mark_synced(self, *, org_id: UUID, synced_at: datetime) -> None: ...


class OutboundChannelAdapterPort(Protocol):
    def is_ready(self, *, org_id: UUID) -> bool: ...

    def enqueue(
        self,
        *,
        prospect: Any,
        campaign: Any,
        campaign_run: int,
    ) -> Any: ...

    def retry_failed(self, *, campaign: Any) -> int: ...

    def campaign_metrics(
        self,
        *,
        org_id: UUID,
        campaign_id: UUID,
    ) -> Mapping[str, Any]: ...


class ResearchResultSinkAdapterPort(Protocol):
    def is_ready(self, *, org_id: UUID) -> bool: ...

    def enqueue(self, *, intake: Any) -> Any: ...


class ProviderDataGovernanceAdapterPort(Protocol):
    def anonymize_intake_data(
        self,
        *,
        org_id: UUID,
        intake_id: UUID,
        marker: str,
    ) -> Mapping[str, int]: ...


_PROSPECT_SOURCE_ADAPTERS: dict[str, ProspectSourceAdapterPort] = {}
_OUTBOUND_CHANNEL_ADAPTERS: dict[str, OutboundChannelAdapterPort] = {}
_RESEARCH_RESULT_SINK_ADAPTERS: dict[str, ResearchResultSinkAdapterPort] = {}
_PROVIDER_DATA_GOVERNANCE_ADAPTERS: dict[str, ProviderDataGovernanceAdapterPort] = {}
_EXECUTION_SAFETY_ADAPTER: ExecutionSafetyAdapterPort | None = None


def register_prospect_source_adapter(
    provider: str,
    adapter: ProspectSourceAdapterPort,
) -> None:
    _PROSPECT_SOURCE_ADAPTERS[provider] = adapter


def register_outbound_channel_adapter(
    channel: str,
    adapter: OutboundChannelAdapterPort,
) -> None:
    _OUTBOUND_CHANNEL_ADAPTERS[channel] = adapter


def register_research_result_sink_adapter(
    provider: str,
    adapter: ResearchResultSinkAdapterPort,
) -> None:
    _RESEARCH_RESULT_SINK_ADAPTERS[provider] = adapter


def register_provider_data_governance_adapter(
    provider: str,
    adapter: ProviderDataGovernanceAdapterPort,
) -> None:
    _PROVIDER_DATA_GOVERNANCE_ADAPTERS[provider] = adapter


def register_execution_safety_adapter(adapter: ExecutionSafetyAdapterPort) -> None:
    global _EXECUTION_SAFETY_ADAPTER
    _EXECUTION_SAFETY_ADAPTER = adapter


def _execution_safety_adapter() -> ExecutionSafetyAdapterPort:
    if _EXECUTION_SAFETY_ADAPTER is None:
        raise ExecutionSafetyError(
            code="execution_safety_unavailable",
            detail="External execution safety is unavailable.",
            status_code=503,
        )
    return _EXECUTION_SAFETY_ADAPTER


def assert_provider_io_authorized(
    *,
    org: Any | None = None,
    channel: str,
    action: str,
    execution_request_id: UUID | None = None,
) -> None:
    _execution_safety_adapter().assert_provider_io_authorized(
        org=org,
        channel=channel,
        action=action,
        execution_request_id=execution_request_id,
    )


def hash_target_identifier(*, org: Any, channel: str, identifier: str) -> str:
    return _execution_safety_adapter().hash_target_identifier(
        org=org,
        channel=channel,
        identifier=identifier,
    )


def reserve_execution(
    *,
    org: Any,
    channel: str,
    action: str,
    target_hash: str,
    payload_hash: str,
    units: int,
    approval_id: UUID,
    idempotency_key: UUID,
) -> ExecutionReservation:
    return _execution_safety_adapter().reserve_execution(
        org=org,
        channel=channel,
        action=action,
        target_hash=target_hash,
        payload_hash=payload_hash,
        units=units,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
    )


def external_execution_request(
    *,
    request_id: UUID,
    org: Any | None = None,
    org_id: UUID | None = None,
    status: str | None = None,
    for_update: bool = False,
    include_org: bool = False,
) -> ExternalExecutionRequestPort | None:
    return _execution_safety_adapter().get_request(
        request_id=request_id,
        org=org,
        org_id=org_id,
        status=status,
        for_update=for_update,
        include_org=include_org,
    )


def mark_execution_sending(
    *,
    org: Any,
    request_id: UUID,
    expected_status: str | None = None,
) -> ExternalExecutionRequestPort:
    return _execution_safety_adapter().mark_sending(
        org=org,
        request_id=request_id,
        expected_status=expected_status,
    )


def mark_provider_accepted(
    *,
    org: Any,
    request_id: UUID,
    local_state_uncertain: bool = False,
) -> ExternalExecutionRequestPort:
    return _execution_safety_adapter().mark_provider_accepted(
        org=org,
        request_id=request_id,
        local_state_uncertain=local_state_uncertain,
    )


def release_execution(
    *,
    org: Any,
    request_id: UUID,
    error_code: str,
    expected_status: str | None = None,
) -> ExternalExecutionRequestPort:
    return _execution_safety_adapter().release(
        org=org,
        request_id=request_id,
        error_code=error_code,
        expected_status=expected_status,
    )


def mark_execution_delivered(
    *,
    org: Any,
    request_id: UUID,
) -> ExternalExecutionRequestPort:
    return _execution_safety_adapter().mark_delivered(
        org=org,
        request_id=request_id,
    )


def reconcile_stale_reserved(
    *,
    org: Any,
    older_than: datetime,
    limit: int = 100,
) -> list[UUID]:
    return _execution_safety_adapter().reconcile_stale_reserved(
        org=org,
        older_than=older_than,
        limit=limit,
    )


def reconcile_stale_sending(
    *,
    org: Any,
    older_than: datetime,
    limit: int = 100,
) -> list[UUID]:
    return _execution_safety_adapter().reconcile_stale_sending(
        org=org,
        older_than=older_than,
        limit=limit,
    )


def prospect_source_adapter(provider: str) -> ProspectSourceAdapterPort:
    try:
        return _PROSPECT_SOURCE_ADAPTERS[provider]
    except KeyError as exc:
        raise ProviderAdapterUnavailable(
            f"The {provider} prospect source adapter is unavailable."
        ) from exc


def outbound_channel_adapter(channel: str) -> OutboundChannelAdapterPort:
    try:
        return _OUTBOUND_CHANNEL_ADAPTERS[channel]
    except KeyError as exc:
        raise ProviderAdapterUnavailable(
            f"The {channel} outbound channel adapter is unavailable."
        ) from exc


def research_result_sink_adapter(provider: str) -> ResearchResultSinkAdapterPort:
    try:
        return _RESEARCH_RESULT_SINK_ADAPTERS[provider]
    except KeyError as exc:
        raise ProviderAdapterUnavailable(
            f"The {provider} research-result sink adapter is unavailable."
        ) from exc


def provider_data_governance_adapters() -> tuple[
    ProviderDataGovernanceAdapterPort, ...
]:
    return tuple(_PROVIDER_DATA_GOVERNANCE_ADAPTERS.values())
