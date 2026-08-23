"""Periodic recovery for the SDR response outbox."""

import logging

from celery import shared_task

from automation.tenant_context import database_org_context
from common.models import Org
from sdr.compliance import scan_retention
from sdr.nurture import reconcile_nurture_jobs
from sdr.outbound import reconcile_outbound_campaigns
from sdr.response import reconcile_recent_response_jobs
from sdr.sources import reconcile_outbound_sources

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
