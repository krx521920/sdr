"""Check that Celery beat messages are reaching a worker through Redis."""

from __future__ import annotations

import math
import time

from django.core.management.base import BaseCommand, CommandError

from common.celery_health import (
    heartbeat_key,
    heartbeat_max_age_seconds,
    redis_client,
)


class Command(BaseCommand):
    help = "Check the freshness of the Celery beat-to-worker Redis heartbeat."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age",
            type=float,
            default=heartbeat_max_age_seconds(),
            help=(
                "Maximum acceptable heartbeat age in seconds "
                "(default: CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS or 90)."
            ),
        )

    def handle(self, *args, **options):
        max_age = float(options["max_age"])
        if not math.isfinite(max_age) or max_age <= 0:
            raise CommandError("max-age must be a finite number greater than zero.")

        client = None
        try:
            client = redis_client()
            raw_heartbeat = client.get(heartbeat_key())
        except Exception as exc:
            raise CommandError(
                "heartbeat_status=unhealthy reason=redis_unavailable"
            ) from exc
        finally:
            if client is not None:
                client.close()

        if raw_heartbeat is None:
            raise CommandError("heartbeat_status=unhealthy reason=missing")

        try:
            written_at = float(raw_heartbeat)
        except (TypeError, ValueError) as exc:
            raise CommandError("heartbeat_status=unhealthy reason=invalid") from exc
        if not math.isfinite(written_at):
            raise CommandError("heartbeat_status=unhealthy reason=invalid")

        age = time.time() - written_at
        if age < 0:
            raise CommandError(
                "heartbeat_status=unhealthy reason=future_timestamp "
                f"age_seconds={age:.3f}"
            )
        if age > max_age:
            raise CommandError(
                "heartbeat_status=unhealthy reason=stale "
                f"age_seconds={age:.3f} max_age_seconds={max_age:g}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "heartbeat_status=healthy "
                f"age_seconds={age:.3f} max_age_seconds={max_age:g}"
            )
        )
