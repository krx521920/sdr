"""Tenant connections and global webhook routing metadata."""

import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models

from common.base import BaseOrgModel
from integrations.secrets import decrypt_secret, encrypt_secret


def default_facebook_qualified_bands() -> list[str]:
    return ["high"]


DEFAULT_FACEBOOK_MESSENGER_AUTO_REPLY = (
    "Thanks for contacting {{ page_name }}. We've received your message and "
    "a member of our team will reply soon."
)


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
    messenger_enabled = models.BooleanField(default=False)
    messenger_auto_reply_enabled = models.BooleanField(default=False)
    messenger_auto_reply_template = models.TextField(
        default=DEFAULT_FACEBOOK_MESSENGER_AUTO_REPLY
    )
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_reply_at = models.DateTimeField(null=True, blank=True)

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


class FacebookMessengerMessageStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    QUEUED = "queued", "Queued"
    PROCESSED = "processed", "Processed"
    IGNORED = "ignored", "Ignored"
    FAILED = "failed", "Failed"


class FacebookMessengerMessage(BaseOrgModel):
    """Tenant-scoped audit record for one inbound Page message."""

    connection = models.ForeignKey(
        FacebookPageConnection,
        on_delete=models.SET_NULL,
        related_name="messenger_messages",
        null=True,
        blank=True,
    )
    intake = models.ForeignKey(
        "sdr.LeadIntake",
        on_delete=models.SET_NULL,
        related_name="facebook_messenger_messages",
        null=True,
        blank=True,
    )
    page_id = models.CharField(max_length=64)
    sender_psid = models.CharField(max_length=128)
    message_id = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    attachment_types = models.JSONField(default=list, blank=True)
    occurred_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=FacebookMessengerMessageStatus.choices,
        default=FacebookMessengerMessageStatus.RECEIVED,
    )
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_facebook_messenger_message"
        ordering = ("-occurred_at", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "page_id", "message_id"],
                name="unique_fb_messenger_message",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-occurred_at"],
                name="fb_messenger_org_status_idx",
            ),
            models.Index(
                fields=["org", "page_id", "sender_psid"],
                name="fb_messenger_sender_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.connection_id and self.connection.org_id != self.org_id:
            raise ValidationError("Facebook message connection must belong to the org")
        if self.intake_id and self.intake.org_id != self.org_id:
            raise ValidationError("Facebook message intake must belong to the org")

    def __str__(self) -> str:
        return f"{self.page_id}:{self.message_id}"


class FacebookMessengerReplyStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    SKIPPED = "skipped", "Skipped"
    FAILED = "failed", "Failed"


class FacebookMessengerReplyKind(models.TextChoices):
    AUTO_ACKNOWLEDGEMENT = "auto_acknowledgement", "Auto acknowledgement"
    MANUAL = "manual", "Manual"


class FacebookMessengerReply(BaseOrgModel):
    """Auditable automatic response sent once per Page-scoped conversation."""

    connection = models.ForeignKey(
        FacebookPageConnection,
        on_delete=models.SET_NULL,
        related_name="messenger_replies",
        null=True,
        blank=True,
    )
    trigger_message = models.ForeignKey(
        FacebookMessengerMessage,
        on_delete=models.PROTECT,
        related_name="outbound_replies",
    )
    page_id = models.CharField(max_length=64)
    recipient_psid = models.CharField(max_length=128)
    kind = models.CharField(
        max_length=32,
        choices=FacebookMessengerReplyKind.choices,
        default=FacebookMessengerReplyKind.AUTO_ACKNOWLEDGEMENT,
    )
    client_request_id = models.UUIDField(null=True, blank=True)
    body = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=FacebookMessengerReplyStatus.choices,
        default=FacebookMessengerReplyStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    provider_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_facebook_messenger_reply"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "page_id", "recipient_psid"],
                name="unique_fb_messenger_auto_reply",
                condition=models.Q(
                    kind=FacebookMessengerReplyKind.AUTO_ACKNOWLEDGEMENT
                ),
            ),
            models.UniqueConstraint(
                fields=["org", "client_request_id"],
                name="unique_fb_messenger_reply_request",
                condition=models.Q(client_request_id__isnull=False),
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="fb_msg_reply_org_status_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.connection_id and self.connection.org_id != self.org_id:
            raise ValidationError("Facebook reply connection must belong to the org")
        if self.trigger_message_id and self.trigger_message.org_id != self.org_id:
            raise ValidationError("Facebook reply trigger must belong to the org")

    def __str__(self) -> str:
        return f"{self.page_id}:{self.recipient_psid}:{self.status}"


class FacebookConversionSettings(BaseOrgModel):
    """Tenant-owned Meta Conversion Leads destination and funnel mapping."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="facebook_conversion_settings",
    )
    is_enabled = models.BooleanField(default=False)
    pixel_id = models.CharField(max_length=32, blank=True)
    access_token_ciphertext = models.TextField(blank=True)
    access_token_hint = models.CharField(max_length=12, blank=True)
    lead_event_source = models.CharField(max_length=100, default="BottleCRM")
    raw_lead_event_name = models.CharField(max_length=100, default="RawLead")
    qualified_lead_event_name = models.CharField(
        max_length=100,
        default="MarketingQualifiedLead",
    )
    converted_event_name = models.CharField(max_length=100, default="Converted")
    qualified_bands = models.JSONField(
        default=default_facebook_qualified_bands,
        blank=True,
    )
    test_event_code = models.CharField(max_length=100, blank=True)
    last_event_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_facebook_conversion_settings"

    def set_access_token(self, token: str) -> None:
        cleaned = token.strip()
        self.access_token_ciphertext = encrypt_secret(cleaned)
        self.access_token_hint = cleaned[-8:]

    def get_access_token(self) -> str:
        if not self.access_token_ciphertext:
            return ""
        return decrypt_secret(self.access_token_ciphertext)

    def clear_access_token(self) -> None:
        self.access_token_ciphertext = ""
        self.access_token_hint = ""

    def __str__(self) -> str:
        return f"{self.org_id}:{self.pixel_id or 'not-configured'}"


class FacebookConversionEventStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class FacebookConversionEvent(BaseOrgModel):
    """Immutable CRM-funnel event queued for Meta Conversion Leads."""

    intake = models.ForeignKey(
        "sdr.LeadIntake",
        on_delete=models.CASCADE,
        related_name="facebook_conversion_events",
    )
    crm_lead = models.ForeignKey(
        "leads.Lead",
        on_delete=models.SET_NULL,
        related_name="facebook_conversion_events",
        null=True,
        blank=True,
    )
    leadgen_id = models.CharField(max_length=32)
    event_name = models.CharField(max_length=100)
    event_key = models.CharField(max_length=160)
    event_time = models.DateTimeField()
    pixel_id = models.CharField(max_length=32)
    lead_event_source = models.CharField(max_length=100)
    test_event_code = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=16,
        choices=FacebookConversionEventStatus.choices,
        default=FacebookConversionEventStatus.PENDING,
    )
    provider_events_received = models.PositiveIntegerField(null=True, blank=True)
    provider_trace_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_facebook_conversion_event"
        ordering = ("-event_time", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "event_key"],
                name="unique_fb_conversion_event_key",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-event_time"],
                name="fb_conversion_org_status_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.intake_id and self.org_id and self.intake.org_id != self.org_id:
            raise ValidationError("Facebook conversion intake must belong to the org")
        if self.crm_lead_id and self.org_id and self.crm_lead.org_id != self.org_id:
            raise ValidationError("Facebook conversion lead must belong to the org")

    def __str__(self) -> str:
        return f"{self.leadgen_id}:{self.event_name}"


class WhatsAppPhoneRoute(models.Model):
    """Non-RLS bootstrap mapping for signed WhatsApp webhook events."""

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    phone_number_id = models.CharField(max_length=64, unique=True)
    org = models.ForeignKey(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="whatsapp_phone_routes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integration_whatsapp_phone_route"
        ordering = ("phone_number_id",)

    def __str__(self) -> str:
        return self.phone_number_id


class WhatsAppBusinessConnection(BaseOrgModel):
    """Tenant-owned encrypted WhatsApp Cloud API sender configuration."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="whatsapp_business_connection",
    )
    route = models.OneToOneField(
        WhatsAppPhoneRoute,
        on_delete=models.CASCADE,
        related_name="connection",
    )
    business_account_id = models.CharField(max_length=64, blank=True)
    display_phone_number = models.CharField(max_length=32, blank=True)
    access_token_ciphertext = models.TextField()
    access_token_hint = models.CharField(max_length=12, blank=True)
    is_active = models.BooleanField(default=False)
    last_message_sent_at = models.DateTimeField(null=True, blank=True)
    last_webhook_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_whatsapp_connection"
        ordering = ("display_phone_number", "created_at")
        indexes = [models.Index(fields=["org", "is_active"])]

    @property
    def phone_number_id(self) -> str:
        return self.route.phone_number_id

    def set_access_token(self, token: str) -> None:
        cleaned = token.strip()
        self.access_token_ciphertext = encrypt_secret(cleaned)
        self.access_token_hint = cleaned[-8:]

    def get_access_token(self) -> str:
        return decrypt_secret(self.access_token_ciphertext)

    def clean(self) -> None:
        super().clean()
        if self.route_id and self.org_id and self.route.org_id != self.org_id:
            raise ValidationError("WhatsApp phone route must belong to the same org")

    def __str__(self) -> str:
        return self.display_phone_number or self.phone_number_id


class WhatsAppMessageStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    READ = "read", "Read"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class WhatsAppMessage(BaseOrgModel):
    """Auditable, idempotent outbound WhatsApp campaign message."""

    connection = models.ForeignKey(
        WhatsAppBusinessConnection,
        on_delete=models.PROTECT,
        related_name="messages",
    )
    campaign = models.ForeignKey(
        "sdr.SDROutboundCampaign",
        on_delete=models.CASCADE,
        related_name="whatsapp_messages",
    )
    prospect = models.ForeignKey(
        "sdr.SDROutboundProspect",
        on_delete=models.CASCADE,
        related_name="whatsapp_messages",
    )
    campaign_run = models.PositiveIntegerField()
    recipient = models.CharField(max_length=32)
    template_name = models.CharField(max_length=512)
    template_language = models.CharField(max_length=20, default="en_US")
    status = models.CharField(
        max_length=16,
        choices=WhatsAppMessageStatus.choices,
        default=WhatsAppMessageStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    provider_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    provider_status_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "integration_whatsapp_message"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "campaign", "prospect", "campaign_run"],
                name="unique_wa_campaign_message",
            ),
            models.UniqueConstraint(
                fields=["org", "provider_message_id"],
                condition=~models.Q(provider_message_id=""),
                name="unique_wa_provider_message",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="wa_message_org_status_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        related = (self.connection, self.campaign, self.prospect)
        if self.org_id and any(item.org_id != self.org_id for item in related):
            raise ValidationError("WhatsApp message relations must belong to the org")
        if self.prospect_id and self.prospect.campaign_id != self.campaign_id:
            raise ValidationError("WhatsApp prospect must belong to the campaign")

    def __str__(self) -> str:
        return f"{self.recipient}:{self.status}"


class ApolloConnection(BaseOrgModel):
    """Tenant-owned encrypted credential for Apollo prospect search."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="apollo_connection",
    )
    api_key_ciphertext = models.TextField()
    api_key_hint = models.CharField(max_length=12, blank=True)
    is_active = models.BooleanField(default=False)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_apollo_connection"

    def set_api_key(self, value: str) -> None:
        cleaned = value.strip()
        self.api_key_ciphertext = encrypt_secret(cleaned)
        self.api_key_hint = cleaned[-8:]

    def get_api_key(self) -> str:
        return decrypt_secret(self.api_key_ciphertext)

    def __str__(self) -> str:
        return f"{self.org_id}:{'active' if self.is_active else 'inactive'}"


class LinkedInConnection(BaseOrgModel):
    """Tenant-owned credential for LinkedIn's partner-only Invitations API."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="linkedin_connection",
    )
    access_token_ciphertext = models.TextField()
    access_token_hint = models.CharField(max_length=12, blank=True)
    is_active = models.BooleanField(default=False)
    partner_access_confirmed = models.BooleanField(default=False)
    last_invitation_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_linkedin_connection"
        indexes = [models.Index(fields=["org", "is_active"])]

    def set_access_token(self, value: str) -> None:
        cleaned = value.strip()
        self.access_token_ciphertext = encrypt_secret(cleaned)
        self.access_token_hint = cleaned[-8:]

    def get_access_token(self) -> str:
        return decrypt_secret(self.access_token_ciphertext)

    def clean(self) -> None:
        super().clean()
        if self.is_active and not self.partner_access_confirmed:
            raise ValidationError(
                "Confirm approved LinkedIn partner API access before enabling."
            )

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        return f"{self.org_id}:{state}"


class LinkedInInvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class LinkedInInvitation(BaseOrgModel):
    """Auditable, idempotent LinkedIn connection invitation."""

    connection = models.ForeignKey(
        LinkedInConnection,
        on_delete=models.PROTECT,
        related_name="invitations",
    )
    campaign = models.ForeignKey(
        "sdr.SDROutboundCampaign",
        on_delete=models.CASCADE,
        related_name="linkedin_invitations",
    )
    prospect = models.ForeignKey(
        "sdr.SDROutboundProspect",
        on_delete=models.CASCADE,
        related_name="linkedin_invitations",
    )
    campaign_run = models.PositiveIntegerField()
    recipient = models.EmailField()
    message_body = models.CharField(max_length=300, blank=True)
    status = models.CharField(
        max_length=16,
        choices=LinkedInInvitationStatus.choices,
        default=LinkedInInvitationStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    provider_invitation_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    provider_status_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "integration_linkedin_invitation"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "campaign", "prospect", "campaign_run"],
                name="unique_li_campaign_invitation",
            ),
            models.UniqueConstraint(
                fields=["org", "provider_invitation_id"],
                condition=~models.Q(provider_invitation_id=""),
                name="unique_li_provider_invitation",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="li_invite_org_status_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        related = (self.connection, self.campaign, self.prospect)
        if self.org_id and any(item.org_id != self.org_id for item in related):
            raise ValidationError(
                "LinkedIn invitation relations must belong to the org"
            )
        if self.prospect_id and self.prospect.campaign_id != self.campaign_id:
            raise ValidationError("LinkedIn prospect must belong to the campaign")

    def __str__(self) -> str:
        return f"{self.recipient}:{self.status}"


FEISHU_BASE_FIELD_KEYS = frozenset(
    {
        "intake_id",
        "company_name",
        "contact_name",
        "email",
        "phone",
        "linkedin_url",
        "website",
        "source",
        "source_record_id",
        "research_summary",
        "research_facts",
        "source_urls",
        "qualification_score",
        "qualification_band",
        "qualification_reasons",
        "assigned_sales",
        "routing_reason",
        "crm_lead_id",
        "processed_at",
        "inspection_status",
    }
)


class FeishuBaseConnection(BaseOrgModel):
    """Tenant-owned Feishu app credentials and explicit Base field mapping."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="feishu_base_connection",
    )
    app_id = models.CharField(max_length=100, blank=True)
    app_secret_ciphertext = models.TextField(blank=True)
    app_secret_hint = models.CharField(max_length=12, blank=True)
    app_token = models.CharField(max_length=255, blank=True)
    table_id = models.CharField(max_length=255, blank=True)
    field_mapping = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=False)
    last_validated_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_feishu_base_connection"
        indexes = [
            models.Index(
                fields=["org", "is_active"],
                name="feishu_base_org_active_idx",
            )
        ]

    def set_app_secret(self, value: str) -> None:
        cleaned = value.strip()
        self.app_secret_ciphertext = encrypt_secret(cleaned)
        self.app_secret_hint = cleaned[-8:]

    def get_app_secret(self) -> str:
        if not self.app_secret_ciphertext:
            return ""
        return decrypt_secret(self.app_secret_ciphertext)

    def clean(self) -> None:
        super().clean()
        mapping = self.field_mapping
        if not isinstance(mapping, dict):
            raise ValidationError({"field_mapping": "Field mapping must be an object."})
        unknown = sorted(set(mapping) - FEISHU_BASE_FIELD_KEYS)
        if unknown:
            raise ValidationError(
                {"field_mapping": f"Unknown field mapping keys: {', '.join(unknown)}"}
            )
        target_names = []
        for key, value in mapping.items():
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    {"field_mapping": f'The mapping for "{key}" must be a field name.'}
                )
            if len(value.strip()) > 100:
                raise ValidationError(
                    {"field_mapping": f'The mapping for "{key}" is too long.'}
                )
            target_names.append(value.strip())
        if len(target_names) != len(set(target_names)):
            raise ValidationError(
                {"field_mapping": "Each Feishu field can be mapped only once."}
            )
        if self.is_active:
            required = {
                "app_id": self.app_id,
                "app_secret": self.app_secret_ciphertext,
                "app_token": self.app_token,
                "table_id": self.table_id,
                "intake_id field mapping": mapping.get("intake_id", ""),
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValidationError(
                    "Configure " + ", ".join(missing) + " before enabling Base sync."
                )

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        return f"{self.org_id}:{state}"


class FeishuBaseSyncStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    SYNCING = "syncing", "Syncing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class FeishuBaseSync(BaseOrgModel):
    """Current idempotent Base synchronization state for a researched intake."""

    connection = models.ForeignKey(
        FeishuBaseConnection,
        on_delete=models.PROTECT,
        related_name="syncs",
    )
    intake = models.OneToOneField(
        "sdr.LeadIntake",
        on_delete=models.CASCADE,
        related_name="feishu_base_sync",
    )
    status = models.CharField(
        max_length=16,
        choices=FeishuBaseSyncStatus.choices,
        default=FeishuBaseSyncStatus.PENDING,
    )
    record_id = models.CharField(max_length=255, blank=True)
    destination_sha256 = models.CharField(max_length=64)
    payload_sha256 = models.CharField(max_length=64)
    attempt_count = models.PositiveIntegerField(default=0)
    synced_field_names = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integration_feishu_base_sync"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="feishu_sync_org_status_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.org_id and self.connection.org_id != self.org_id:
            raise ValidationError("Feishu Base connection must belong to the org.")
        if self.org_id and self.intake.org_id != self.org_id:
            raise ValidationError("Feishu Base intake must belong to the org.")

    def __str__(self) -> str:
        return f"{self.intake_id}:{self.status}"
