"""Provider feedback hooks for CRM state transitions."""

import logging
from functools import partial

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from integrations.providers.facebook.conversions import (
    schedule_converted_event_for_lead,
)
from leads.models import Lead

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Lead, dispatch_uid="facebook_conversion_capture_lead_status")
def capture_previous_lead_status(sender, instance: Lead, **kwargs) -> None:
    if not instance.pk or instance._state.adding:
        instance._facebook_previous_status = None
        return
    instance._facebook_previous_status = (
        sender.objects.filter(id=instance.pk, org_id=instance.org_id)
        .values_list("status", flat=True)
        .first()
    )


@receiver(post_save, sender=Lead, dispatch_uid="facebook_conversion_lead_status")
def queue_converted_lead_feedback(
    sender,
    instance: Lead,
    created: bool,
    **kwargs,
) -> None:
    previous = getattr(instance, "_facebook_previous_status", None)
    current = (instance.status or "").strip().lower()
    previous_normalized = (previous or "").strip().lower()
    if created or current != "converted" or previous_normalized == current:
        return
    transaction.on_commit(
        partial(
            _schedule_converted_safely,
            org_id=instance.org_id,
            lead_id=instance.id,
            event_time=instance.updated_at,
        )
    )


def _schedule_converted_safely(*, org_id, lead_id, event_time) -> None:
    try:
        schedule_converted_event_for_lead(
            org_id=org_id,
            lead_id=lead_id,
            event_time=event_time,
        )
    except Exception:
        logger.exception("Could not schedule Meta Converted event for lead %s", lead_id)
