"""Organization and campaign safety gates for outbound nurture email."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytz
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from sdr.models import (
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    OutboundCampaignStatus,
    OutboundProspectStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRResponseSettings,
)

CAMPAIGN_SAFETY_PAUSE_REASON = "Outbound campaign paused by email safety controls."


@dataclass(frozen=True, slots=True)
class DeliverySafetyDecision:
    allowed: bool
    reason: str = ""
    next_attempt_at: datetime | None = None
    used_today: int = 0
    daily_limit: int = 0


def reserve_delivery_send(
    delivery: LeadNurtureDelivery,
    *,
    now: datetime | None = None,
) -> DeliverySafetyDecision:
    """Atomically reserve one organization send slot or defer the delivery."""

    now = now or timezone.now()
    configuration, _ = SDRResponseSettings.objects.get_or_create(org_id=delivery.org_id)
    campaign = _campaign_for_delivery(delivery)
    if campaign and campaign.safety_hold:
        return DeliverySafetyDecision(
            allowed=False,
            reason="campaign_safety_hold",
        )

    if configuration.email_safety_enabled:
        next_window = next_allowed_send_at(
            delivery,
            configuration=configuration,
            earliest=now,
        )
        if next_window > now:
            _defer_delivery(
                delivery,
                next_attempt_at=next_window,
                reason="recipient_working_hours",
            )
            return DeliverySafetyDecision(
                allowed=False,
                reason="recipient_working_hours",
                next_attempt_at=next_window,
                daily_limit=configuration.org_daily_send_limit,
            )

    with transaction.atomic():
        configuration = SDRResponseSettings.objects.select_for_update().get(
            org_id=delivery.org_id
        )
        locked = LeadNurtureDelivery.objects.select_for_update().get(
            id=delivery.id,
            org_id=delivery.org_id,
        )
        if locked.status == NurtureDeliveryStatus.SENT:
            return DeliverySafetyDecision(allowed=True)

        used_today = 0
        if configuration.email_safety_enabled:
            day_start, day_end = _organization_day_bounds(configuration, now)
            used_today = (
                LeadNurtureDelivery.objects.filter(org_id=delivery.org_id)
                .exclude(id=delivery.id)
                .filter(
                    Q(
                        status=NurtureDeliveryStatus.SENT,
                        sent_at__gte=day_start,
                        sent_at__lt=day_end,
                    )
                    | Q(
                        status=NurtureDeliveryStatus.SENDING,
                        updated_at__gte=day_start,
                        updated_at__lt=day_end,
                    )
                )
                .count()
            )
            if used_today >= configuration.org_daily_send_limit:
                next_attempt = next_allowed_send_at(
                    locked,
                    configuration=configuration,
                    earliest=day_end,
                )
                _defer_locked_delivery(
                    locked,
                    next_attempt_at=next_attempt,
                    reason="organization_daily_send_limit",
                )
                _copy_delivery_state(delivery, locked)
                return DeliverySafetyDecision(
                    allowed=False,
                    reason="organization_daily_send_limit",
                    next_attempt_at=next_attempt,
                    used_today=used_today,
                    daily_limit=configuration.org_daily_send_limit,
                )

        locked.status = NurtureDeliveryStatus.SENDING
        locked.attempt_count += 1
        locked.last_error_code = ""
        locked.last_error_message = ""
        locked.save(
            update_fields=[
                "status",
                "attempt_count",
                "last_error_code",
                "last_error_message",
                "updated_at",
            ]
        )
        _copy_delivery_state(delivery, locked)
        return DeliverySafetyDecision(
            allowed=True,
            used_today=used_today,
            daily_limit=(
                configuration.org_daily_send_limit
                if configuration.email_safety_enabled
                else 0
            ),
        )


def next_allowed_send_at(
    delivery: LeadNurtureDelivery,
    *,
    configuration: SDRResponseSettings,
    earliest: datetime,
) -> datetime:
    if not configuration.enforce_recipient_working_hours:
        return earliest

    recipient_timezone = _recipient_timezone(delivery, configuration)
    local = earliest.astimezone(recipient_timezone)
    weekdays = _safe_weekdays(configuration.recipient_send_weekdays)
    window_start = configuration.recipient_send_window_start
    window_end = configuration.recipient_send_window_end
    if window_start >= window_end:
        return earliest

    for offset in range(8):
        candidate_date = local.date() + timedelta(days=offset)
        if candidate_date.weekday() not in weekdays:
            continue
        start = datetime.combine(candidate_date, window_start, recipient_timezone)
        end = datetime.combine(candidate_date, window_end, recipient_timezone)
        if offset == 0 and start <= local < end:
            return earliest
        if local < start:
            return start.astimezone(earliest.tzinfo)
    return earliest + timedelta(days=1)


def evaluate_campaign_safety(
    delivery: LeadNurtureDelivery,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Pause an active outbound campaign when provider rates breach policy."""

    campaign = _campaign_for_delivery(delivery)
    if campaign is None:
        return None
    configuration, _ = SDRResponseSettings.objects.get_or_create(org_id=delivery.org_id)
    if not configuration.email_safety_enabled:
        return None

    now = now or timezone.now()
    cutoff = now - timedelta(days=configuration.safety_window_days)
    deliveries = LeadNurtureDelivery.objects.filter(
        org_id=delivery.org_id,
        enrollment__intake__outbound_prospect__campaign_id=campaign.id,
        sent_at__gte=cutoff,
    )
    sent = deliveries.count()
    if sent < configuration.safety_min_sample_size:
        return {
            "paused": False,
            "sent": sent,
            "minimum_sample_size": configuration.safety_min_sample_size,
        }

    bounced = deliveries.filter(bounced_at__isnull=False).count()
    complained = deliveries.filter(complained_at__isnull=False).count()
    bounce_rate = _percentage(bounced, sent)
    complaint_rate = _percentage(complained, sent)
    breaches = []
    if (
        configuration.bounce_rate_threshold > 0
        and bounce_rate >= configuration.bounce_rate_threshold
    ):
        breaches.append("bounce_rate")
    if (
        configuration.complaint_rate_threshold > 0
        and complaint_rate >= configuration.complaint_rate_threshold
    ):
        breaches.append("complaint_rate")
    snapshot = {
        "sent": sent,
        "bounced": bounced,
        "complained": complained,
        "bounce_rate": float(bounce_rate),
        "complaint_rate": float(complaint_rate),
        "bounce_rate_threshold": float(configuration.bounce_rate_threshold),
        "complaint_rate_threshold": float(configuration.complaint_rate_threshold),
        "window_days": configuration.safety_window_days,
        "breaches": breaches,
    }
    if not breaches:
        return {"paused": False, **snapshot}

    with transaction.atomic():
        locked = SDROutboundCampaign.objects.select_for_update().get(
            id=campaign.id,
            org_id=campaign.org_id,
        )
        if locked.status != OutboundCampaignStatus.ACTIVE:
            return {"paused": False, **snapshot}
        reason = (
            "Email safety hold: "
            + ", ".join(value.replace("_", " ") for value in breaches)
            + " exceeded the configured threshold."
        )
        locked.status = OutboundCampaignStatus.PAUSED
        locked.safety_hold = True
        locked.safety_paused_at = now
        locked.safety_cleared_at = None
        locked.safety_pause_reason = reason
        locked.safety_snapshot = snapshot
        locked.save(
            update_fields=[
                "status",
                "safety_hold",
                "safety_paused_at",
                "safety_cleared_at",
                "safety_pause_reason",
                "safety_snapshot",
                "updated_at",
            ]
        )
        SDROutboundProspect.objects.filter(
            org_id=locked.org_id,
            campaign=locked,
            status=OutboundProspectStatus.QUEUED,
        ).update(status=OutboundProspectStatus.READY)

    from sdr.nurture import pause_enrollment

    enrollments = LeadNurtureEnrollment.objects.filter(
        org_id=campaign.org_id,
        intake__outbound_prospect__campaign_id=campaign.id,
        status=NurtureEnrollmentStatus.ACTIVE,
    )
    paused_enrollments = 0
    for enrollment in enrollments.iterator(chunk_size=100):
        pause_enrollment(enrollment, reason=CAMPAIGN_SAFETY_PAUSE_REASON)
        paused_enrollments += 1
    return {
        "paused": True,
        "campaign_id": str(campaign.id),
        "paused_enrollments": paused_enrollments,
        **snapshot,
    }


