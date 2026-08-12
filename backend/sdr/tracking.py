"""Signed, provider-neutral interaction tracking for SDR nurture email."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from sdr.models import (
    LeadLifecycleEventType,
    LeadNurtureDelivery,
    LeadNurtureInteraction,
    NurtureDeliveryStatus,
    NurtureInteractionType,
)
from sdr.response import record_lifecycle_event

TRACKING_SALT = "sdr.nurture.tracking.v1"
TRACKING_VERSION = 1
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


@dataclass(frozen=True)
class TrackingEvent:
    org_id: UUID
    delivery_id: UUID
    event_type: str
    target_url: str = ""


def build_tracked_email_content(
    delivery: LeadNurtureDelivery,
    body: str,
    *,
    unsubscribe: str = "",
) -> tuple[str, str]:
    """Return tracked plain-text and escaped HTML representations of a body."""

    plain = _replace_links(body, delivery, as_html=False)
    html_body = _replace_links(body, delivery, as_html=True).replace("\n", "<br>\n")
    pixel_url = tracking_url(delivery, NurtureInteractionType.OPEN)
    pixel = (
        f'<img src="{html.escape(pixel_url, quote=True)}" width="1" height="1" '
        'alt="" aria-hidden="true" style="display:block;border:0;width:1px;height:1px" />'
    )
    footer_plain = f"\n\nUnsubscribe: {unsubscribe}" if unsubscribe else ""
    footer_html = (
        "<p style=\"font-size:12px;color:#64748b\">"
        f'<a href="{html.escape(unsubscribe, quote=True)}">Unsubscribe</a></p>'
        if unsubscribe
        else ""
    )
    return (
        f"{plain}{footer_plain}",
        f'<div style="white-space:normal">{html_body}</div>\n{footer_html}\n{pixel}',
    )


def tracking_url(
    delivery: LeadNurtureDelivery,
    event_type: str,
    *,
    target_url: str = "",
) -> str:
    token = make_tracking_token(
        delivery,
        event_type,
        target_url=target_url,
    )
    base_url = settings.SDR_NURTURE_TRACKING_BASE_URL.rstrip("/")
    if event_type == NurtureInteractionType.OPEN:
        return f"{base_url}/api/sdr/public/nurture/open/{token}/pixel.gif"
    return f"{base_url}/api/sdr/public/nurture/click/{token}/"


def make_tracking_token(
    delivery: LeadNurtureDelivery,
    event_type: str,
    *,
    target_url: str = "",
) -> str:
    if event_type not in NurtureInteractionType.values:
        raise ValueError("Unsupported nurture interaction type.")
    destination = ""
    if event_type == NurtureInteractionType.CLICK:
        destination = validate_destination(target_url)
    payload = {
        "v": TRACKING_VERSION,
        "org": str(delivery.org_id),
        "delivery": str(delivery.id),
        "event": event_type,
    }
    if destination:
        payload["url"] = destination
    return signing.dumps(payload, salt=TRACKING_SALT, compress=True)


def parse_tracking_token(token: str, expected_event: str) -> TrackingEvent:
    if expected_event not in NurtureInteractionType.values:
        raise signing.BadSignature("Unsupported tracking event.")
    payload = signing.loads(
        token,
        salt=TRACKING_SALT,
        max_age=settings.SDR_NURTURE_TRACKING_MAX_AGE_SECONDS,
    )
    try:
        if payload.get("v") != TRACKING_VERSION:
            raise ValueError("Unsupported tracking token version.")
        event_type = str(payload["event"])
        if event_type != expected_event:
            raise ValueError("Tracking token event mismatch.")
        target_url = str(payload.get("url") or "")
        if event_type == NurtureInteractionType.CLICK:
            target_url = validate_destination(target_url)
        elif target_url:
            raise ValueError("Open tokens cannot contain a destination.")
        return TrackingEvent(
            org_id=UUID(str(payload["org"])),
            delivery_id=UUID(str(payload["delivery"])),
            event_type=event_type,
            target_url=target_url,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise signing.BadSignature("Invalid nurture tracking token.") from exc


@transaction.atomic
def record_interaction(
    event: TrackingEvent,
    *,
    remote_addr: str,
    user_agent: str,
) -> bool:
    """Record one unique visitor/target event and update delivery aggregates."""

    delivery = (
        LeadNurtureDelivery.objects.select_for_update()
        .select_related("enrollment__intake")
        .filter(
            id=event.delivery_id,
            org_id=event.org_id,
            status=NurtureDeliveryStatus.SENT,
        )
        .first()
    )
    if delivery is None:
        return False

    target_hash = hashlib.sha256(event.target_url.encode("utf-8")).hexdigest()
    visitor_hash = _visitor_hash(remote_addr, user_agent)
    _, created = LeadNurtureInteraction.objects.get_or_create(
        org_id=event.org_id,
        delivery=delivery,
        event_type=event.event_type,
        target_hash=target_hash,
        visitor_hash=visitor_hash,
        defaults={"target_url": event.target_url},
    )
    if not created:
        return False

    now = timezone.now()
    if event.event_type == NurtureInteractionType.OPEN:
        first_delivery_event = delivery.opened_at is None
        delivery.opened_at = delivery.opened_at or now
        delivery.open_count += 1
        update_fields = ["opened_at", "open_count", "updated_at"]
        lifecycle_type = LeadLifecycleEventType.NURTURE_EMAIL_OPENED
        lifecycle_suffix = "opened"
    else:
        first_delivery_event = delivery.clicked_at is None
        delivery.clicked_at = delivery.clicked_at or now
        delivery.click_count += 1
        delivery.last_clicked_url = event.target_url
        update_fields = [
            "clicked_at",
            "click_count",
            "last_clicked_url",
            "updated_at",
        ]
        lifecycle_type = LeadLifecycleEventType.NURTURE_LINK_CLICKED
        lifecycle_suffix = "clicked"
    delivery.save(update_fields=update_fields)

    if first_delivery_event:
        record_lifecycle_event(
            intake=delivery.enrollment.intake,
            event_type=lifecycle_type,
            event_key=f"nurture:delivery:{delivery.id}:{lifecycle_suffix}",
            data={
                "delivery_id": str(delivery.id),
                "sequence_id": str(delivery.enrollment.sequence_id),
                "step": delivery.step_position,
                "variant": delivery.variant,
                **({"target_url": event.target_url} if event.target_url else {}),
            },
        )
    return True


def validate_destination(value: str) -> str:
    destination = (value or "").strip()
    if not destination or len(destination) > 2048:
        raise ValueError("Tracked links must contain a valid HTTP(S) URL.")
    if any(ord(char) < 32 for char in destination):
        raise ValueError("Tracked links cannot contain control characters.")
    try:
        parsed = urlsplit(destination)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("Tracked links must contain a valid HTTP(S) URL.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Tracked links must contain a valid HTTP(S) URL.")
    return destination


def _replace_links(
    body: str,
    delivery: LeadNurtureDelivery,
    *,
    as_html: bool,
) -> str:
    chunks: list[str] = []
    cursor = 0
    for match in URL_PATTERN.finditer(body):
        chunks.append(html.escape(body[cursor : match.start()]) if as_html else body[cursor : match.start()])
        candidate = match.group(0)
        target = candidate.rstrip(TRAILING_URL_PUNCTUATION)
        suffix = candidate[len(target) :]
        try:
            tracked = tracking_url(
                delivery,
                NurtureInteractionType.CLICK,
                target_url=target,
            )
        except ValueError:
            chunks.append(html.escape(candidate) if as_html else candidate)
        else:
            if as_html:
                chunks.append(
                    f'<a href="{html.escape(tracked, quote=True)}" rel="noopener noreferrer">'
                    f"{html.escape(target)}</a>{html.escape(suffix)}"
                )
            else:
                chunks.append(f"{tracked}{suffix}")
        cursor = match.end()
    chunks.append(html.escape(body[cursor:]) if as_html else body[cursor:])
    return "".join(chunks)


def _visitor_hash(remote_addr: str, user_agent: str) -> str:
    value = f"{remote_addr[:128]}\x00{user_agent[:512]}"
    return salted_hmac(
        "sdr.nurture.visitor",
        value,
        secret=settings.SECRET_KEY,
        algorithm="sha256",
    ).hexdigest()
