from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from common.celery_health import redis_client
from common.tasks import celery_beat_heartbeat
from crm.celery import app


class FakeRedis:
    def __init__(self, value=None, get_error=None):
        self.value = value
        self.get_error = get_error
        self.set_calls = []
        self.closed = False

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))

    def get(self, key):
        if self.get_error is not None:
            raise self.get_error
        return self.value

    def close(self):
        self.closed = True


def test_beat_schedule_dispatches_heartbeat_every_thirty_seconds():
    entry = app.conf.beat_schedule["celery-beat-heartbeat"]

    assert entry["task"] == "common.tasks.celery_beat_heartbeat"
    assert entry["schedule"] == 30.0
    assert entry["options"]["expires"] < entry["schedule"]


@override_settings(CELERY_BROKER_URL="redis://broker.example.test:6379/4")
def test_heartbeat_client_uses_configured_broker_with_short_timeouts(monkeypatch):
    sentinel = object()
    from_url_calls = []

    def fake_from_url(url, **kwargs):
        from_url_calls.append((url, kwargs))
        return sentinel

    monkeypatch.setattr("common.celery_health.Redis.from_url", fake_from_url)

    assert redis_client() is sentinel
    assert from_url_calls == [
        (
            "redis://broker.example.test:6379/4",
            {"socket_connect_timeout": 2, "socket_timeout": 2},
        )
    ]


@override_settings(
    CELERY_BEAT_HEARTBEAT_KEY="test:celery:beat:heartbeat",
    CELERY_BEAT_HEARTBEAT_TTL_SECONDS=123,
)
def test_heartbeat_task_writes_timestamp_with_ttl(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr("common.tasks.redis_client", lambda: client)
    monkeypatch.setattr("common.tasks.time.time", lambda: 1_725_000_000.25)

    result = celery_beat_heartbeat.run()

    assert result == 1_725_000_000.25
    assert client.set_calls == [
        (
            ("test:celery:beat:heartbeat", "1725000000.250000"),
            {"ex": 123},
        )
    ]
    assert client.closed is True


def test_check_command_accepts_fresh_heartbeat(monkeypatch):
    client = FakeRedis(value=b"1725000000.0")
    monkeypatch.setattr(
        "common.management.commands.check_celery_beat.redis_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "common.management.commands.check_celery_beat.time.time",
        lambda: 1_725_000_030.0,
    )
    output = StringIO()

    call_command("check_celery_beat", max_age=60, stdout=output)

    assert "heartbeat_status=healthy" in output.getvalue()
    assert "age_seconds=30.000" in output.getvalue()
    assert client.closed is True


@pytest.mark.parametrize(
    ("value", "now", "reason"),
    [
        (None, 1_725_000_030.0, "missing"),
        (b"not-a-timestamp", 1_725_000_030.0, "invalid"),
        (b"1725000000.0", 1_725_000_091.0, "stale"),
        (b"1725000031.0", 1_725_000_030.0, "future_timestamp"),
    ],
)
def test_check_command_rejects_unhealthy_heartbeat(monkeypatch, value, now, reason):
    client = FakeRedis(value=value)
    monkeypatch.setattr(
        "common.management.commands.check_celery_beat.redis_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "common.management.commands.check_celery_beat.time.time", lambda: now
    )

    with pytest.raises(CommandError, match=f"reason={reason}"):
        call_command("check_celery_beat", max_age=60)

    assert client.closed is True


def test_check_command_reports_redis_failure_without_leaking_url(monkeypatch):
    client = FakeRedis(get_error=RuntimeError("connection failed"))
    monkeypatch.setattr(
        "common.management.commands.check_celery_beat.redis_client",
        lambda: client,
    )

    with pytest.raises(CommandError, match="reason=redis_unavailable") as error:
        call_command("check_celery_beat", max_age=60)

    assert "redis://" not in str(error.value)
    assert client.closed is True


@pytest.mark.parametrize("max_age", [0, -1, float("inf"), float("nan")])
def test_check_command_rejects_invalid_max_age_before_connecting(max_age):
    with pytest.raises(CommandError, match="max-age must be"):
        call_command("check_celery_beat", max_age=max_age)