def clear_campaign_safety_hold(campaign: SDROutboundCampaign) -> dict:
    with transaction.atomic():
        locked = SDROutboundCampaign.objects.select_for_update().get(
            id=campaign.id,
            org_id=campaign.org_id,
        )
        if not locked.safety_hold:
            return {"action": "clear_safety_hold", "cleared": False}
        locked.safety_hold = False
        locked.safety_cleared_at = timezone.now()
        locked.save(update_fields=["safety_hold", "safety_cleared_at", "updated_at"])
    return {"action": "clear_safety_hold", "cleared": True}


def _campaign_for_delivery(
    delivery: LeadNurtureDelivery,
) -> SDROutboundCampaign | None:
    return (
        SDROutboundCampaign.objects.filter(
            org_id=delivery.org_id,
            prospects__intake_id=delivery.enrollment.intake_id,
        )
        .distinct()
        .first()
    )


def _recipient_timezone(
    delivery: LeadNurtureDelivery,
    configuration: SDRResponseSettings,
) -> ZoneInfo:
    prospect = getattr(delivery.enrollment.intake, "outbound_prospect", None)
    timezone_name = getattr(prospect, "recipient_timezone", "") if prospect else ""
    if not timezone_name and prospect and prospect.country:
        choices = pytz.country_timezones.get(prospect.country.upper(), ())
        timezone_name = choices[0] if choices else ""
    timezone_name = timezone_name or configuration.default_recipient_timezone or "UTC"
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def _organization_day_bounds(
    configuration: SDRResponseSettings,
    now: datetime,
) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(configuration.default_recipient_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("UTC")
    local = now.astimezone(zone)
    start = datetime.combine(local.date(), time.min, zone)
    end = datetime.combine(local.date() + timedelta(days=1), time.min, zone)
    return start.astimezone(now.tzinfo), end.astimezone(now.tzinfo)


def _safe_weekdays(value) -> set[int]:
    if not isinstance(value, list):
        return {0, 1, 2, 3, 4}
    cleaned = {day for day in value if isinstance(day, int) and 0 <= day <= 6}
    return cleaned or {0, 1, 2, 3, 4}


def _defer_delivery(
    delivery: LeadNurtureDelivery,
    *,
    next_attempt_at: datetime,
    reason: str,
) -> None:
    with transaction.atomic():
        locked = LeadNurtureDelivery.objects.select_for_update().get(
            id=delivery.id,
            org_id=delivery.org_id,
        )
        _defer_locked_delivery(
            locked,
            next_attempt_at=next_attempt_at,
            reason=reason,
        )
        _copy_delivery_state(delivery, locked)


def _defer_locked_delivery(
    delivery: LeadNurtureDelivery,
    *,
    next_attempt_at: datetime,
    reason: str,
) -> None:
    delivery.status = NurtureDeliveryStatus.PENDING
    delivery.scheduled_for = next_attempt_at
    delivery.deferral_count += 1
    delivery.last_error_code = reason[:80]
    delivery.last_error_message = ""
    delivery.save(
        update_fields=[
            "status",
            "scheduled_for",
            "deferral_count",
            "last_error_code",
            "last_error_message",
            "updated_at",
        ]
    )
    LeadNurtureEnrollment.objects.filter(
        id=delivery.enrollment_id,
        org_id=delivery.org_id,
    ).update(next_run_at=next_attempt_at)


def _copy_delivery_state(
    target: LeadNurtureDelivery,
    source: LeadNurtureDelivery,
) -> None:
    for field in (
        "status",
        "attempt_count",
        "deferral_count",
        "scheduled_for",
        "last_error_code",
        "last_error_message",
    ):
        setattr(target, field, getattr(source, field))


def _percentage(value: int, total: int) -> Decimal:
    if not total:
        return Decimal("0")
    return (Decimal(value) * Decimal("100") / Decimal(total)).quantize(Decimal("0.01"))
