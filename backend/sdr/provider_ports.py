"""Runtime ports registered by concrete provider applications at startup."""

from __future__ import annotations

from collections.abc import Mapping
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


class ProspectSourceClientPort(Protocol):
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
_PROVIDER_DATA_GOVERNANCE_ADAPTERS: dict[
    str, ProviderDataGovernanceAdapterPort
] = {}


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
