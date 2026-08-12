"""Durable intake records for reliable, tenant-scoped SDR processing."""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from common.base import BaseOrgModel
from common.secrets import decrypt_secret, encrypt_secret


class LeadIntakeStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class LeadIntakeSource(models.TextChoices):
    FACEBOOK_AD = "facebook_ad", "Facebook Lead Ad"
    FACEBOOK_MESSENGER = "facebook_messenger", "Facebook Messenger"
    WEBSITE_FORM = "website_form", "Website Form"
    LINKEDIN = "linkedin", "LinkedIn"
    EMAIL = "email", "Email"
    API = "api", "API"
    MANUAL = "manual", "Manual"
    OUTBOUND = "outbound", "Outbound Prospect"


DEFAULT_ACKNOWLEDGEMENT_SUBJECT = "Thanks for contacting {{ organization_name }}"
DEFAULT_ACKNOWLEDGEMENT_BODY = """Hi {{ first_name }},

Thanks for contacting {{ organization_name }}. We have received your request and a member of our team will follow up shortly.

Best,
{{ organization_name }}"""


class SDRRoutingStrategy(models.TextChoices):
    LEAST_LOADED = "least_loaded", "Least loaded"
    ROUND_ROBIN = "round_robin", "Round robin"
    DIRECT = "direct", "Direct"


