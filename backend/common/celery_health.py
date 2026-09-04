"""Redis-backed liveness state for the Celery beat-to-worker path."""

from __future__ import annotations

from django.conf import settings
from redis import Redis

DEFAULT_HEARTBEAT_KEY = "crm:celery:beat:heartbeat"
DEFAULT_HEARTBEAT_TTL_SECONDS = 120
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 90.0


def heartbeat_key() -> str:
    """Return the deployment-specific, non-tenant heartbeat key."""
    return getattr(settings, "CELERY_BEAT_HEARTBEAT_KEY", DEFAULT_HEARTBEAT_KEY)


def heartbeat_ttl_seconds() -> int:
    """Return how long Redis retains a heartbeat after the worker writes it."""
    return int(
        getattr(
            settings,
            "CELERY_BEAT_HEARTBEAT_TTL_SECONDS",
            DEFAULT_HEARTBEAT_TTL_SECONDS,
        )
    )


def heartbeat_max_age_seconds() -> float:
    """Return the default age threshold used by the health-check command."""
    return float(
        getattr(
            settings,
            "CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS",
            DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
        )
    )


def redis_client() -> Redis:
    """Build a short-timeout client for the Redis instance used as broker."""
    return Redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
