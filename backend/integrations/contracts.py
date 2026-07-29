"""Contracts shared by inbound lead provider adapters."""

from typing import Mapping, Protocol, Sequence

from sdr.domain import LeadCandidate, LeadSource
from sdr.ports import LeadSourcePort


class WebhookLeadSourcePort(LeadSourcePort, Protocol):
    provider: LeadSource

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> bool: ...

    def parse_webhook(
        self, *, headers: Mapping[str, str], body: bytes
    ) -> Sequence[LeadCandidate]: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[LeadSource, LeadSourcePort] = {}

    def register(self, source: LeadSource, provider: LeadSourcePort) -> None:
        if source in self._providers:
            raise ValueError(f"provider already registered for {source}")
        self._providers[source] = provider

    def get(self, source: LeadSource) -> LeadSourcePort:
        try:
            return self._providers[source]
        except KeyError as exc:
            raise LookupError(f"no provider registered for {source}") from exc

    def available_sources(self) -> tuple[LeadSource, ...]:
        return tuple(self._providers)
