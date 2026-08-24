"""Tenant-owned people, evidence, opportunities, and explainable matches."""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from common.base import BaseOrgModel


class PersonStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    ARCHIVED = "archived", "Archived"


class PersonAvailability(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    AVAILABLE = "available", "Available"
    OPEN_TO_OFFERS = "open_to_offers", "Open to offers"
    BUSY = "busy", "Busy"
    UNAVAILABLE = "unavailable", "Unavailable"


class PersonIdentityKind(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Phone"
    LINKEDIN = "linkedin", "LinkedIn"
    WHATSAPP = "whatsapp", "WhatsApp"
    WECHAT = "wechat", "WeChat"
    EXTERNAL = "external", "External"


class EvidenceKind(models.TextChoices):
    PROFILE = "profile", "Profile"
    SKILL = "skill", "Skill"
    EXPERIENCE = "experience", "Experience"
    RELATIONSHIP = "relationship", "Relationship"
    INTERACTION = "interaction", "Interaction"
    AVAILABILITY = "availability", "Availability"
    PREFERENCE = "preference", "Preference"
    VERIFICATION = "verification", "Verification"
    OTHER = "other", "Other"


class EvidenceSource(models.TextChoices):
    CRM = "crm", "CRM"
    APOLLO = "apollo", "Apollo"
    LINKEDIN = "linkedin", "LinkedIn"
    WHATSAPP = "whatsapp", "WhatsApp"
    WECHAT = "wechat", "WeChat"
    FEISHU = "feishu", "Feishu"
    EMAIL = "email", "Email"
    MANUAL = "manual", "Manual"
    AI = "ai", "AI"
    OTHER = "other", "Other"


class MatchOpportunityType(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    EMPLOYMENT = "employment", "Employment"
    CONTRACTOR = "contractor", "Contractor"
    PROJECT = "project", "Project"
    EXPERT = "expert", "Expert"
    REFERRAL = "referral", "Referral"
    PARTNERSHIP = "partnership", "Partnership"


class MatchOpportunityStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    PAUSED = "paused", "Paused"
    FILLED = "filled", "Filled"
    CLOSED = "closed", "Closed"


class MatchStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    REVIEWING = "reviewing", "Reviewing"
    SHORTLISTED = "shortlisted", "Shortlisted"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"


class MatchEvidenceDirection(models.TextChoices):
    POSITIVE = "positive", "Positive"
    NEGATIVE = "negative", "Negative"
    NEUTRAL = "neutral", "Neutral"


def default_scoring_weights():
    return {
        "skills": 45,
        "titles": 20,
        "locations": 15,
        "availability": 20,
    }


class Person(BaseOrgModel):
    """A canonical person assembled from one or more channel identities."""

    display_name = models.CharField(max_length=255)
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    headline = models.CharField(max_length=500, blank=True)
    summary = models.TextField(blank=True)
    current_title = models.CharField(max_length=255, blank=True)
    current_company = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=255, blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    skills = models.JSONField(default=list)
    roles = models.JSONField(default=list)
    attributes = models.JSONField(default=dict)
    availability = models.CharField(
        max_length=24,
        choices=PersonAvailability.choices,
        default=PersonAvailability.UNKNOWN,
    )
    status = models.CharField(
        max_length=16,
        choices=PersonStatus.choices,
        default=PersonStatus.ACTIVE,
    )

    class Meta:
        db_table = "matching_person"
        ordering = ("display_name", "id")
        indexes = [
            models.Index(fields=["org", "status", "display_name"]),
            models.Index(fields=["org", "availability", "-updated_at"]),
        ]

    def __str__(self):
        return self.display_name


class PersonIdentity(BaseOrgModel):
    """A normalized channel address for a person, unique inside one tenant."""

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="identities",
    )
    kind = models.CharField(max_length=24, choices=PersonIdentityKind.choices)
    normalized_value = models.CharField(max_length=500)
    display_value = models.CharField(max_length=500, blank=True)
    source = models.CharField(
        max_length=24,
        choices=EvidenceSource.choices,
        default=EvidenceSource.MANUAL,
    )
    is_primary = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matching_person_identity"
        ordering = ("kind", "normalized_value")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "kind", "normalized_value"],
                name="unique_matching_identity_per_org",
            )
        ]
        indexes = [models.Index(fields=["org", "person", "kind"])]

    def clean(self):
        super().clean()
        if self.person_id and self.org_id and self.person.org_id != self.org_id:
            raise ValidationError("Identity must belong to the person's org")

    def __str__(self):
        return f"{self.kind}:{self.normalized_value}"