class SDRRoutingRule(BaseOrgModel):
    """Ordered, tenant-owned rule for assigning normalized SDR leads."""

    name = models.CharField(max_length=160)
    priority = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    strategy = models.CharField(
        max_length=24,
        choices=SDRRoutingStrategy.choices,
        default=SDRRoutingStrategy.LEAST_LOADED,
    )
    countries = models.JSONField(default=list, blank=True)
    sources = models.JSONField(default=list, blank=True)
    qualification_bands = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "sdr_routing_rule"
        ordering = ("priority", "created_at", "id")
        indexes = [
            models.Index(
                fields=["org", "is_active", "priority"],
                name="sdr_rule_org_active_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class SDRRoutingRuleMember(BaseOrgModel):
    """A sales profile participating in one SDR routing rule."""

    rule = models.ForeignKey(
        SDRRoutingRule,
        on_delete=models.CASCADE,
        related_name="members",
    )
    profile = models.ForeignKey(
        "common.Profile",
        on_delete=models.CASCADE,
        related_name="sdr_routing_memberships",
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sdr_routing_rule_member"
        ordering = ("position", "created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "profile"],
                name="unique_profile_per_sdr_routing_rule",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "rule", "position"],
                name="sdr_member_org_rule_idx",
            ),
        ]


class SDRRoutingRuleState(BaseOrgModel):
    """Mutable cursor kept separate from the administrator-authored rule."""

    rule = models.OneToOneField(
        SDRRoutingRule,
        on_delete=models.CASCADE,
        related_name="state",
    )
    next_index = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "sdr_routing_rule_state"


class LeadInspectionStatus(models.TextChoices):
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class SDRModelProvider(models.TextChoices):
    OPENAI = "openai", "OpenAI"
    DOUBAO = "doubao", "Doubao / Volcengine Ark"
    DEEPSEEK = "deepseek", "DeepSeek"


class LeadInspectionFallbackKind(models.TextChoices):
    NONE = "", "None"
    MODEL = "model", "Model provider"
    RULES = "rules", "Deterministic rules"


class SDRIntelligenceSettings(BaseOrgModel):
    """Tenant ICP and cost/latency controls for the lead inspector."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="sdr_intelligence_settings",
    )
    is_enabled = models.BooleanField(default=False)
    research_enabled = models.BooleanField(default=True)
    ai_scoring_enabled = models.BooleanField(default=True)
    provider = models.CharField(
        max_length=24,
        choices=SDRModelProvider.choices,
        default=SDRModelProvider.OPENAI,
    )
    model = models.CharField(max_length=100, default="gpt-5.6-luna")
    reasoning_effort = models.CharField(
        max_length=16,
        choices=[
            ("none", "None"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("xhigh", "Extra high"),
            ("max", "Maximum"),
        ],
        default="low",
    )
    fallback_provider = models.CharField(
        max_length=24,
        choices=SDRModelProvider.choices,
        blank=True,
    )
    fallback_model = models.CharField(max_length=100, blank=True)
    fallback_reasoning_effort = models.CharField(
        max_length=16,
        choices=[
            ("none", "None"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("xhigh", "Extra high"),
            ("max", "Maximum"),
        ],
        default="low",
    )
    icp_description = models.TextField(blank=True)
    positive_signals = models.TextField(blank=True)
    negative_signals = models.TextField(blank=True)
    max_research_pages = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(3)],
    )
    website_timeout_seconds = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(15)],
    )

    class Meta:
        db_table = "sdr_intelligence_settings"


class SDRModelCredential(BaseOrgModel):
    """Encrypted tenant BYOK credential for one allow-listed model provider."""

    provider = models.CharField(max_length=24, choices=SDRModelProvider.choices)
    api_key_ciphertext = models.TextField()
    api_key_hint = models.CharField(max_length=12, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "sdr_model_credential"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "provider"],
                name="unique_sdr_model_credential_per_org",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "provider", "is_active"],
                name="sdr_credential_org_provider_idx",
            )
        ]

    def set_api_key(self, api_key: str) -> None:
        cleaned = api_key.strip()
        self.api_key_ciphertext = encrypt_secret(cleaned)
        self.api_key_hint = cleaned[-8:]

    def get_api_key(self) -> str:
        return decrypt_secret(self.api_key_ciphertext)

    def __str__(self) -> str:
        return f"{self.org_id}:{self.provider}"


class SDRResponseSettings(BaseOrgModel):
    """Tenant-owned controls for acknowledgement and sales handoff delivery."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="sdr_response_settings",
    )
    acknowledgement_email_enabled = models.BooleanField(default=False)
    acknowledgement_subject = models.CharField(
        max_length=255,
        default=DEFAULT_ACKNOWLEDGEMENT_SUBJECT,
    )
    acknowledgement_body = models.TextField(default=DEFAULT_ACKNOWLEDGEMENT_BODY)
    acknowledgement_from_email = models.EmailField(blank=True)
    sales_in_app_enabled = models.BooleanField(default=True)
    feishu_enabled = models.BooleanField(default=False)
    feishu_webhook_ciphertext = models.TextField(blank=True)
    feishu_webhook_hint = models.CharField(max_length=12, blank=True)
    response_sla_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(1), MaxValueValidator(86400)],
    )

    class Meta:
        db_table = "sdr_response_settings"

    def set_feishu_webhook(self, webhook_url: str) -> None:
        cleaned = webhook_url.strip()
        self.feishu_webhook_ciphertext = encrypt_secret(cleaned)
        self.feishu_webhook_hint = cleaned[-8:]

    def get_feishu_webhook(self) -> str:
        if not self.feishu_webhook_ciphertext:
            return ""
        return decrypt_secret(self.feishu_webhook_ciphertext)

    def clear_feishu_webhook(self) -> None:
        self.feishu_webhook_ciphertext = ""
        self.feishu_webhook_hint = ""


class LeadInspection(BaseOrgModel):
    """Auditable research and qualification outcome for one intake."""

    intake = models.OneToOneField(
        "sdr.LeadIntake",
        on_delete=models.CASCADE,
        related_name="inspection",
    )
    status = models.CharField(
        max_length=16,
        choices=LeadInspectionStatus.choices,
        default=LeadInspectionStatus.RUNNING,
    )
    website_url = models.URLField(max_length=1000, blank=True)
    source_urls = models.JSONField(default=list, blank=True)
    research_summary = models.TextField(blank=True)
    research_facts = models.JSONField(default=dict, blank=True)
    content_sha256 = models.CharField(max_length=64, blank=True)
    provider = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=64, blank=True)
    configuration_sha256 = models.CharField(max_length=64, blank=True)
    provider_response_id = models.CharField(max_length=255, blank=True)
    qualification_score = models.PositiveSmallIntegerField(null=True, blank=True)
    qualification_band = models.CharField(max_length=32, blank=True)
    qualification_reasons = models.JSONField(default=list, blank=True)
    used_fallback = models.BooleanField(default=False)
    fallback_kind = models.CharField(
        max_length=16,
        choices=LeadInspectionFallbackKind.choices,
        blank=True,
    )
    provider_attempts = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sdr_lead_inspection"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="sdr_inspect_org_status_idx",
            )
        ]


