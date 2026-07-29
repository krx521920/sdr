"""Tenant connections and global webhook routing metadata."""

import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models

from common.base import BaseOrgModel
from integrations.secrets import decrypt_secret, encrypt_secret


class FacebookPageRoute(models.Model):
    """Minimal non-RLS bootstrap used to map a signed Meta event to a tenant."""

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    page_id = models.CharField(max_length=64, unique=True)
    org = models.ForeignKey(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="facebook_page_routes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integration_facebook_page_route"
        ordering = ("page_id",)

    def __str__(self) -> str:
        return self.page_id


class FacebookPageConnection(BaseOrgModel):
    """An encrypted Page token and state owned by one CRM organization."""

    route = models.OneToOneField(
        FacebookPageRoute,
        on_delete=models.CASCADE,
        related_name="connection",
    )
    page_name = models.CharField(max_length=255, blank=True)
    access_token_ciphertext = models.TextField()
    access_token_hint = models.CharField(max_length=12, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    last_webhook_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_facebook_page_connection"
        ordering = ("page_name", "created_at")
        indexes = [models.Index(fields=["org", "is_active"])]

    @property
    def page_id(self) -> str:
        return self.route.page_id

    def set_access_token(self, token: str) -> None:
        cleaned = token.strip()
        self.access_token_ciphertext = encrypt_secret(cleaned)
        self.access_token_hint = cleaned[-8:]

    def get_access_token(self) -> str:
        return decrypt_secret(self.access_token_ciphertext)

    def clean(self) -> None:
        super().clean()
        if self.route_id and self.org_id and self.route.org_id != self.org_id:
            raise ValidationError("Facebook Page route must belong to the same org")

    def __str__(self) -> str:
        return self.page_name or self.page_id


class FacebookOAuthSessionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    EXCHANGING = "exchanging", "Exchanging"
    READY = "ready", "Ready"
    SELECTING = "selecting", "Selecting"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"


class FacebookOAuthSession(BaseOrgModel):
    """Short-lived encrypted handoff between Meta OAuth and Page selection."""

    initiated_by_profile = models.ForeignKey(
        "common.Profile",
        on_delete=models.CASCADE,
        related_name="facebook_oauth_sessions",
    )
    status = models.CharField(
        max_length=16,
        choices=FacebookOAuthSessionStatus.choices,
        default=FacebookOAuthSessionStatus.PENDING,
    )
    pages_snapshot = models.JSONField(default=list)
    page_tokens_ciphertext = models.TextField(blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_facebook_oauth_session"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["org", "status", "-created_at"])]

    def set_page_credentials(self, pages: list[dict]) -> None:
        self.page_tokens_ciphertext = encrypt_secret(
            json.dumps(pages, separators=(",", ":"), ensure_ascii=False)
        )

    def get_page_credentials(self) -> list[dict]:
        if not self.page_tokens_ciphertext:
            return []
        pages = json.loads(decrypt_secret(self.page_tokens_ciphertext))
        if not isinstance(pages, list):
            raise ValueError("stored Facebook Page credentials are invalid")
        return pages

    def clear_page_credentials(self) -> None:
        self.page_tokens_ciphertext = ""

    def __str__(self) -> str:
        return f"{self.org_id}:{self.status}"
