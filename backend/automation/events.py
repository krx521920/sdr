"""Tenant-aware events exchanged between bounded contexts."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    org_id: UUID
    name: str
    aggregate_id: UUID | str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventPublisher(Protocol):
    def publish(self, event: EventEnvelope) -> None: ...