class LeadIntake(BaseOrgModel):
    source = models.CharField(max_length=32, choices=LeadIntakeSource.choices)
    source_record_id = models.CharField(max_length=255)
    raw_payload = models.JSONField(default=dict)
    normalized_payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=LeadIntakeStatus.choices,
        default=LeadIntakeStatus.RECEIVED,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    qualification_score = models.PositiveSmallIntegerField(null=True, blank=True)
    qualification_band = models.CharField(max_length=32, blank=True)
    matched_existing = models.BooleanField(default=False)
    crm_created = models.BooleanField(null=True, blank=True)
    crm_lead = models.ForeignKey(
        "leads.Lead",
        on_delete=models.SET_NULL,
        related_name="sdr_intakes",
        null=True,
        blank=True,
    )
    assigned_profile = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="sdr_intakes",
        null=True,
        blank=True,
    )
    routing_rule = models.ForeignKey(
        SDRRoutingRule,
        on_delete=models.SET_NULL,
        related_name="lead_intakes",
        null=True,
        blank=True,
    )
    routing_reason = models.CharField(max_length=500, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sdr_lead_intake"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "source", "source_record_id"],
                name="unique_sdr_intake_source_record_per_org",
            )
        ]
        indexes = [
            models.Index(fields=["org", "status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.source_record_id}"


class LeadLifecycleEventType(models.TextChoices):
    RECEIVED = "received", "Received"
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    QUALIFIED = "qualified", "Qualified"
    ASSIGNED = "assigned", "Assigned"
    CRM_HANDOFF = "crm_handoff", "CRM handoff"
    ACKNOWLEDGEMENT_SENT = "acknowledgement_sent", "Acknowledgement sent"
    SALES_NOTIFIED = "sales_notified", "Sales notified"
    NURTURE_ENROLLED = "nurture_enrolled", "Nurture enrolled"
    NURTURE_EMAIL_SENT = "nurture_email_sent", "Nurture email sent"
    NURTURE_EMAIL_OPENED = "nurture_email_opened", "Nurture email opened"
    NURTURE_LINK_CLICKED = "nurture_link_clicked", "Nurture link clicked"
    NURTURE_SUPPRESSED = "nurture_suppressed", "Nurture suppressed"
    NURTURE_DELIVERED = "nurture_delivered", "Nurture delivered"
    NURTURE_BOUNCED = "nurture_bounced", "Nurture bounced"
    NURTURE_COMPLAINED = "nurture_complained", "Nurture complained"
    CHANNEL_MESSAGE_RECEIVED = "channel_message_received", "Channel message received"
    NURTURE_STOPPED = "nurture_stopped", "Nurture stopped"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class LeadLifecycleEvent(BaseOrgModel):
    """Idempotent business milestones for an inbound lead."""

    intake = models.ForeignKey(
        LeadIntake,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )
    event_type = models.CharField(max_length=40, choices=LeadLifecycleEventType.choices)
    event_key = models.CharField(max_length=120)
    data = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "sdr_lead_lifecycle_event"
        ordering = ("occurred_at", "created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "intake", "event_key"],
                name="unique_sdr_lifecycle_event_key",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "event_type", "-occurred_at"],
                name="sdr_lifecycle_org_type_idx",
            )
        ]


class LeadDeliveryKind(models.TextChoices):
    ACKNOWLEDGEMENT_EMAIL = "acknowledgement_email", "Acknowledgement email"
    SALES_IN_APP = "sales_in_app", "Sales in-app notification"
    SALES_FEISHU = "sales_feishu", "Sales Feishu notification"


class LeadDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class LeadDelivery(BaseOrgModel):
    """Idempotency and audit record for one customer or sales message."""

    intake = models.ForeignKey(
        LeadIntake,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    kind = models.CharField(max_length=40, choices=LeadDeliveryKind.choices)
    recipient = models.CharField(max_length=500)
    status = models.CharField(
        max_length=16,
        choices=LeadDeliveryStatus.choices,
        default=LeadDeliveryStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    last_error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sdr_lead_delivery"
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "intake", "kind", "recipient"],
                name="unique_sdr_lead_delivery",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="sdr_delivery_org_status_idx",
            )
        ]


class NurtureEnrollmentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    REPLIED = "replied", "Replied"
    CONVERTED = "converted", "Converted"


class NurtureDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class NurtureReplySentiment(models.TextChoices):
    POSITIVE = "positive", "Positive"
    NEUTRAL = "neutral", "Neutral"
    NEGATIVE = "negative", "Negative"


class SDRNurtureSequence(BaseOrgModel):
    """Ordered email journey selected from source and qualification rules."""

    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    priority = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=False)
    auto_enroll = models.BooleanField(default=False)
    sources = models.JSONField(default=list, blank=True)
    qualification_bands = models.JSONField(default=list, blank=True)
    from_email = models.EmailField(blank=True)

    class Meta:
        db_table = "sdr_nurture_sequence"
        ordering = ("priority", "created_at", "id")
        indexes = [
            models.Index(
                fields=["org", "is_active", "auto_enroll", "priority"],
                name="sdr_nurture_org_active_idx",
            )
        ]

    def __str__(self) -> str:
        return self.name


class SDRNurtureStep(BaseOrgModel):
    """One delayed message with an optional deterministic B variant."""

    sequence = models.ForeignKey(
        SDRNurtureSequence,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    position = models.PositiveSmallIntegerField()
    delay_minutes = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(525600)],
        help_text="Delay after enrollment or the preceding successful step.",
    )
    subject_a = models.CharField(max_length=255)
    body_a = models.TextField()
    subject_b = models.CharField(max_length=255, blank=True)
    body_b = models.TextField(blank=True)
    variant_b_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    class Meta:
        db_table = "sdr_nurture_step"
        ordering = ("position", "created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "sequence", "position"],
                name="unique_sdr_nurture_step_position",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "sequence", "position"],
                name="sdr_nurture_step_org_seq_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sequence}: step {self.position}"


