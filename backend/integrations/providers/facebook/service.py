"""Tenant connection and lead-processing services for Meta Lead Ads."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from integrations.models import FacebookPageConnection, FacebookPageRoute
from integrations.providers.facebook.adapter import (
    FacebookLeadAdsAdapter,
    FacebookLeadEvent,
)
from integrations.providers.facebook.client import FacebookGraphClient
from integrations.tenant_context import database_org_context
from sdr.services import LeadIntakeResult, process_candidate_intake


class FacebookPageAlreadyConnected(ValueError):
    pass


class FacebookPageIdentityMismatch(ValueError):
    pass


class FacebookConnectionUnavailable(LookupError):
    pass


def graph_client() -> FacebookGraphClient:
    return FacebookGraphClient(
        app_secret=settings.META_APP_SECRET,
        api_version=settings.META_GRAPH_API_VERSION,
        base_url=settings.META_GRAPH_API_BASE_URL,
        timeout=settings.META_GRAPH_API_TIMEOUT,
    )


def connect_facebook_page(
    *,
    org_id: UUID,
    page_access_token: str,
    token_expires_at: datetime | None = None,
    client: FacebookGraphClient | None = None,
) -> FacebookPageConnection:
    """Validate a Page token with Meta and store it encrypted for one tenant."""

    api_client = client or graph_client()
    identity = api_client.fetch_page_identity(access_token=page_access_token)
    page_id = str(identity.get("id", "")).strip()
    if not page_id:
        raise FacebookPageIdentityMismatch("Meta did not return a Page id")

    existing_route = FacebookPageRoute.objects.filter(page_id=page_id).first()
    if existing_route and existing_route.org_id != org_id:
        raise FacebookPageAlreadyConnected(
            "This Facebook Page is already connected to another organization"
        )
    api_client.subscribe_page(
        page_id=page_id,
        access_token=page_access_token,
    )

    return _store_facebook_page(
        org_id=org_id,
        page_id=page_id,
        page_name=str(identity.get("name", ""))[:255],
        page_access_token=page_access_token,
        token_expires_at=token_expires_at,
    )


@transaction.atomic
def _store_facebook_page(
    *,
    org_id: UUID,
    page_id: str,
    page_name: str,
    page_access_token: str,
    token_expires_at: datetime | None,
) -> FacebookPageConnection:
    route = (
        FacebookPageRoute.objects.select_for_update().filter(page_id=page_id).first()
    )
    if route is None:
        try:
            with transaction.atomic():
                route = FacebookPageRoute.objects.create(page_id=page_id, org_id=org_id)
        except IntegrityError:
            route = FacebookPageRoute.objects.select_for_update().get(page_id=page_id)
    if route and route.org_id != org_id:
        raise FacebookPageAlreadyConnected(
            "This Facebook Page is already connected to another organization"
        )
    connection, _ = FacebookPageConnection.objects.get_or_create(
        org_id=org_id,
        route=route,
        defaults={
            "page_name": page_name,
            "access_token_ciphertext": "pending",
        },
    )
    connection.page_name = page_name
    connection.token_expires_at = token_expires_at
    connection.is_active = True
    connection.set_access_token(page_access_token)
    connection.full_clean()
    connection.save()
    return connection


def process_facebook_lead_event(
    *,
    event_payload: Mapping[str, Any],
    client: FacebookGraphClient | None = None,
) -> LeadIntakeResult:
    """Resolve the tenant, fetch the lead, and run the shared SDR pipeline."""

    event = FacebookLeadEvent(
        page_id=str(event_payload["page_id"]),
        leadgen_id=str(event_payload["leadgen_id"]),
        form_id=_optional_string(event_payload.get("form_id")),
        ad_id=_optional_string(event_payload.get("ad_id")),
        adgroup_id=_optional_string(event_payload.get("adgroup_id")),
        created_time=(
            int(event_payload["created_time"])
            if event_payload.get("created_time") is not None
            else None
        ),
    )
    try:
        route = FacebookPageRoute.objects.only("id", "org_id", "page_id").get(
            page_id=event.page_id
        )
    except FacebookPageRoute.DoesNotExist as exc:
        raise FacebookConnectionUnavailable(
            "No organization is connected to this Facebook Page"
        ) from exc

    with database_org_context(route.org_id):
        try:
            connection = FacebookPageConnection.objects.get(
                org_id=route.org_id,
                route=route,
                is_active=True,
            )
        except FacebookPageConnection.DoesNotExist as exc:
            raise FacebookConnectionUnavailable(
                "The Facebook Page connection is inactive or missing"
            ) from exc
        if (
            connection.token_expires_at
            and connection.token_expires_at <= timezone.now()
        ):
            raise FacebookConnectionUnavailable("The Facebook Page token has expired")

        lead = (client or graph_client()).fetch_lead(
            leadgen_id=event.leadgen_id,
            access_token=connection.get_access_token(),
        )
        adapter = FacebookLeadAdsAdapter(app_secret=settings.META_APP_SECRET)
        candidate = adapter.normalize(org_id=route.org_id, event=event, lead=lead)
        result = process_candidate_intake(
            candidate=candidate,
            raw_payload={"webhook": event.as_payload(), "lead": dict(lead)},
        )
        FacebookPageConnection.objects.filter(id=connection.id).update(
            last_webhook_at=timezone.now()
        )
        return result


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
