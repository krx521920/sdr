"""Durable intake records for reliable, tenant-scoped SDR processing."""

from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from common.base import BaseOrgModel, OrgScopedManager, OrgScopedQuerySet
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


def validate_iana_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValidationError("Enter a valid IANA timezone.") from exc


def validate_send_weekdays(value) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(day, int) or day < 0 or day > 6 for day in value)
        or len(set(value)) != len(value)
    ):
        raise ValidationError("Send weekdays must be unique integers from 0 to 6.")


def default_send_weekdays() -> list[int]:
    return [0, 1, 2, 3, 4]


def default_compliance_channels() -> list[str]:
    return ["email", "whatsapp", "linkedin", "phone", "wechat"]


class SDRComplianceChannel(models.TextChoices):
    EMAIL = "email", "Email"
    WHATSAPP = "whatsapp", "WhatsApp"
    LINKEDIN = "linkedin", "LinkedIn"
    PHONE = "phone", "Phone"
    WECHAT = "wechat", "WeChat"


class SDRLawfulBasis(models.TextChoices):
    UNASSESSED = "unassessed", "Not assessed"
    CONSENT = "consent", "Consent"
    LEGITIMATE_INTEREST = "legitimate_interest", "Legitimate interest"
    CONTRACT = "contract", "Contract / pre-contract request"
    LEGAL_OBLIGATION = "legal_obligation", "Legal obligation"
    PUBLIC_TASK = "public_task", "Public task"
    VITAL_INTEREST = "vital_interest", "Vital interest"


class SDRCollectionMethod(models.TextChoices):
    INBOUND_FORM = "inbound_form", "Inbound form"
    DIRECT_MESSAGE = "direct_message", "Direct message"
    INBOUND_EMAIL = "inbound_email", "Inbound email"
    PROVIDER_API = "provider_api", "Provider API"
    CSV_IMPORT = "csv_import", "CSV import"
    MANUAL = "manual", "Manual entry"
    OTHER = "other", "Other"


class SDRRetentionMode(models.TextChoices):
    DISABLED = "disabled", "Disabled"
    AUDIT_ONLY = "audit_only", "Audit only"
    ANONYMIZE_SDR = "anonymize_sdr", "Anonymize SDR-owned data"


class SDRProvenanceStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RETENTION_DUE = "retention_due", "Retention review due"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    ANONYMIZED = "anonymized", "Anonymized"


class SDRDoNotContactReason(models.TextChoices):
    UNSUBSCRIBED = "unsubscribed", "Unsubscribed / objected"
    COMPLAINT = "complaint", "Complaint"
    ADMIN = "admin", "Administrator"
    REGULATORY = "regulatory", "Regulatory rule"
    DATA_REQUEST = "data_request", "Data subject request"
    INVALID = "invalid", "Invalid contact"


class SDRDoNotContactSource(models.TextChoices):
    EMAIL_SUPPRESSION = "email_suppression", "Email suppression"
    PROVIDER = "provider", "Provider"
    ADMIN = "admin", "Administrator"
    DATA_SUBJECT = "data_subject", "Data subject"
    RETENTION = "retention", "Retention workflow"
    SYSTEM = "system", "System"


