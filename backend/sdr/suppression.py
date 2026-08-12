"""Tenant-scoped SDR email suppression and signed unsubscribe tokens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from sdr.models import (
    EmailSuppressionReason,
    EmailSuppressionSource,
    LeadLifecycleEventType,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureEnrollmentStatus,
    SDREmailSuppression,
)
from sdr.response import record_lifecycle_event

UNSUBSCRIBE_SALT = "sdr.nurture.unsubscribe.v1"
UNSUBSCRIBE_VERSION = 1
SUPPRESSIBLE_ENROLLMENT_STATUSES = (
    NurtureEnrollmentStatus.ACTIVE,
    NurtureEnrollmentStatus.PAUSED,
    NurtureEnrollmentStatus.COMPLETED,
)


@dataclass(frozen=True)
class UnsubscribeEvent:
    org_id: UUID
    delivery_id: UUID


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ValueError("A valid email address is required.") from exc
    return email


def is_email_suppressed(*, org_id: UUID, email: str) -> bool:
    try:
        normalized = normalize_email(email)
    except ValueError:
        return True
    return SDREmailSuppression.objects.filter(
        org_id=org_id,
        email__iexact=normalized,
        is_active=True,
    ).exists()


@transaction.atomic
def suppress_email(
    *,
    org_id: UUID,
    email: str,
    reason: str,
    source: str,
    source_delivery: LeadNurtureDelivery | None = None,
    details: dict[str, Any] | None = None,
) -> tuple[SDREmailSuppression, bool]:
    normalized = normalize_email(email)
    if reason not in EmailSuppressionReason.values:
        raise ValueError("Unsupported email suppression reason.")
    if source not in EmailSuppressionSource.values:
        raise ValueError("Unsupported email suppression source.")

    now = timezone.now()
    suppression = (
        SDREmailSuppression.objects.select_for_update()
        .filter(org_id=org_id, email__iexact=normalized)
        .first()
    )
    created = suppression is None
    if suppression is None:
        suppression = SDREmailSuppression.objects.create(
            org_id=org_id,
            email=normalized,
            reason=reason,
            source=source,
            source_delivery=source_delivery,
            details=dict(details or {}),
        )
    else:
        suppression.email = normalized
        suppression.reason = reason
        suppression.source = source
        suppression.is_active = True
        suppression.suppressed_at = now
        suppression.released_at = None
        suppression.source_delivery = source_delivery or suppression.source_delivery
        suppression.details = dict(details or {})
        suppression.save(
            update_fields=[
                "email",
                "reason",
                "source",
                "is_active",
                "suppressed_at",
                "released_at",
                "source_delivery",
                "details",
                "updated_at",
            ]
        )

    _stop_matching_enrollments(suppression)
    return suppression, created


@transaction.atomic
def release_suppression(
    suppression: SDREmailSuppression,
    *,
    updated_by=None,
) -> SDREmailSuppression:
    suppression = SDREmailSuppression.objects.select_for_update().get(
        id=suppression.id,
        org_id=suppression.org_id,
    )
    if not suppression.is_active:
        return suppression
    suppression.is_active = False
    suppression.released_at = timezone.now()
    suppression.updated_by = updated_by
    suppression.save(
        update_fields=["is_active", "released_at", "updated_by", "updated_at"]
    )
    return suppression


def unsubscribe_url(delivery: LeadNurtureDelivery) -> str:
    token = signing.dumps(
        {
            "v": UNSUBSCRIBE_VERSION,
            "org": str(delivery.org_id),
            "delivery": str(delivery.id),
        },
        salt=UNSUBSCRIBE_SALT,
        compress=True,
    )
    base_url = settings.SDR_NURTURE_TRACKING_BASE_URL.rstrip("/")
    return f"{base_url}/api/sdr/public/nurture/unsubscribe/{token}/"


def parse_unsubscribe_token(token: str) -> UnsubscribeEvent:
    payload = signing.loads(
        token,
        salt=UNSUBSCRIBE_SALT,
        max_age=settings.SDR_NURTURE_TRACKING_MAX_AGE_SECONDS,
    )
    try:
        if payload.get("v") != UNSUBSCRIBE_VERSION:
            raise ValueError("Unsupported unsubscribe token version.")
        return UnsubscribeEvent(
            org_id=UUID(str(payload["org"])),
            delivery_id=UUID(str(payload["delivery"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise signing.BadSignature("Invalid unsubscribe token.") from exc


def _stop_matching_enrollments(suppression: SDREmailSuppression) -> None:
    from sdr.nurture import stop_enrollment

    enrollments = (
        LeadNurtureEnrollment.objects.filter(
            org_id=suppression.org_id,
            lead__email__iexact=suppression.email,
            status__in=SUPPRESSIBLE_ENROLLMENT_STATUSES,
        )
        .select_related("intake", "sequence")
        .prefetch_related("deliveries")
    )
    for enrollment in enrollments:
        stop_enrollment(
            enrollment,
            status=NurtureEnrollmentStatus.CANCELLED,
            reason="The email address is on the tenant suppression list.",
        )
        record_lifecycle_event(
            intake=enrollment.intake,
            event_type=LeadLifecycleEventType.NURTURE_SUPPRESSED,
            event_key=f"nurture:suppressed:{suppression.id}",
            data={
                "suppression_id": str(suppression.id),
                "reason": suppression.reason,
                "source": suppression.source,
            },
        )
