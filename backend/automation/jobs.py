"""Idempotent background job contracts."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID


@dataclass(frozen=True, slots=True)
class JobRequest:
    org_id: UUID
    name: str
    idempotency_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    queue: str = "default"
    max_attempts: int = 3
    scheduled_for: datetime | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
