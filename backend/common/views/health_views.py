"""Process health endpoints used by container and load-balancer probes."""

import logging

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

_BROKER_TIMEOUT_SECONDS = 1


def _database_is_ready() -> bool:
    """Confirm that Django can execute a minimal query on the primary database."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone() == (1,)


def _broker_is_ready() -> bool:
    """Ping the Redis instance configured as the Celery broker."""
    broker_url = getattr(settings, "CELERY_BROKER_URL", "")
    if not broker_url:
        return False

    client = redis.Redis.from_url(
        broker_url,
        socket_connect_timeout=_BROKER_TIMEOUT_SECONDS,
        socket_timeout=_BROKER_TIMEOUT_SECONDS,
    )
    try:
        return client.ping() is True
    finally:
        client.close()


def _run_check(name: str, check) -> str:
    try:
        return "ok" if check() else "failed"
    except Exception as exc:  # Probe failures must produce a controlled response.
        # Log only the exception type: broker URLs and database errors can contain
        # credentials or other deployment details that must not reach probe output.
        logger.warning("Readiness %s check failed (%s)", name, type(exc).__name__)
        return "failed"


@require_GET
def readyz(request):
    """Report whether the database and Celery broker can accept application work."""
    checks = {
        "database": _run_check("database", _database_is_ready),
        "broker": _run_check("broker", _broker_is_ready),
    }
    is_ready = all(result == "ok" for result in checks.values())
    response = JsonResponse(
        {"status": "ready" if is_ready else "not_ready", "checks": checks},
        status=200 if is_ready else 503,
    )
    response["Cache-Control"] = "no-store"
    return response
