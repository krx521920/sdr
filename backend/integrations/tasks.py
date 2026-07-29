"""Compatibility Celery entry points that now create durable automation jobs."""

from collections.abc import Mapping
from typing import Any

from celery import shared_task

from integrations.providers.facebook.jobs import enqueue_facebook_lead_event


@shared_task
def process_facebook_lead(event_payload: Mapping[str, Any]):
    job = enqueue_facebook_lead_event(event_payload)
    return {
        "job_id": str(job.id),
        "status": job.status,
    }