class LeadNurtureEnrollment(BaseOrgModel):
    """A lead's durable progress through one nurture sequence."""

    sequence = models.ForeignKey(
        SDRNurtureSequence,
        on_delete=models.PROTECT,
        related_name="enrollments",
    )
    intake = models.OneToOneField(
        LeadIntake,
        on_delete=models.CASCADE,
        related_name="nurture_enrollment",
    )
    lead = models.ForeignKey(
        "leads.Lead",
        on_delete=models.SET_NULL,
        related_name="sdr_nurture_enrollments",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=16,
        choices=NurtureEnrollmentStatus.choices,
        default=NurtureEnrollmentStatus.ACTIVE,
    )
    current_step_position = models.PositiveSmallIntegerField(default=0)
    resume_count = models.PositiveIntegerField(default=0)
    next_run_at = models.DateTimeField(null=True, blank=True)
    enrolled_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    stop_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = "sdr_nurture_enrollment"
        ordering = ("-enrolled_at", "-created_at")
        indexes = [
            models.Index(
                fields=["org", "status", "next_run_at"],
                name="sdr_nurture_enroll_status_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sequence}:{self.intake_id}"


class LeadNurtureDelivery(BaseOrgModel):
    """Auditable email attempt and immutable A/B template snapshot."""

    enrollment = models.ForeignKey(
        LeadNurtureEnrollment,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    step = models.ForeignKey(
        SDRNurtureStep,
        on_delete=models.SET_NULL,
        related_name="deliveries",
        null=True,
        blank=True,
    )
    step_position = models.PositiveSmallIntegerField()
    variant = models.CharField(
        max_length=1,
        choices=(("A", "A"), ("B", "B")),
        default="A",
    )
    recipient = models.EmailField()
    subject_template = models.CharField(max_length=255)
    body_template = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=NurtureDeliveryStatus.choices,
        default=NurtureDeliveryStatus.PENDING,
    )
    scheduled_for = models.DateTimeField()
    attempt_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    last_error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    complained_at = models.DateTimeField(null=True, blank=True)
    bounce_type = models.CharField(max_length=32, blank=True)
    bounce_subtype = models.CharField(max_length=64, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    open_count = models.PositiveIntegerField(default=0)
    click_count = models.PositiveIntegerField(default=0)
    last_clicked_url = models.URLField(max_length=2048, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    reply_message_id = models.CharField(max_length=512, blank=True)
    reply_sentiment = models.CharField(
        max_length=16,
        choices=NurtureReplySentiment.choices,
        blank=True,
    )

    class Meta:
        db_table = "sdr_nurture_delivery"
        ordering = ("scheduled_for", "created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "enrollment", "step_position"],
                name="unique_sdr_nurture_delivery_step",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "scheduled_for"],
                name="sdr_nurture_delivery_due_idx",
            ),
            models.Index(
                fields=["org", "variant", "sent_at"],
                name="sdr_nurture_variant_sent_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.enrollment_id}:step-{self.step_position}{self.variant}"


class NurtureInteractionType(models.TextChoices):
    OPEN = "open", "Open"
    CLICK = "click", "Click"


class LeadNurtureInteraction(BaseOrgModel):
    """Privacy-preserving, deduplicated recipient interaction audit row."""

    delivery = models.ForeignKey(
        LeadNurtureDelivery,
        on_delete=models.CASCADE,
        related_name="interactions",
    )
    event_type = models.CharField(
        max_length=12,
        choices=NurtureInteractionType.choices,
    )
    target_url = models.URLField(max_length=2048, blank=True)
    target_hash = models.CharField(max_length=64)
    visitor_hash = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "sdr_nurture_interaction"
        ordering = ("occurred_at", "created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "delivery",
                    "event_type",
                    "target_hash",
                    "visitor_hash",
                ],
                name="unique_sdr_nurture_interaction",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "event_type", "-occurred_at"],
                name="sdr_nurture_interact_event_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.delivery_id}:{self.event_type}"


class EmailSuppressionReason(models.TextChoices):
    UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
    COMPLAINT = "complaint", "Spam complaint"
    HARD_BOUNCE = "hard_bounce", "Hard bounce"
    INVALID = "invalid", "Invalid address"
    ADMIN = "admin", "Administrator"


class EmailSuppressionSource(models.TextChoices):
    ONE_CLICK = "one_click", "One-click unsubscribe"
    INBOUND_REPLY = "inbound_reply", "Inbound email reply"
    PROVIDER = "provider", "Email provider"
    ADMIN = "admin", "Administrator"


class SDREmailSuppression(BaseOrgModel):
    """Tenant-owned email opt-out and deliverability suppression state."""

    email = models.EmailField()
    reason = models.CharField(
        max_length=24,
        choices=EmailSuppressionReason.choices,
    )
    source = models.CharField(
        max_length=24,
        choices=EmailSuppressionSource.choices,
    )
    is_active = models.BooleanField(default=True)
    suppressed_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)
    source_delivery = models.ForeignKey(
        LeadNurtureDelivery,
        on_delete=models.SET_NULL,
        related_name="suppressions",
        null=True,
        blank=True,
    )
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "sdr_email_suppression"
        ordering = ("-is_active", "-suppressed_at", "email")
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                "org",
                name="unique_sdr_email_suppression",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "is_active", "-suppressed_at"],
                name="sdr_suppression_org_active_idx",
            )
        ]

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.email}:{self.reason}"


