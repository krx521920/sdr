from __future__ import absolute_import, unicode_literals

import os

from celery import Celery
from celery.schedules import crontab
from django.conf import settings

# set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "crm.settings")
# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crm.dev_settings')
# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crm.server_settings')

app = Celery("crm")

# Using a string here means the worker don't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()
app.autodiscover_tasks(related_name="celery_tasks")  # tasks app uses celery_tasks.py

# Celery Beat Schedule for recurring tasks
app.conf.beat_schedule = {
    # Prove that beat can publish to the broker and a worker can consume the
    # message. Expire delayed deliveries before they can create a false-fresh
    # heartbeat after a queue backlog.
    "celery-beat-heartbeat": {
        "task": "common.tasks.celery_beat_heartbeat",
        "schedule": 30.0,
        "options": {"expires": 25.0},
    },
    # Generate invoices from recurring invoice templates - daily at midnight
    "generate-recurring-invoices": {
        "task": "invoices.tasks.generate_recurring_invoices",
        "schedule": crontab(hour=0, minute=0),
    },
    # Mark overdue invoices - daily at 1 AM
    "check-overdue-invoices": {
        "task": "invoices.tasks.check_overdue_invoices",
        "schedule": crontab(hour=1, minute=0),
    },
    # Process payment reminders - daily at 9 AM
    "process-payment-reminders": {
        "task": "invoices.tasks.process_payment_reminders",
        "schedule": crontab(hour=9, minute=0),
    },
    # Mark expired estimates - daily at midnight
    "check-expired-estimates": {
        "task": "invoices.tasks.check_expired_estimates",
        "schedule": crontab(hour=0, minute=30),
    },
    # Check for stale/rotten opportunities - daily at 8 AM
    "check-stale-opportunities": {
        "task": "opportunity.tasks.check_stale_opportunities",
        "schedule": crontab(hour=8, minute=0),
    },
    # Check goal milestones and send notifications - daily at 9:15 AM
    "check-goal-milestones": {
        "task": "opportunity.tasks.check_goal_milestones",
        "schedule": crontab(hour=9, minute=15),
    },
    # Scan cases for SLA breach and fire configured escalations - every 5 minutes
    "scan-for-breached-cases": {
        "task": "cases.tasks.scan_for_breached_cases",
        "schedule": crontab(minute="*/5"),
    },
    # Purge already-read in-app notifications older than 90 days - daily at 3 AM
    "purge-read-notifications": {
        "task": "common.tasks.purge_read_notifications",
        "schedule": crontab(hour=3, minute=0),
    },
    # Stop forgotten time-tracking timers older than 12 hours - every 30 minutes
    "auto-stop-stale-timers": {
        "task": "cases.tasks.auto_stop_stale_timers",
        "schedule": crontab(minute="*/30"),
    },
    # Drop rotated/expired refresh token records - daily at 3:30 AM
    "flush-expired-refresh-tokens": {
        "task": "common.tasks.flush_expired_refresh_tokens",
        "schedule": crontab(hour=3, minute=30),
    },
    # Recover persisted automation jobs if a broker publish or worker lease was
    # lost. Idempotent claims make duplicate deliveries safe.
    "dispatch-due-automation-jobs": {
        "task": "automation.dispatch_due_jobs",
        "schedule": crontab(minute="*"),
    },
    "reconcile-sdr-response-jobs": {
        "task": "sdr.reconcile_response_jobs",
        "schedule": crontab(minute="*"),
    },
    "reconcile-sdr-nurture-jobs": {
        "task": "sdr.reconcile_nurture_jobs",
        "schedule": crontab(minute="*/5"),
    },
    "reconcile-sdr-outbound-campaigns": {
        "task": "sdr.reconcile_outbound_campaigns",
        "schedule": crontab(minute="*/15"),
    },
    "reconcile-sdr-outbound-sources": {
        "task": "sdr.reconcile_outbound_sources",
        "schedule": crontab(minute="*/15"),
    },
    # Release calls that never reached provider I/O, conservatively mark stale
    # in-flight calls UNKNOWN, and project Person-import terminal states.
    "reconcile-sdr-apollo-candidate-states": {
        "task": "sdr.reconcile_apollo_candidate_states",
        "schedule": crontab(minute="*/5"),
    },
    "scan-sdr-compliance-retention": {
        "task": "sdr.scan_compliance_retention",
        "schedule": crontab(hour=2, minute=15),
    },
    "scan-matching-governance-retention": {
        "task": "matching.scan_governance_retention",
        "schedule": crontab(hour=2, minute=30),
    },
    # Uncommitted import previews contain staging-only identity data. Scrub a
    # bounded page each hour so high-volume inboxes cannot build an indefinite
    # privacy backlog.
    "expire-stale-matching-import-previews": {
        "task": "matching.expire_stale_import_previews",
        "schedule": crontab(minute=45),
    },
}

# These inherited CRM jobs predate the durable AutomationJob ledger. Keep them
# available in development, but fail closed in production until each workflow
# has idempotent side-effect records and crash-safe retry semantics.
if not settings.ENABLE_LEGACY_CRM_BEAT_TASKS:
    for legacy_task_name in (
        "generate-recurring-invoices",
        "check-overdue-invoices",
        "process-payment-reminders",
        "check-expired-estimates",
        "check-stale-opportunities",
        "check-goal-milestones",
        "scan-for-breached-cases",
        "purge-read-notifications",
        "auto-stop-stale-timers",
        "flush-expired-refresh-tokens",
    ):
        app.conf.beat_schedule.pop(legacy_task_name, None)
