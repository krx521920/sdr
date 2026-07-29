"""Periodic recovery for the SDR response outbox."""

import logging

from celery import shared_task

from automation.tenant_context import database_org_context
from common.models import Org
from sdr.response import reconcile_recent_response_jobs

logger = logging.getLogger(__name__)


@shared_task(name="sdr.reconcile_response_jobs")
def reconcile_response_jobs():
    reconciled = 0
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            try:
                reconciled += reconcile_recent_response_jobs(org_id=org_id)
            except Exception:
                logger.exception(
                    "Could not reconcile SDR response jobs for org %s", org_id
                )
    return {"reconciled": reconciled}
