"""Short-lived, tenant-safe Meta OAuth onboarding workflow."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from automation.tenant_context import database_org_context
from integrations.models import (
    FacebookOAuthSession,
    FacebookOAuthSessionStatus,
    FacebookPageConnection,
)
from integrations.providers.facebook.client import (
    FacebookGraphAPIError,
    FacebookGraphClient,
)
from integrations.providers.facebook.service import (
    connect_facebook_page,
    graph_client,
)

STATE_SALT = "integrations.facebook.oauth-state.v1"


class FacebookOAuthConfigurationError(RuntimeError):
    pass


class FacebookOAuthStateError(ValueError):
    pass


class FacebookOAuthSelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FacebookOAuthStart:
    session_id: UUID
    authorization_url: str
    expires_at: datetime


def start_facebook_oauth(*, org_id: UUID, profile_id: UUID) -> FacebookOAuthStart:
    _require_configuration()
    now = timezone.now()
    expires_at = now + timedelta(seconds=settings.META_OAUTH_STATE_TTL)
    FacebookOAuthSession.objects.filter(
        org_id=org_id,
        initiated_by_profile_id=profile_id,
        status__in=(
            FacebookOAuthSessionStatus.PENDING,
            FacebookOAuthSessionStatus.EXCHANGING,
            FacebookOAuthSessionStatus.READY,
            FacebookOAuthSessionStatus.SELECTING,
        ),
        expires_at__lte=now,
    ).update(
        status=FacebookOAuthSessionStatus.EXPIRED,
        page_tokens_ciphertext="",
        error_code="expired",
    )
    session = FacebookOAuthSession.objects.create(
        org_id=org_id,
        initiated_by_profile_id=profile_id,
        expires_at=expires_at,
    )
    state = signing.dumps(
        {
            "session_id": str(session.id),
            "org_id": str(org_id),
            "profile_id": str(profile_id),
        },
        salt=STATE_SALT,
    )
    query = urlencode(
        {
            "client_id": settings.META_APP_ID,
            "redirect_uri": settings.META_OAUTH_REDIRECT_URI,
            "state": state,
            "scope": ",".join(settings.META_OAUTH_SCOPES),
            "response_type": "code",
        }
    )
    return FacebookOAuthStart(
        session_id=session.id,
        authorization_url=f"{settings.META_OAUTH_DIALOG_URL}?{query}",
        expires_at=expires_at,
    )


def finish_facebook_oauth(
    *,
    code: str,
    state: str,
    client: FacebookGraphClient | None = None,
) -> FacebookOAuthSession:
    session_id, org_id, profile_id = _decode_state(state)
    with database_org_context(org_id):
        _claim_exchange(
            session_id=session_id,
            org_id=org_id,
            profile_id=profile_id,
        )
        try:
            api_client = client or graph_client()
            token = api_client.exchange_oauth_code(
                code=code,
                redirect_uri=settings.META_OAUTH_REDIRECT_URI,
            )
            pages = api_client.fetch_managed_pages(user_access_token=token.access_token)
            credentials, snapshot = _sanitize_pages(pages)
        except Exception as exc:
            _mark_failed(session_id=session_id, org_id=org_id, exc=exc)
            raise

        with transaction.atomic():
            session = FacebookOAuthSession.objects.select_for_update().get(
                id=session_id,
                org_id=org_id,
                initiated_by_profile_id=profile_id,
            )
            if session.status != FacebookOAuthSessionStatus.EXCHANGING:
                raise FacebookOAuthStateError("OAuth session is no longer exchangeable")
            session.pages_snapshot = snapshot
            session.set_page_credentials(credentials)
            session.status = FacebookOAuthSessionStatus.READY
            session.error_code = ""
            session.error_message = ""
            session.expires_at = timezone.now() + timedelta(
                seconds=settings.META_OAUTH_STATE_TTL
            )
            session.save(
                update_fields=[
                    "pages_snapshot",
                    "page_tokens_ciphertext",
                    "status",
                    "error_code",
                    "error_message",
                    "expires_at",
                    "updated_at",
                ]
            )
            return session


def select_facebook_pages(
    *,
    org_id: UUID,
    profile_id: UUID,
    session_id: UUID,
    page_ids: Sequence[str],
    client: FacebookGraphClient | None = None,
) -> tuple[FacebookPageConnection, ...]:
    selected_ids = tuple(dict.fromkeys(str(page_id).strip() for page_id in page_ids))
    if not selected_ids or any(not page_id for page_id in selected_ids):
        raise FacebookOAuthSelectionError("Select at least one Facebook Page")

    with transaction.atomic():
        session = FacebookOAuthSession.objects.select_for_update().get(
            id=session_id,
            org_id=org_id,
            initiated_by_profile_id=profile_id,
        )
        if session.expires_at <= timezone.now():
            session.status = FacebookOAuthSessionStatus.EXPIRED
            session.clear_page_credentials()
            session.error_code = "expired"
            session.save(
                update_fields=[
                    "status",
                    "page_tokens_ciphertext",
                    "error_code",
                    "updated_at",
                ]
            )
            raise FacebookOAuthSelectionError("Facebook authorization has expired")
        if session.status != FacebookOAuthSessionStatus.READY:
            raise FacebookOAuthSelectionError("Facebook authorization is not ready")

        credentials_by_id = {
            str(page.get("id")): page for page in session.get_page_credentials()
        }
        if any(page_id not in credentials_by_id for page_id in selected_ids):
            raise FacebookOAuthSelectionError(
                "Selected Facebook Page is not authorized"
            )
        selected_pages = [credentials_by_id[page_id] for page_id in selected_ids]
        session.status = FacebookOAuthSessionStatus.SELECTING
        session.error_code = ""
        session.error_message = ""
        session.save(
            update_fields=["status", "error_code", "error_message", "updated_at"]
        )

    api_client = client or graph_client()
    try:
        connections = tuple(
            connect_facebook_page(
                org_id=org_id,
                page_access_token=str(page["access_token"]),
                client=api_client,
            )
            for page in selected_pages
        )
    except Exception as exc:
        FacebookOAuthSession.objects.filter(id=session_id, org_id=org_id).update(
            status=FacebookOAuthSessionStatus.READY,
            error_code="page_connection_failed",
            error_message=str(exc)[:1000],
        )
        raise

    with transaction.atomic():
        session = FacebookOAuthSession.objects.select_for_update().get(
            id=session_id,
            org_id=org_id,
            initiated_by_profile_id=profile_id,
        )
        session.status = FacebookOAuthSessionStatus.COMPLETED
        session.completed_at = timezone.now()
        session.clear_page_credentials()
        session.error_code = ""
        session.error_message = ""
        session.save(
            update_fields=[
                "status",
                "completed_at",
                "page_tokens_ciphertext",
                "error_code",
                "error_message",
                "updated_at",
            ]
        )
    return connections


def _require_configuration() -> None:
    missing = [
        name
        for name, value in {
            "META_APP_ID": settings.META_APP_ID,
            "META_APP_SECRET": settings.META_APP_SECRET,
            "META_OAUTH_REDIRECT_URI": settings.META_OAUTH_REDIRECT_URI,
        }.items()
        if not value
    ]
    if missing:
        raise FacebookOAuthConfigurationError(
            f"Facebook OAuth is not configured: {', '.join(missing)}"
        )


def _decode_state(state: str) -> tuple[UUID, UUID, UUID]:
    try:
        payload = signing.loads(
            state,
            salt=STATE_SALT,
            max_age=settings.META_OAUTH_STATE_TTL,
        )
        return (
            UUID(str(payload["session_id"])),
            UUID(str(payload["org_id"])),
            UUID(str(payload["profile_id"])),
        )
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise FacebookOAuthStateError(
            "Facebook OAuth state is invalid or expired"
        ) from exc


def _claim_exchange(*, session_id: UUID, org_id: UUID, profile_id: UUID) -> None:
    with transaction.atomic():
        try:
            session = FacebookOAuthSession.objects.select_for_update().get(
                id=session_id,
                org_id=org_id,
                initiated_by_profile_id=profile_id,
            )
        except FacebookOAuthSession.DoesNotExist as exc:
            raise FacebookOAuthStateError(
                "Facebook OAuth session was not found"
            ) from exc
        if session.expires_at <= timezone.now():
            session.status = FacebookOAuthSessionStatus.EXPIRED
            session.error_code = "expired"
            session.save(update_fields=["status", "error_code", "updated_at"])
            raise FacebookOAuthStateError("Facebook OAuth session has expired")
        if session.status != FacebookOAuthSessionStatus.PENDING:
            raise FacebookOAuthStateError("Facebook OAuth state has already been used")
        session.status = FacebookOAuthSessionStatus.EXCHANGING
        session.save(update_fields=["status", "updated_at"])


def _mark_failed(*, session_id: UUID, org_id: UUID, exc: Exception) -> None:
    error_code = (
        "meta_api_error" if isinstance(exc, FacebookGraphAPIError) else "oauth_failed"
    )
    FacebookOAuthSession.objects.filter(id=session_id, org_id=org_id).update(
        status=FacebookOAuthSessionStatus.FAILED,
        page_tokens_ciphertext="",
        error_code=error_code,
        error_message=str(exc)[:1000],
    )


def _sanitize_pages(
    pages: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    credentials: list[dict[str, Any]] = []
    snapshot: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        page_id = str(page.get("id", "")).strip()
        access_token = str(page.get("access_token", "")).strip()
        if not page_id or not access_token or page_id in seen:
            continue
        seen.add(page_id)
        name = str(page.get("name", "")).strip()[:255]
        tasks = page.get("tasks", [])
        safe_tasks = (
            [str(task) for task in tasks]
            if isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes))
            else []
        )
        credentials.append(
            {
                "id": page_id,
                "name": name,
                "access_token": access_token,
            }
        )
        snapshot.append({"id": page_id, "name": name, "tasks": safe_tasks})
    return credentials, snapshot