class EmailProviderEventType(models.TextChoices):
    DELIVERY = "delivery", "Delivery"
    BOUNCE = "bounce", "Bounce"
    COMPLAINT = "complaint", "Complaint"


class SDREmailProviderEvent(BaseOrgModel):
    """Idempotent, sanitized delivery feedback received from an email provider."""

    delivery = models.ForeignKey(
        LeadNurtureDelivery,
        on_delete=models.CASCADE,
        related_name="provider_events",
    )
    provider = models.CharField(
        max_length=24,
        choices=(("ses", "AWS SES"),),
        default="ses",
    )
    provider_event_id = models.CharField(max_length=255)
    event_type = models.CharField(
        max_length=24,
        choices=EmailProviderEventType.choices,
    )
    event_at = models.DateTimeField(default=timezone.now)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "sdr_email_provider_event"
        ordering = ("-event_at", "-created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "provider", "provider_event_id"],
                name="unique_sdr_email_provider_event",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "event_type", "-event_at"],
                name="sdr_provider_event_org_type_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.provider_event_id}"


class OutboundCampaignStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    COMPLETED = "completed", "Completed"
    ARCHIVED = "archived", "Archived"


class OutboundProspectStatus(models.TextChoices):
    READY = "ready", "Ready"
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    PROMOTED = "promoted", "Promoted to CRM"
    FAILED = "failed", "Failed"
    DISQUALIFIED = "disqualified", "Disqualified"


class SDROutboundCampaign(BaseOrgModel):
    """Tenant-owned ICP campaign grouping a cleaned outbound prospect list."""

    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    icp_description = models.TextField(blank=True)
    channels = models.JSONField(default=list, blank=True)
    sequence = models.ForeignKey(
        SDRNurtureSequence,
        on_delete=models.PROTECT,
        related_name="outbound_campaigns",
        null=True,
        blank=True,
    )
    daily_send_limit = models.PositiveSmallIntegerField(
        default=50,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
        help_text="Maximum prospects released into the SDR pipeline per local day.",
    )
    status = models.CharField(
        max_length=16,
        choices=OutboundCampaignStatus.choices,
        default=OutboundCampaignStatus.DRAFT,
    )
    owner = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="sdr_outbound_campaigns",
        null=True,
        blank=True,
    )
    launched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    run_count = models.PositiveIntegerField(default=0)
    last_refilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sdr_outbound_campaign"
        ordering = ("-created_at", "id")
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "org",
                name="unique_sdr_out_campaign_name",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"],
                name="sdr_out_campaign_status_idx",
            )
        ]

    def __str__(self) -> str:
        return self.name


class SDROutboundProspect(BaseOrgModel):
    """Cleaned, tenant-deduplicated prospect awaiting SDR promotion."""

    campaign = models.ForeignKey(
        SDROutboundCampaign,
        on_delete=models.PROTECT,
        related_name="prospects",
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    job_title = models.CharField(max_length=255, blank=True)
    linkedin_url = models.URLField(max_length=500, blank=True)
    company_name = models.CharField(max_length=255)
    website = models.URLField(max_length=500, blank=True)
    industry = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=100, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    notes = models.TextField(blank=True)
    dedupe_key = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=OutboundProspectStatus.choices,
        default=OutboundProspectStatus.READY,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    last_error_message = models.TextField(blank=True)
    intake = models.OneToOneField(
        LeadIntake,
        on_delete=models.SET_NULL,
        related_name="outbound_prospect",
        null=True,
        blank=True,
    )
    promoted_at = models.DateTimeField(null=True, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    queued_run = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sdr_outbound_prospect"
        ordering = ("-created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "dedupe_key"],
                name="unique_sdr_out_prospect_key",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "campaign", "status", "-created_at"],
                name="sdr_out_prospect_status_idx",
            )
        ]

    def __str__(self) -> str:
        contact = " ".join(filter(None, (self.first_name, self.last_name)))
        return contact or self.email or self.company_name