class SDRComplianceEventType(models.TextChoices):
    PROVENANCE_RECORDED = "provenance_recorded", "Provenance recorded"
    CONTACT_ALLOWED = "contact_allowed", "Contact allowed"
    CONTACT_BLOCKED = "contact_blocked", "Contact blocked"
    DNC_ADDED = "dnc_added", "Do-not-contact added"
    DNC_RELEASED = "dnc_released", "Do-not-contact released"
    RETENTION_DUE = "retention_due", "Retention review due"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    DELETION_CANCELLED = "deletion_cancelled", "Deletion request cancelled"
    ANONYMIZED = "anonymized", "SDR data anonymized"


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
                name="sdr_cred_org_provider_idx",
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
    email_safety_enabled = models.BooleanField(default=True)
    org_daily_send_limit = models.PositiveIntegerField(
        default=1000,
        validators=[MinValueValidator(1), MaxValueValidator(100000)],
        help_text="Maximum nurture emails sent by the organization per local day.",
    )
    bounce_rate_threshold = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=5,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Campaign bounce percentage that triggers a safety hold.",
    )
    complaint_rate_threshold = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.1,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Campaign complaint percentage that triggers a safety hold.",
    )
    safety_min_sample_size = models.PositiveIntegerField(
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(1000000)],
    )
    safety_window_days = models.PositiveSmallIntegerField(
        default=7,
        validators=[MinValueValidator(1), MaxValueValidator(90)],
    )
    enforce_recipient_working_hours = models.BooleanField(default=False)
    default_recipient_timezone = models.CharField(
        max_length=64,
        default="UTC",
        validators=[validate_iana_timezone],
    )
    recipient_send_window_start = models.TimeField(default=time(9, 0))
    recipient_send_window_end = models.TimeField(default=time(17, 0))
    recipient_send_weekdays = models.JSONField(
        default=default_send_weekdays,
        validators=[validate_send_weekdays],
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


class SDRComplianceSettings(BaseOrgModel):
    """Tenant controls for contact eligibility and SDR data retention."""

    org = models.OneToOneField(
        "common.Org",
        on_delete=models.CASCADE,
        related_name="sdr_compliance_settings",
    )
    enforcement_enabled = models.BooleanField(default=False)
    require_lawful_basis = models.BooleanField(default=True)
    retention_mode = models.CharField(
        max_length=24,
        choices=SDRRetentionMode.choices,
        default=SDRRetentionMode.AUDIT_ONLY,
    )
    retention_days = models.PositiveIntegerField(
        default=730,
        validators=[MinValueValidator(30), MaxValueValidator(3650)],
    )
    deletion_grace_days = models.PositiveSmallIntegerField(
        default=30,
        validators=[MinValueValidator(0), MaxValueValidator(365)],
    )
    last_retention_scan_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sdr_compliance_settings"


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


class SDRDataProvenance(BaseOrgModel):
    """Auditable collection source and legal assessment for one intake."""

    intake = models.OneToOneField(
        LeadIntake,
        on_delete=models.CASCADE,
        related_name="data_provenance",
    )
    collection_method = models.CharField(
        max_length=24,
        choices=SDRCollectionMethod.choices,
        default=SDRCollectionMethod.OTHER,
    )
    source_url = models.URLField(max_length=1000, blank=True)
    lawful_basis = models.CharField(
        max_length=32,
        choices=SDRLawfulBasis.choices,
        default=SDRLawfulBasis.UNASSESSED,
    )
    lawful_basis_notes = models.TextField(blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    consent_evidence = models.TextField(blank=True)
    country_code = models.CharField(max_length=3, blank=True)
    allowed_channels = models.JSONField(
        default=default_compliance_channels,
        blank=True,
    )
    retention_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=24,
        choices=SDRProvenanceStatus.choices,
        default=SDRProvenanceStatus.ACTIVE,
    )
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    anonymized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sdr_data_provenance"
        indexes = [
            models.Index(
                fields=["org", "status", "retention_until"],
                name="sdr_prov_retention_idx",
            )
        ]


class SDRChannelComplianceRule(BaseOrgModel):
    """Country/channel rule; '*' is the organization-wide fallback."""

    country_code = models.CharField(max_length=3, default="*")
    channel = models.CharField(max_length=16, choices=SDRComplianceChannel.choices)
    is_allowed = models.BooleanField(default=True)
    requires_consent = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "sdr_channel_compliance_rule"
        ordering = ("country_code", "channel")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "country_code", "channel"],
                name="unique_sdr_country_channel_rule",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "channel", "country_code"],
                name="sdr_rule_channel_country_idx",
            )
        ]

    def save(self, *args, **kwargs):
        self.country_code = (self.country_code or "*").strip().upper()
        return super().save(*args, **kwargs)


class SDRDoNotContactEntry(BaseOrgModel):
    """Cross-channel contact objection / block list."""

    channel = models.CharField(max_length=16, choices=SDRComplianceChannel.choices)
    identifier = models.CharField(max_length=1000)
    identifier_hash = models.CharField(max_length=64)
    country_code = models.CharField(max_length=3, blank=True)
    reason = models.CharField(max_length=24, choices=SDRDoNotContactReason.choices)
    source = models.CharField(max_length=24, choices=SDRDoNotContactSource.choices)
    is_active = models.BooleanField(default=True)
    blocked_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "sdr_do_not_contact"
        ordering = ("-is_active", "-blocked_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "channel", "identifier_hash"],
                name="unique_sdr_channel_dnc",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "channel", "is_active", "identifier_hash"],
                name="sdr_dnc_active_lookup_idx",
            )
        ]


