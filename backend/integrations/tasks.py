"""Asynchronous provider jobs kept behind idempotent SDR intake services."""

from collections.abc import Mapping
from typing import Any

from celery import shared_task

from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.service import process_facebook_lead_event
from sdr.services import IntakeProcessingFailed


@shared_task(bind=True, max_retries=5)
def process_facebook_lead(self, event_payload: Mapping[str, Any]):
    try:
        result = process_facebook_lead_event(event_payload=event_payload)
    except FacebookGraphAPIError as exc:
        if not exc.retryable:
            raise
        raise self.retry(exc=exc, countdown=_retry_delay(self.request.retries)) from exc
    except IntakeProcessingFailed as exc:
        raise self.retry(exc=exc, countdown=_retry_delay(self.request.retries)) from exc
    return {
        "intake_id": str(result.intake_id),
        "lead_id": str(result.lead_id) if result.lead_id else None,
        "replayed": result.replayed,
    }


def _retry_delay(retry_count: int) -> int:
    return min(300, 2 ** (retry_count + 1))
