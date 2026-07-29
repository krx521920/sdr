"""Durable intake records for reliable, tenant-scoped SDR processing."""

from django.db import models

from common.base import BaseOrgModel


class LeadIntakeStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class LeadIntakeSource(models.TextChoices):
    FACEBOOK_AD = "facebook_ad", "Facebook Lead Ad"
    WEBSITE_FORM = "website_form", "Website Form"
    LINKEDIN = "linkedin", "LinkedIn"
    EMAIL = "email", "Email"
    API = "api", "API"
    MANUAL = "manual", "Manual"


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