class AppendOnlyComplianceQuerySet(OrgScopedQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Compliance audit events cannot be updated")

    def delete(self):
        raise ValidationError("Compliance audit events cannot be deleted")


class AppendOnlyComplianceManager(OrgScopedManager):
    def get_queryset(self):
        return AppendOnlyComplianceQuerySet(self.model, using=self._db)


class AppendOnlyComplianceMixin:
    """Application guard; PostgreSQL adds an independent database trigger."""

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Compliance audit events cannot be updated")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Compliance audit events cannot be deleted")


class SDRComplianceEvent(AppendOnlyComplianceMixin, BaseOrgModel):
    """Immutable audit fact produced by compliance decisions and workflows."""

    intake = models.ForeignKey(
        LeadIntake,
        on_delete=models.SET_NULL,
        related_name="compliance_events",
        null=True,
        blank=True,
    )
    prospect = models.ForeignKey(
        "sdr.SDROutboundProspect",
        on_delete=models.SET_NULL,
        related_name="compliance_events",
        null=True,
        blank=True,
    )
    event_type = models.CharField(
        max_length=32,
        choices=SDRComplianceEventType.choices,
    )
    channel = models.CharField(
        max_length=16,
        choices=SDRComplianceChannel.choices,
        blank=True,
    )
    allowed = models.BooleanField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    event_key = models.CharField(max_length=255)
    snapshot = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    objects = AppendOnlyComplianceManager()

    class Meta:
        db_table = "sdr_compliance_event"
        ordering = ("-occurred_at", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "event_key"],
                name="unique_sdr_compliance_event",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "event_type", "-occurred_at"],
                name="sdr_compliance_event_idx",
            )
        ]


class SalesFeedbackDecision(models.TextChoices):
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    RECYCLE = "recycle", "Recycle / nurture"


class SalesFeedbackReason(models.TextChoices):
    GOOD_FIT = "good_fit", "Good fit"
    WRONG_INDUSTRY = "wrong_industry", "Wrong industry"
    WRONG_COMPANY_SIZE = "wrong_company_size", "Wrong company size"
    WRONG_ROLE = "wrong_role", "Wrong contact role"
    BAD_CONTACT = "bad_contact", "Invalid or unreachable contact"
    NO_NEED = "no_need", "No current need"
    NO_BUDGET = "no_budget", "No budget"
    BAD_TIMING = "bad_timing", "Bad timing"
    DUPLICATE = "duplicate", "Duplicate"
    OTHER = "other", "Other"


