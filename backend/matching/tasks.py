"""Periodic tenant-isolated governance retention and expiry scans."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from automation.tenant_context import database_org_context
from common.models import Org
from matching.governance import scan_governance_retention
from matching.import_pipeline import expire_stale_import_previews

logger = logging.getLogger(__name__)


@shared_task(name="matching.scan_governance_retention")
def scan_all_governance_retention():
    totals = {
        "due": 0,
        "restricted": 0,
        "anonymized": 0,
        "expired": 0,
        "recomputed": 0,
        "failed_orgs": 0,
    }
    org_ids = Org.objects.values_list("id", flat=True).iterator(chunk_size=100)
    for org_id in org_ids:
        with database_org_context(org_id):
            try:
                org = Org.objects.get(id=org_id)
                result = scan_governance_retention(
                    org=org,
                    execute=True,
                    limit=500,
                    actor=None,
                )
                for field in (
                    "due",
                    "restricted",
                    "anonymized",
                    "expired",
                    "recomputed",
                ):
                    totals[field] += int(result.get(field, 0))
            except Exception:
                totals["failed_orgs"] += 1
                logger.exception(
                    "Could not scan matching governance retention for org %s",
                    org_id,
                )
    return totals


@shared_task(name="matching.expire_stale_import_previews")
def expire_all_stale_import_previews():
    """Scrub bounded, uncommitted import staging data tenant by tenant."""

    retention_days = settings.MATCHING_IMPORT_PREVIEW_RETENTION_DAYS
    older_than = timezone.now() - timedelta(days=retention_days)
    totals = {
        "expired_count": 0,
        "failed_orgs": 0,
    }
    org_ids = Org.objects.values_list("id", flat=True).iterator(chunk_size=100)
    for org_id in org_ids:
        with database_org_context(org_id):
            try:
                org = Org.objects.get(id=org_id)
                result = expire_stale_import_previews(
                    org=org,
                    older_than=older_than,
                    limit=500,
                )
                totals["expired_count"] += int(result["expired_count"])
            except Exception:
                totals["failed_orgs"] += 1
                logger.exception(
                    "Could not expire stale matching import previews for org %s",
                    org_id,
                )
    return totals
