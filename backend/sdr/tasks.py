"""Periodic recovery for the SDR response outbox."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from automation.tenant_context import database_org_context
from common.models import Org
from sdr.compliance import scan_retention
from sdr.nurture import reconcile_nurture_jobs
from sdr.outbound import reconcile_outbound_campaigns
from sdr.response import reconcile_recent_response_jobs
from sdr.sources import reconcile_apollo_candidate_states, reconcile_outbound_sources

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


@shared_task(name="sdr.reconcile_nurture_jobs")
def reconcile_all_nurture_jobs():
    reconciled = 0
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            try:
                reconciled += reconcile_nurture_jobs(org_id=org_id)
            except Exception:
                logger.exception(
                    "Could not reconcile SDR nurture jobs for org %s", org_id
                )
    return {"reconciled": reconciled}


@shared_task(name="sdr.reconcile_outbound_campaigns")
def reconcile_all_outbound_campaigns():
    queued = 0
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            try:
                queued += reconcile_outbound_campaigns(org_id=org_id)
            except Exception:
                logger.exception(
                    "Could not reconcile SDR outbound campaigns for org %s",
                    org_id,
                )
    return {"queued": queued}


@shared_task(name="sdr.reconcile_outbound_sources")
def reconcile_all_outbound_sources():
    queued = 0
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            try:
                queued += reconcile_outbound_sources(org_id=org_id)
            except Exception:
                logger.exception(
                    "Could not reconcile SDR outbound sources for org %s",
                    org_id,
                )
    return {"queued": queued}


@shared_task(name="sdr.reconcile_apollo_candidate_states")
def reconcile_all_apollo_candidate_states():
    """Recover stale Apollo requests and import projections without network I/O."""

    totals = {
        "released_requests": 0,
        "unknown_requests": 0,
        "candidates_released": 0,
        "candidates_unknown": 0,
        "candidates_imported": 0,
        "candidates_import_review_required": 0,
        "candidates_import_failed": 0,
        "candidates_import_retry_required": 0,
    }
    now = timezone.now()
    reserved_before = now - timedelta(
        seconds=settings.CHANNEL_EXECUTION_RESERVED_TIMEOUT_SECONDS
    )
    sending_before = now - timedelta(
        seconds=settings.CHANNEL_EXECUTION_SENDING_TIMEOUT_SECONDS
    )
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            try:
                org = Org.objects.get(id=org_id)
                result = reconcile_apollo_candidate_states(
                    org=org,
                    reserved_before=reserved_before,
                    sending_before=sending_before,
                )
                for key in totals:
                    totals[key] += result[key]
            except Exception:
                logger.exception(
                    "Could not reconcile Apollo candidate states for org %s",
                    org_id,
                )
    return totals


@shared_task(name="sdr.scan_compliance_retention")
def scan_all_compliance_retention():
    due = 0
    anonymized = 0
    for org_id in Org.objects.values_list("id", flat=True).iterator(chunk_size=100):
        with database_org_context(org_id):
            try:
                result = scan_retention(org_id=org_id, execute=True)
                due += result["due"]
                anonymized += result["anonymized"]
            except Exception:
                logger.exception(
                    "Could not scan SDR compliance retention for org %s",
                    org_id,
                )
    return {"due": due, "anonymized": anonymized}