class SDRSalesFeedback(BaseOrgModel):
    """Current sales verdict for an SDR handoff, with immutable AI snapshots."""

    intake = models.OneToOneField(
        LeadIntake,
        on_delete=models.CASCADE,
        related_name="sales_feedback",
    )
    feedback_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="submitted_sdr_sales_feedback",
        null=True,
    )
    decision = models.CharField(max_length=16, choices=SalesFeedbackDecision.choices)
    reason = models.CharField(
        max_length=32,
        choices=SalesFeedbackReason.choices,
        blank=True,
    )
    quality_score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    satisfaction_score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    notes = models.CharField(max_length=1000, blank=True)
    qualification_score_snapshot = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )
    qualification_band_snapshot = models.CharField(max_length=32, blank=True)
    provider_snapshot = models.CharField(max_length=32, blank=True)
    model_snapshot = models.CharField(max_length=100, blank=True)
    prompt_version_snapshot = models.CharField(max_length=64, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "sdr_sales_feedback"
        ordering = ("-submitted_at", "-created_at")
        indexes = [
            models.Index(
                fields=["org", "decision", "-submitted_at"],
                name="sdr_feedback_org_decision_idx",
            ),
            models.Index(
                fields=["org", "qualification_band_snapshot", "-submitted_at"],
                name="sdr_feedback_org_band_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.intake_id and self.org_id != self.intake.org_id:
            errors["intake"] = (
                "Feedback and intake must belong to the same organization."
            )
        if self.feedback_by_id and self.org_id != self.feedback_by.org_id:
            errors["feedback_by"] = (
                "Feedback author must belong to the same organization."
            )
        if self.decision == SalesFeedbackDecision.ACCEPTED:
            if self.reason and self.reason != SalesFeedbackReason.GOOD_FIT:
                errors["reason"] = "Accepted feedback may only use the good-fit reason."
        elif not self.reason:
            errors["reason"] = "A reason is required for rejected or recycled leads."
        if self.reason == SalesFeedbackReason.OTHER and not self.notes.strip():
            errors["notes"] = "Add notes when the reason is Other."
        if errors:
            raise ValidationError(errors)


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
    deferral_count = models.PositiveIntegerField(default=0)
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
                name="sdr_event_org_type_idx",
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


class OutboundSourceProvider(models.TextChoices):
    APOLLO = "apollo", "Apollo"


class OutboundCopyDraftStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    GENERATING = "generating", "Generating"
    READY = "ready", "Ready for review"
    FAILED = "failed", "Failed"
    APPLIED = "applied", "Applied"


class SDROutboundCampaign(BaseOrgModel):
    """Tenant-owned ICP campaign grouping a cleaned outbound prospect list."""

    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    icp_description = models.TextField(blank=True)
    channels = models.JSONField(default=list, blank=True)
    linkedin_invitation_message = models.CharField(max_length=300, blank=True)
    whatsapp_template_name = models.CharField(max_length=512, blank=True)
    whatsapp_template_language = models.CharField(max_length=20, default="en_US")
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
    safety_hold = models.BooleanField(default=False)
    safety_paused_at = models.DateTimeField(null=True, blank=True)
    safety_cleared_at = models.DateTimeField(null=True, blank=True)
    safety_pause_reason = models.CharField(max_length=500, blank=True)
    safety_snapshot = models.JSONField(default=dict, blank=True)

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
    recipient_timezone = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_iana_timezone],
    )
    source_url = models.URLField(max_length=1000, blank=True)
    notes = models.TextField(blank=True)
    lawful_basis = models.CharField(
        max_length=32,
        choices=SDRLawfulBasis.choices,
        default=SDRLawfulBasis.UNASSESSED,
    )
    lawful_basis_notes = models.TextField(blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    consent_evidence = models.TextField(blank=True)
    allowed_channels = models.JSONField(
        default=default_compliance_channels,
        blank=True,
    )
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


class SDROutboundSource(BaseOrgModel):
    """Recurring provider query that imports prospects into one campaign."""

    campaign = models.ForeignKey(
        SDROutboundCampaign,
        on_delete=models.CASCADE,
        related_name="sources",
    )
    name = models.CharField(max_length=160)
    provider = models.CharField(
        max_length=24,
        choices=OutboundSourceProvider.choices,
        default=OutboundSourceProvider.APOLLO,
    )
    is_active = models.BooleanField(default=False)
    search_filters = models.JSONField(default=dict)
    interval_hours = models.PositiveSmallIntegerField(
        default=24,
        validators=[MinValueValidator(1), MaxValueValidator(168)],
    )
    max_results_per_sync = models.PositiveSmallIntegerField(
        default=25,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Maximum Apollo enrichment requests made by one source sync.",
    )
    enrichment_credits_acknowledged = models.BooleanField(default=False)
    next_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_job_id = models.UUIDField(null=True, blank=True)
    next_page = models.PositiveSmallIntegerField(default=1)
    sync_count = models.PositiveIntegerField(default=0)
    last_sync_stats = models.JSONField(default=dict, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    last_error_message = models.CharField(max_length=1000, blank=True)

    class Meta:
        db_table = "sdr_outbound_source"
        ordering = ("name", "created_at")
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "campaign",
                "org",
                name="unique_sdr_out_source_name",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "is_active", "next_sync_at"],
                name="sdr_out_source_due_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.campaign_id and self.org_id and self.campaign.org_id != self.org_id:
            raise ValidationError("Outbound source campaign must belong to the org")
        if self.is_active and not self.enrichment_credits_acknowledged:
            raise ValidationError(
                "Acknowledge Apollo enrichment credit usage before enabling the source"
            )

    def __str__(self) -> str:
        return f"{self.campaign}:{self.name}"


class ApolloCandidateStatus(models.TextChoices):
    PENDING_ENRICHMENT_APPROVAL = (
        "pending_enrichment_approval",
        "Pending enrichment approval",
    )
    ENRICHMENT_RESERVED = "enrichment_reserved", "Enrichment reserved"
    IMPORT_QUEUED = "import_queued", "Person import queued"
    IMPORTED = "imported", "Imported"
    IMPORT_REVIEW_REQUIRED = "import_review_required", "Import review required"
    IMPORT_FAILED = "import_failed", "Import failed"
    IMPORT_RETRY_REQUIRED = "import_retry_required", "Import retry required"
    UNKNOWN = "unknown", "Unknown provider outcome"
    SKIPPED = "skipped", "Skipped"


class SDRApolloCandidate(BaseOrgModel):
    """Minimal, encrypted Apollo search result awaiting explicit enrichment approval."""

    source = models.ForeignKey(
        SDROutboundSource,
        on_delete=models.CASCADE,
        related_name="apollo_candidates",
    )
    search_request = models.ForeignKey(
        "integrations.ExternalExecutionRequest",
        on_delete=models.PROTECT,
        related_name="apollo_search_candidates",
    )
    enrichment_request = models.ForeignKey(
        "integrations.ExternalExecutionRequest",
        on_delete=models.PROTECT,
        related_name="apollo_enrichment_candidates",
        null=True,
        blank=True,
    )
    import_batch = models.ForeignKey(
        "matching.PersonImportBatch",
        on_delete=models.SET_NULL,
        related_name="apollo_candidates",
        null=True,
        blank=True,
    )
    provider_person_id_ciphertext = models.TextField()
    provider_person_id_hash = models.CharField(max_length=64)
    safe_label = models.CharField(max_length=120)
    status = models.CharField(
        max_length=40,
        choices=ApolloCandidateStatus.choices,
        default=ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL,
    )

    class Meta:
        db_table = "sdr_apollo_candidate"
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "source", "provider_person_id_hash"],
                name="unique_sdr_apollo_candidate",
            )
        ]
        indexes = [
            models.Index(
                fields=["org", "source", "status"],
                name="sdr_apollo_cand_status_idx",
            )
        ]

    def set_provider_person_id(self, value: str) -> None:
        self.provider_person_id_ciphertext = encrypt_secret(value)

    def get_provider_person_id(self) -> str:
        return decrypt_secret(self.provider_person_id_ciphertext)

    def clean(self) -> None:
        super().clean()
        if self.source_id and self.org_id and self.source.org_id != self.org_id:
            raise ValidationError("Apollo candidate source must belong to the org")
        if (
            self.search_request_id
            and self.org_id
            and self.search_request.org_id != self.org_id
        ):
            raise ValidationError("Apollo search request must belong to the org")
        if (
            self.enrichment_request_id
            and self.org_id
            and self.enrichment_request.org_id != self.org_id
        ):
            raise ValidationError("Apollo enrichment request must belong to the org")
        if (
            self.import_batch_id
            and self.org_id
            and self.import_batch.org_id != self.org_id
        ):
            raise ValidationError("Apollo import batch must belong to the org")