class Evidence(BaseOrgModel):
    """Source-attributed facts used to explain and audit a match."""

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="evidence",
    )
    kind = models.CharField(max_length=24, choices=EvidenceKind.choices)
    source = models.CharField(max_length=24, choices=EvidenceSource.choices)
    summary = models.TextField()
    facts = models.JSONField(default=dict)
    source_uri = models.URLField(max_length=1000, blank=True)
    source_record_id = models.CharField(max_length=255, blank=True)
    observed_at = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=0.500,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    content_hash = models.CharField(max_length=64, editable=False)

    class Meta:
        db_table = "matching_evidence"
        ordering = ("-observed_at", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "person", "source", "source_record_id"],
                condition=~models.Q(source_record_id=""),
                name="unique_matching_source_record",
            )
        ]
        indexes = [
            models.Index(fields=["org", "person", "kind", "-observed_at"]),
            models.Index(fields=["org", "source", "source_record_id"]),
        ]

    def clean(self):
        super().clean()
        if self.person_id and self.org_id and self.person.org_id != self.org_id:
            raise ValidationError("Evidence must belong to the person's org")
        if (
            self.valid_until
            and self.observed_at
            and self.valid_until < self.observed_at
        ):
            raise ValidationError("valid_until cannot precede observed_at")

    def save(self, *args, **kwargs):
        payload = json.dumps(
            {
                "facts": self.facts,
                "kind": self.kind,
                "person_id": str(self.person_id or ""),
                "source": self.source,
                "source_record_id": self.source_record_id,
                "summary": self.summary,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        self.content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.person_id}:{self.kind}:{self.source}"


class MatchOpportunity(BaseOrgModel):
    """A place or demand to which people can be matched."""

    opportunity_type = models.CharField(
        max_length=24,
        choices=MatchOpportunityType.choices,
    )
    status = models.CharField(
        max_length=16,
        choices=MatchOpportunityStatus.choices,
        default=MatchOpportunityStatus.DRAFT,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    organization_name = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=255, blank=True)
    remote_mode = models.CharField(max_length=40, blank=True)
    required_criteria = models.JSONField(default=dict)
    preferred_criteria = models.JSONField(default=dict)
    exclusion_criteria = models.JSONField(default=dict)
    scoring_weights = models.JSONField(default=default_scoring_weights)
    owner = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="owned_match_opportunities",
        null=True,
        blank=True,
    )
    opened_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matching_opportunity"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["org", "status", "opportunity_type", "-created_at"]),
            models.Index(fields=["org", "owner", "status"]),
        ]

    def clean(self):
        super().clean()
        if self.owner_id and self.org_id and self.owner.org_id != self.org_id:
            raise ValidationError("Opportunity owner must belong to the same org")
        if self.closes_at and self.opened_at and self.closes_at < self.opened_at:
            raise ValidationError("closes_at cannot precede opened_at")

    def __str__(self):
        return self.title


class Match(BaseOrgModel):
    """A versioned, explainable assessment of one person for one opportunity."""

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    opportunity = models.ForeignKey(
        MatchOpportunity,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    status = models.CharField(
        max_length=24,
        choices=MatchStatus.choices,
        default=MatchStatus.PROPOSED,
    )
    overall_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
    )
    eligibility_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
    )
    fit_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
    )
    trust_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
    )
    relationship_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
    )
    availability_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
    )
    confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    rank = models.PositiveIntegerField(null=True, blank=True)
    reasons = models.JSONField(default=list)
    gaps = models.JSONField(default=list)
    score_breakdown = models.JSONField(default=dict)
    engine_version = models.CharField(max_length=64, default="rules-v1")
    model_provider = models.CharField(max_length=40, blank=True)
    model_name = models.CharField(max_length=120, blank=True)
    evaluated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "matching_match"
        ordering = ("rank", "-overall_score", "person__display_name")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "person", "opportunity"],
                name="unique_person_opportunity_match",
            )
        ]
        indexes = [
            models.Index(fields=["org", "opportunity", "-overall_score"]),
            models.Index(fields=["org", "person", "status"]),
        ]

    def clean(self):
        super().clean()
        if not (self.org_id and self.person_id and self.opportunity_id):
            return
        if self.person.org_id != self.org_id:
            raise ValidationError("Match person must belong to the same org")
        if self.opportunity.org_id != self.org_id:
            raise ValidationError("Match opportunity must belong to the same org")

    def __str__(self):
        return f"{self.person_id}->{self.opportunity_id}:{self.overall_score}"


class MatchEvidence(BaseOrgModel):
    """Evidence citation and contribution attached to a generated match."""

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="evidence_links",
    )
    evidence = models.ForeignKey(
        Evidence,
        on_delete=models.PROTECT,
        related_name="match_links",
    )
    direction = models.CharField(
        max_length=16,
        choices=MatchEvidenceDirection.choices,
        default=MatchEvidenceDirection.POSITIVE,
    )
    relevance = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    contribution = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(-100), MaxValueValidator(100)],
    )
    explanation = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = "matching_match_evidence"
        ordering = ("-relevance", "-contribution")
        constraints = [
            models.UniqueConstraint(
                fields=["match", "evidence"],
                name="unique_evidence_per_match",
            )
        ]
        indexes = [models.Index(fields=["org", "match", "-relevance"])]

    def clean(self):
        super().clean()
        if not (self.org_id and self.match_id and self.evidence_id):
            return
        if self.match.org_id != self.org_id or self.evidence.org_id != self.org_id:
            raise ValidationError("Match evidence must belong to the same org")
        if self.match.person_id != self.evidence.person_id:
            raise ValidationError("Evidence must describe the matched person")

    def __str__(self):
        return f"{self.match_id}:{self.evidence_id}"