class SDROutboundCopyDraft(BaseOrgModel):
    """AI-generated campaign copy that requires an explicit human apply action."""

    campaign = models.ForeignKey(
        SDROutboundCampaign,
        on_delete=models.CASCADE,
        related_name="copy_drafts",
    )
    status = models.CharField(
        max_length=16,
        choices=OutboundCopyDraftStatus.choices,
        default=OutboundCopyDraftStatus.PENDING,
    )
    language = models.CharField(max_length=40, default="English")
    tone = models.CharField(max_length=80, default="concise and consultative")
    offering_summary = models.TextField()
    value_proposition = models.TextField()
    proof_points = models.TextField(blank=True)
    cta_goal = models.CharField(max_length=500)
    step_count = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    generated_steps = models.JSONField(default=list, blank=True)
    provider = models.CharField(max_length=24, blank=True)
    model = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=80, blank=True)
    provider_response_id = models.CharField(max_length=255, blank=True)
    provider_attempts = models.JSONField(default=list, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    last_job_id = models.UUIDField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="reviewed_sdr_outbound_copy_drafts",
        null=True,
        blank=True,
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)

    class Meta:
        db_table = "sdr_outbound_copy_draft"
        ordering = ("-created_at", "id")
        indexes = [
            models.Index(
                fields=["org", "campaign", "-created_at"],
                name="sdr_copy_draft_campaign_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.campaign_id and self.org_id and self.campaign.org_id != self.org_id:
            raise ValidationError("Outbound copy draft campaign must belong to the org")
        if self.reviewed_by_id and self.reviewed_by.org_id != self.org_id:
            raise ValidationError("Outbound copy reviewer must belong to the org")

    def __str__(self) -> str:
        return f"{self.campaign}:copy:{self.status}"
