"""Tenant-owned people, evidence, opportunities, and explainable matches."""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
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


class EvidenceCollectionMethod(models.TextChoices):
    INBOUND_FORM = "inbound_form", "Inbound form"
    DIRECT_MESSAGE = "direct_message", "Direct message"
    INBOUND_EMAIL = "inbound_email", "Inbound email"
    PROVIDER_API = "provider_api", "Provider API"
    CSV_IMPORT = "csv_import", "CSV import"
    MANUAL = "manual", "Manual entry"
    AI_EXTRACTION = "ai_extraction", "AI extraction"
    OTHER = "other", "Other"


class EvidenceLawfulBasis(models.TextChoices):
    UNASSESSED = "unassessed", "Not assessed"
    CONSENT = "consent", "Consent"
    LEGITIMATE_INTEREST = "legitimate_interest", "Legitimate interest"
    CONTRACT = "contract", "Contract / pre-contract request"
    LEGAL_OBLIGATION = "legal_obligation", "Legal obligation"
    PUBLIC_TASK = "public_task", "Public task"
    VITAL_INTEREST = "vital_interest", "Vital interest"


class EvidenceProcessingStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RESTRICTED = "restricted", "Restricted"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    ANONYMIZED = "anonymized", "Anonymized"


class EvidenceConfirmationStatus(models.TextChoices):
    PENDING = "pending", "Pending confirmation"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"


class EvidenceGovernanceAction(models.TextChoices):
    CREATED = "created", "Created"
    PROVENANCE_UPDATED = "provenance_updated", "Provenance updated"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    RESTRICTED = "restricted", "Restricted"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    ANONYMIZED = "anonymized", "Anonymized"


class PersonGovernanceStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    ANONYMIZED = "anonymized", "Anonymized"


class PersonGovernanceEventType(models.TextChoices):
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    DELETION_CANCELLED = "deletion_cancelled", "Deletion cancelled"
    ANONYMIZED = "anonymized", "Anonymized"
    EXPORT_REQUESTED = "export_requested", "Export requested"
    EXPORT_DELIVERED = "export_delivered", "Export delivered"
    EXPORT_INVALIDATED = "export_invalidated", "Export invalidated"


class GovernanceContactChannel(models.TextChoices):
    EMAIL = "email", "Email"
    WHATSAPP = "whatsapp", "WhatsApp"
    LINKEDIN = "linkedin", "LinkedIn"
    PHONE = "phone", "Phone"
    WECHAT = "wechat", "WeChat"
    OTHER = "other", "Other"


class PersonContactIntentState(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    OPEN = "open", "Open"
    CONDITIONAL = "conditional", "Conditional"
    NOT_OPEN = "not_open", "Not open"
    WITHDRAWN = "withdrawn", "Withdrawn"
    OBJECTED = "objected", "Objected"


class PersonContactIntentPurpose(models.TextChoices):
    GENERAL_CONTACT = "general_contact", "General contact"
    CUSTOMER = "customer", "Customer"
    EMPLOYMENT = "employment", "Employment"
    CONTRACTOR = "contractor", "Contractor"
    PROJECT = "project", "Project"
    EXPERT = "expert", "Expert"
    REFERRAL = "referral", "Referral"
    PARTNERSHIP = "partnership", "Partnership"


def default_governance_channels():
    return ["email", "whatsapp", "linkedin", "phone", "wechat"]


def default_governance_purposes():
    return [value for value, _label in PersonContactIntentPurpose.choices]


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Append-only matching history cannot be updated")

    def delete(self):
        raise ValidationError("Append-only matching history cannot be deleted")


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyHistoryMixin:
    """Application-layer guard; PostgreSQL migrations add the database guard."""

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Append-only matching history cannot be updated")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Append-only matching history cannot be deleted")


class PersonImportBatchStatus(models.TextChoices):
    PREVIEWED = "previewed", "Previewed"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    PARTIAL = "partial", "Completed with issues"
    FAILED = "failed", "Failed"


class PersonImportRecordStatus(models.TextChoices):
    READY = "ready", "Ready"
    INVALID = "invalid", "Invalid"
    CREATED = "created", "Created"
    MERGED = "merged", "Merged"
    CONFLICT = "conflict", "Conflict"
    SKIPPED = "skipped", "Skipped"
    REPLAYED = "replayed", "Replayed"
    FAILED = "failed", "Failed"


class PersonImportConflictStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class PersonImportDecisionAction(models.TextChoices):
    LINK_EXISTING = "link_existing", "Link existing person"
    SKIP = "skip", "Skip record"


class PersonImportImpactType(models.TextChoices):
    CREATED = "created", "Created"
    MERGED = "merged", "Merged"


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


class MatchProjectionState(models.TextChoices):
    CURRENT = "current", "Current"
    RETIRED = "retired", "Retired"


class MatchEvidenceDirection(models.TextChoices):
    POSITIVE = "positive", "Positive"
    NEGATIVE = "negative", "Negative"
    NEUTRAL = "neutral", "Neutral"


class MatchRunOutcome(models.TextChoices):
    SUCCEEDED = "succeeded", "Succeeded"
    SKIPPED = "skipped", "Skipped"


class MatchRevisionKind(models.TextChoices):
    EVALUATION = "evaluation", "Evaluation"
    RERANK = "rerank", "Rerank"
    RETIREMENT = "retirement", "Retirement"


class MatchRecommendationVerdict(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    ACCURATE = "accurate", "Accurate"
    PARTIALLY_ACCURATE = "partially_accurate", "Partially accurate"
    INACCURATE = "inaccurate", "Inaccurate"
    UNCERTAIN = "uncertain", "Uncertain"


class MatchFeedbackEventKind(models.TextChoices):
    RECOMMENDATION = "recommendation_feedback", "Recommendation feedback"
    OUTCOME = "lifecycle_outcome", "Lifecycle outcome"


class MatchFeedbackAction(models.TextChoices):
    RECORD = "record", "Record"
    CORRECT = "correct", "Correct"
    RETRACT = "retract", "Retract"


class MatchFeedbackSource(models.TextChoices):
    MANUAL = "manual", "Manual"
    CRM = "crm", "CRM"
    EMAIL = "email", "Email"
    WHATSAPP = "whatsapp", "WhatsApp"
    LINKEDIN = "linkedin", "LinkedIn"
    ATS = "ats", "ATS"
    SYSTEM = "system", "System"
    IMPORT = "import", "Import"
    OTHER = "other", "Other"


class MatchOutcomeCode(models.TextChoices):
    CONTACT_ATTEMPTED = "contact_attempted", "Contact attempted"
    CONTACT_REACHED = "contact_reached", "Contact reached"
    INTERVIEW_SCHEDULED = "interview_scheduled", "Interview scheduled"
    INTERVIEW_COMPLETED = "interview_completed", "Interview completed"
    DEAL_WON = "deal_won", "Deal won"
    DEAL_LOST = "deal_lost", "Deal lost"
    HIRED = "hired", "Hired"
    NOT_HIRED = "not_hired", "Not hired"
    COLLABORATION_STARTED = "collaboration_started", "Collaboration started"
    COLLABORATION_COMPLETED = "collaboration_completed", "Collaboration completed"
    REFERRAL_MADE = "referral_made", "Referral made"
    REFERRAL_ACCEPTED = "referral_accepted", "Referral accepted"
    NOT_PURSUED = "not_pursued", "Not pursued"
    WITHDREW = "withdrew", "Withdrew"


class MatchFeedbackDimension(models.TextChoices):
    SKILLS = "skills", "Skills"
    TITLES = "titles", "Titles"
    LOCATIONS = "locations", "Locations"
    AVAILABILITY = "availability", "Availability"
    TRUST = "trust", "Trust"
    RELATIONSHIP = "relationship", "Relationship"


class MatchFeedbackAssessment(models.TextChoices):
    HELPFUL = "helpful", "Helpful"
    NEUTRAL = "neutral", "Neutral"
    MISLEADING = "misleading", "Misleading"
    OUTDATED = "outdated", "Outdated"
    INSUFFICIENT = "insufficient", "Insufficient"


class MatchScoringPolicyVersionSource(models.TextChoices):
    HUMAN = "human", "Human"
    AI_SUGGESTION = "ai_suggestion", "AI suggestion"
    LEGACY = "legacy", "Legacy"


class MatchScoringPolicyAction(models.TextChoices):
    DRAFT_CREATED = "draft_created", "Draft created"
    PUBLISHED = "published", "Published"
    REJECTED = "rejected", "Rejected"


class MatchWeightSuggestionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class MatchWeightSuggestionReviewAction(models.TextChoices):
    ACCEPT = "accept", "Accept"
    REJECT = "reject", "Reject"


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
    governance_status = models.CharField(
        max_length=24,
        choices=PersonGovernanceStatus.choices,
        default=PersonGovernanceStatus.ACTIVE,
    )
    governance_revision = models.PositiveBigIntegerField(default=0)
    retention_until = models.DateTimeField(null=True, blank=True)
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    anonymized_at = models.DateTimeField(null=True, blank=True)
    onboarding_idempotency_key = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
    )
    onboarding_request_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
    )
    onboarding_identity_ids = models.JSONField(default=list, editable=False)
    onboarding_evidence_ids = models.JSONField(default=list, editable=False)

    class Meta:
        db_table = "matching_person"
        ordering = ("display_name", "id")
        indexes = [
            models.Index(fields=["org", "status", "display_name"]),
            models.Index(fields=["org", "availability", "-updated_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["org", "onboarding_idempotency_key"],
                condition=models.Q(onboarding_idempotency_key__isnull=False),
                name="unique_matching_onboarding_key_per_org",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        governance_status=PersonGovernanceStatus.DELETION_REQUESTED
                    )
                    | models.Q(deletion_requested_at__isnull=False)
                ),
                name="matching_person_deletion_time",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(governance_status=PersonGovernanceStatus.ANONYMIZED)
                    | models.Q(anonymized_at__isnull=False)
                ),
                name="matching_person_anonymized_time",
            ),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        """Retire live match projections before persisting an ineligible state.

        PostgreSQL also enforces the invariant below the ORM. Keeping the
        retirement and Person update in one transaction makes normal model
        saves safe on SQLite as well, while a governance cancellation remains
        deliberately non-restorative.
        """

        if self._state.adding:
            return super().save(*args, **kwargs)

        update_fields = kwargs.get("update_fields")
        if update_fields is not None and not {
            "status",
            "governance_status",
        }.intersection(update_fields):
            return super().save(*args, **kwargs)

        from matching.locking import lock_matching_org
        from matching.services import retire_person_match_projections

        with transaction.atomic():
            lock_matching_org(self.org_id)
            persisted = (
                type(self).objects.select_for_update()
                .filter(org_id=self.org_id, id=self.id)
                .first()
            )
            effective_status = (
                self.status
                if update_fields is None or "status" in update_fields
                else getattr(persisted, "status", self.status)
            )
            effective_governance_status = (
                self.governance_status
                if update_fields is None or "governance_status" in update_fields
                else getattr(persisted, "governance_status", self.governance_status)
            )
            was_matchable = bool(
                persisted is not None
                and persisted.status == PersonStatus.ACTIVE
                and persisted.governance_status == PersonGovernanceStatus.ACTIVE
            )
            will_be_matchable = bool(
                effective_status == PersonStatus.ACTIVE
                and effective_governance_status == PersonGovernanceStatus.ACTIVE
            )
            if persisted is not None and was_matchable and not will_be_matchable:
                retirement_reason = (
                    f"governance_{effective_governance_status}"
                    if effective_governance_status != PersonGovernanceStatus.ACTIVE
                    else f"person_status_{effective_status}"
                )[:64]
                retire_person_match_projections(
                    person=persisted,
                    reason=retirement_reason,
                )
            return super().save(*args, **kwargs)


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
    source_namespace = models.CharField(max_length=128, blank=True, default="")
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
                fields=["org", "source", "source_namespace", "source_record_id"],
                condition=~models.Q(source_record_id=""),
                name="unique_matching_source_namespace_record",
            )
        ]
        indexes = [
            models.Index(fields=["org", "person", "kind", "-observed_at"]),
            models.Index(
                fields=["org", "source", "source_namespace", "source_record_id"]
            ),
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

    def compute_content_hash(self):
        payload = json.dumps(
            {
                "facts": self.facts,
                "kind": self.kind,
                "person_id": str(self.person_id or ""),
                "source": self.source,
                "source_namespace": self.source_namespace,
                "source_record_id": self.source_record_id,
                "summary": self.summary,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        self.content_hash = self.compute_content_hash()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.person_id}:{self.kind}:{self.source}"


class EvidenceProvenance(BaseOrgModel):
    """Current privacy, provenance, and confirmation projection for Evidence."""

    evidence = models.OneToOneField(
        Evidence,
        on_delete=models.CASCADE,
        related_name="provenance",
    )
    collection_method = models.CharField(
        max_length=24,
        choices=EvidenceCollectionMethod.choices,
        default=EvidenceCollectionMethod.OTHER,
    )
    lawful_basis = models.CharField(
        max_length=32,
        choices=EvidenceLawfulBasis.choices,
        default=EvidenceLawfulBasis.UNASSESSED,
    )
    lawful_basis_notes = models.TextField(blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    consent_evidence_ref = models.CharField(max_length=1000, blank=True)
    country_code = models.CharField(max_length=3, blank=True)
    allowed_channels = models.JSONField(default=default_governance_channels)
    allowed_purposes = models.JSONField(default=default_governance_purposes)
    retention_until = models.DateTimeField(null=True, blank=True)
    processing_status = models.CharField(
        max_length=24,
        choices=EvidenceProcessingStatus.choices,
        default=EvidenceProcessingStatus.ACTIVE,
    )
    confirmation_status = models.CharField(
        max_length=24,
        choices=EvidenceConfirmationStatus.choices,
        default=EvidenceConfirmationStatus.CONFIRMED,
    )
    source_content_sha256 = models.CharField(max_length=64, blank=True)
    confirmed_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="confirmed_matching_evidence",
        null=True,
        blank=True,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveBigIntegerField(default=0)
    expiry_processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matching_evidence_provenance"
        indexes = [
            models.Index(
                fields=["org", "processing_status", "confirmation_status"],
                name="matching_ev_gov_status_idx",
            ),
            models.Index(
                fields=["org", "retention_until", "expiry_processed_at"],
                name="matching_ev_gov_expiry_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(lawful_basis=EvidenceLawfulBasis.CONSENT)
                    | (
                        models.Q(consent_at__isnull=False)
                        & ~models.Q(consent_evidence_ref="")
                    )
                ),
                name="matching_ev_gov_consent_complete",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        confirmation_status=EvidenceConfirmationStatus.CONFIRMED
                    )
                    | models.Q(confirmed_at__isnull=False)
                ),
                name="matching_ev_gov_confirmation_time",
            ),
        ]

    def clean(self):
        super().clean()
        if self.evidence_id and self.org_id and self.evidence.org_id != self.org_id:
            raise ValidationError("Evidence provenance must belong to the same org")
        if (
            self.confirmed_by_id
            and self.org_id
            and self.confirmed_by.org_id != self.org_id
        ):
            raise ValidationError("Evidence confirmer must belong to the same org")


class EvidenceGovernanceEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable provenance or human-confirmation transition."""

    provenance = models.ForeignKey(
        EvidenceProvenance,
        on_delete=models.PROTECT,
        related_name="events",
    )
    evidence = models.ForeignKey(
        Evidence,
        on_delete=models.PROTECT,
        related_name="governance_events",
    )
    action = models.CharField(max_length=32, choices=EvidenceGovernanceAction.choices)
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="evidence_governance_events",
        null=True,
        blank=True,
    )
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=64, blank=True)
    safe_snapshot = models.JSONField(default=dict)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_evidence_governance_event"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_ev_gov_event_key",
            ),
            models.UniqueConstraint(
                fields=["org", "provenance", "resulting_revision"],
                name="unique_matching_ev_gov_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resulting_revision=models.F("expected_revision") + 1
                ),
                name="matching_ev_gov_revision_increments",
            ),
        ]
        indexes = [models.Index(fields=["org", "evidence", "-created_at"])]

    def clean(self):
        super().clean()
        if not (self.org_id and self.provenance_id and self.evidence_id):
            return
        if (
            self.provenance.org_id != self.org_id
            or self.evidence.org_id != self.org_id
            or self.provenance.evidence_id != self.evidence_id
        ):
            raise ValidationError("Evidence governance event relationships are invalid")
        if self.actor_id and self.actor.org_id != self.org_id:
            raise ValidationError("Evidence governance actor must belong to the same org")


class PersonContactIntent(BaseOrgModel):
    """Current confirmed contact posture for one person, purpose, and channel."""

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="contact_intents",
    )
    identity = models.ForeignKey(
        PersonIdentity,
        on_delete=models.SET_NULL,
        related_name="contact_intents",
        null=True,
        blank=True,
    )
    opportunity = models.ForeignKey(
        "matching.MatchOpportunity",
        on_delete=models.SET_NULL,
        related_name="contact_intents",
        null=True,
        blank=True,
    )
    evidence = models.ForeignKey(
        Evidence,
        on_delete=models.SET_NULL,
        related_name="contact_intents",
        null=True,
        blank=True,
    )
    channel = models.CharField(max_length=16, choices=GovernanceContactChannel.choices)
    purpose = models.CharField(
        max_length=24,
        choices=PersonContactIntentPurpose.choices,
        default=PersonContactIntentPurpose.GENERAL_CONTACT,
    )
    state = models.CharField(
        max_length=16,
        choices=PersonContactIntentState.choices,
        default=PersonContactIntentState.UNKNOWN,
    )
    source = models.CharField(
        max_length=24,
        choices=EvidenceSource.choices,
        default=EvidenceSource.MANUAL,
    )
    confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=0.500,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    observed_at = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "matching_person_contact_intent"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "person", "channel", "purpose"],
                name="unique_matching_contact_intent",
            ),
            models.CheckConstraint(
                condition=~models.Q(source=EvidenceSource.AI),
                name="matching_intent_projection_not_ai",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(valid_until__isnull=True)
                    | models.Q(valid_until__gte=models.F("observed_at"))
                ),
                name="matching_intent_validity_order",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "person", "channel", "purpose"]),
            models.Index(fields=["org", "state", "valid_until"]),
        ]

    def clean(self):
        super().clean()
        if self.person_id and self.org_id and self.person.org_id != self.org_id:
            raise ValidationError("Contact intent person must belong to the same org")
        for child in (self.identity, self.opportunity, self.evidence):
            if child is not None and child.org_id != self.org_id:
                raise ValidationError("Contact intent relationships must share an org")
        if self.identity_id and self.identity.person_id != self.person_id:
            raise ValidationError("Contact intent identity must belong to its person")
        if self.evidence_id and self.evidence.person_id != self.person_id:
            raise ValidationError("Contact intent evidence must describe its person")


class PersonContactIntentEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable proposed or effective contact-intent observation."""

    intent = models.ForeignKey(
        PersonContactIntent,
        on_delete=models.PROTECT,
        related_name="events",
    )
    evidence = models.ForeignKey(
        Evidence,
        on_delete=models.PROTECT,
        related_name="contact_intent_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="contact_intent_events",
        null=True,
        blank=True,
    )
    from_state = models.CharField(max_length=16, choices=PersonContactIntentState.choices)
    to_state = models.CharField(max_length=16, choices=PersonContactIntentState.choices)
    source = models.CharField(max_length=24, choices=EvidenceSource.choices)
    confirmation_status = models.CharField(
        max_length=24,
        choices=EvidenceConfirmationStatus.choices,
    )
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=64, blank=True)
    safe_snapshot = models.JSONField(default=dict)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_person_contact_intent_event"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_intent_event_key",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            source=EvidenceSource.AI,
                            confirmation_status=EvidenceConfirmationStatus.PENDING,
                            resulting_revision=models.F("expected_revision"),
                        )
                    )
                    | (
                        ~models.Q(source=EvidenceSource.AI)
                        & models.Q(
                            confirmation_status=EvidenceConfirmationStatus.CONFIRMED,
                            resulting_revision=models.F("expected_revision") + 1,
                        )
                    )
                ),
                name="matching_intent_event_confirmation",
            ),
        ]
        indexes = [models.Index(fields=["org", "intent", "-created_at"])]

    def clean(self):
        super().clean()
        if self.intent_id and self.org_id and self.intent.org_id != self.org_id:
            raise ValidationError("Contact intent event must belong to the same org")
        if self.evidence_id and self.evidence.org_id != self.org_id:
            raise ValidationError("Contact intent event evidence belongs to another org")
        if self.actor_id and self.actor.org_id != self.org_id:
            raise ValidationError("Contact intent event actor belongs to another org")


class PersonGovernanceEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable person deletion, anonymization, and export audit fact."""

    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="governance_events",
    )
    event_type = models.CharField(
        max_length=32,
        choices=PersonGovernanceEventType.choices,
    )
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="person_governance_events",
        null=True,
        blank=True,
    )
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()
    safe_snapshot = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(default=timezone.now)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_person_governance_event"
        ordering = ("-occurred_at", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_person_gov_event_key",
            ),
            models.UniqueConstraint(
                fields=["org", "person", "resulting_revision", "event_type"],
                name="unique_matching_person_gov_event_revision",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        event_type__in=[
                            PersonGovernanceEventType.EXPORT_REQUESTED,
                            PersonGovernanceEventType.EXPORT_DELIVERED,
                            PersonGovernanceEventType.EXPORT_INVALIDATED,
                        ],
                        resulting_revision=models.F("expected_revision"),
                    )
                    | (
                        models.Q(
                            event_type__in=[
                                PersonGovernanceEventType.DELETION_REQUESTED,
                                PersonGovernanceEventType.DELETION_CANCELLED,
                                PersonGovernanceEventType.ANONYMIZED,
                            ]
                        )
                        & models.Q(
                            resulting_revision=models.F("expected_revision") + 1
                        )
                    )
                ),
                name="matching_person_gov_event_revision",
            ),
        ]
        indexes = [models.Index(fields=["org", "person", "-occurred_at"])]

    def clean(self):
        super().clean()
        if self.person_id and self.org_id and self.person.org_id != self.org_id:
            raise ValidationError("Person governance event must belong to the same org")
        if self.actor_id and self.actor.org_id != self.org_id:
            raise ValidationError("Person governance actor belongs to another org")


class PersonImportBatch(BaseOrgModel):
    """Durable preview and execution ledger for one bounded person CSV import."""

    requested_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="requested_person_imports",
        null=True,
        blank=True,
    )
    automation_job = models.OneToOneField(
        "automation.AutomationJob",
        on_delete=models.PROTECT,
        related_name="person_import_batch",
        null=True,
        blank=True,
    )
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    commit_idempotency_key = models.UUIDField(null=True, blank=True)
    commit_request_hash = models.CharField(max_length=64, blank=True)
    content_hash = models.CharField(max_length=64)
    schema_version = models.PositiveSmallIntegerField(default=1)
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    mapping = models.JSONField(default=dict)
    headers = models.JSONField(default=list)
    source = models.CharField(
        max_length=24,
        choices=EvidenceSource.choices,
        default=EvidenceSource.MANUAL,
    )
    source_namespace = models.CharField(max_length=128, default="manual:csv")
    status = models.CharField(
        max_length=24,
        choices=PersonImportBatchStatus.choices,
        default=PersonImportBatchStatus.PREVIEWED,
    )
    revision = models.PositiveBigIntegerField(default=0)
    total_count = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    ready_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    merged_count = models.PositiveIntegerField(default=0)
    conflict_count = models.PositiveIntegerField(default=0)
    invalid_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    replayed_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    match_run_ids = models.JSONField(default=list)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matching_person_import_batch"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_person_import_key",
            )
        ]
        indexes = [
            models.Index(fields=["org", "status", "-created_at"]),
            models.Index(fields=["org", "content_hash"]),
        ]

    def clean(self):
        super().clean()
        if self.requested_by_id and self.org_id:
            if self.requested_by.org_id != self.org_id:
                raise ValidationError("Import requester must belong to the same org")
        if self.automation_job_id and self.org_id:
            if self.automation_job.org_id != self.org_id:
                raise ValidationError("Import job must belong to the same org")


class PersonImportRecord(BaseOrgModel):
    """One normalized, bounded CSV row and its mutable processing projection."""

    batch = models.ForeignKey(
        PersonImportBatch,
        on_delete=models.CASCADE,
        related_name="records",
    )
    row_number = models.PositiveIntegerField()
    row_hash = models.CharField(max_length=64)
    source_record_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    normalized_payload = models.JSONField(default=dict)
    masked_identities = models.JSONField(default=list)
    field_errors = models.JSONField(default=list)
    status = models.CharField(
        max_length=24,
        choices=PersonImportRecordStatus.choices,
        default=PersonImportRecordStatus.READY,
    )
    revision = models.PositiveBigIntegerField(default=0)
    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="import_records",
        null=True,
        blank=True,
    )
    error_code = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = "matching_person_import_record"
        ordering = ("row_number", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"],
                name="unique_matching_import_row_number",
            )
        ]
        indexes = [
            models.Index(fields=["org", "batch", "status", "row_number"]),
            models.Index(fields=["org", "row_hash"]),
        ]

    def clean(self):
        super().clean()
        if self.batch_id and self.org_id and self.batch.org_id != self.org_id:
            raise ValidationError("Import record must belong to the batch org")
        if self.person_id and self.org_id and self.person.org_id != self.org_id:
            raise ValidationError("Import record person must belong to the same org")


class PersonImportConflict(BaseOrgModel):
    """A safe, resolvable projection of an ambiguous import record."""

    batch = models.ForeignKey(
        PersonImportBatch,
        on_delete=models.CASCADE,
        related_name="conflicts",
    )
    record = models.OneToOneField(
        PersonImportRecord,
        on_delete=models.CASCADE,
        related_name="conflict",
    )
    code = models.CharField(max_length=80)
    person_ids = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=PersonImportConflictStatus.choices,
        default=PersonImportConflictStatus.OPEN,
    )
    revision = models.PositiveBigIntegerField(default=0)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matching_person_import_conflict"
        ordering = ("record__row_number",)
        indexes = [models.Index(fields=["org", "batch", "status"])]

    def clean(self):
        super().clean()
        if not (self.org_id and self.batch_id and self.record_id):
            return
        if self.batch.org_id != self.org_id or self.record.org_id != self.org_id:
            raise ValidationError("Import conflict must belong to the same org")
        if self.record.batch_id != self.batch_id:
            raise ValidationError("Import conflict record must belong to its batch")


class PersonImportImpact(BaseOrgModel):
    """A changed person projection caused by a successfully processed row."""

    batch = models.ForeignKey(
        PersonImportBatch,
        on_delete=models.CASCADE,
        related_name="impacts",
    )
    record = models.ForeignKey(
        PersonImportRecord,
        on_delete=models.CASCADE,
        related_name="impacts",
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="import_impacts",
    )
    impact_type = models.CharField(
        max_length=16,
        choices=PersonImportImpactType.choices,
    )
    changed_fields = models.JSONField(default=list)

    class Meta:
        db_table = "matching_person_import_impact"
        ordering = ("record__row_number",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "batch", "person"],
                name="unique_matching_import_person_impact",
            )
        ]
        indexes = [models.Index(fields=["org", "batch", "person"])]

    def clean(self):
        super().clean()
        if not (self.org_id and self.batch_id and self.record_id and self.person_id):
            return
        if (
            self.batch.org_id != self.org_id
            or self.record.org_id != self.org_id
            or self.person.org_id != self.org_id
        ):
            raise ValidationError("Import impact must belong to the same org")
        if self.record.batch_id != self.batch_id:
            raise ValidationError("Import impact record must belong to its batch")


class PersonIdentityObservation(BaseOrgModel):
    """Source-specific observation of an identity attached to an imported person."""

    batch = models.ForeignKey(
        PersonImportBatch,
        on_delete=models.CASCADE,
        related_name="identity_observations",
    )
    record = models.ForeignKey(
        PersonImportRecord,
        on_delete=models.CASCADE,
        related_name="identity_observations",
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="identity_observations",
    )
    identity = models.ForeignKey(
        PersonIdentity,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    kind = models.CharField(max_length=24, choices=PersonIdentityKind.choices)
    normalized_value_hash = models.CharField(max_length=64)
    source = models.CharField(max_length=24, choices=EvidenceSource.choices)
    source_namespace = models.CharField(max_length=128)
    source_record_id = models.CharField(max_length=255)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "matching_person_identity_observation"
        ordering = ("record__row_number", "kind")
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "record", "kind", "normalized_value_hash"],
                name="unique_matching_identity_observation",
            ),
            models.UniqueConstraint(
                fields=[
                    "org",
                    "identity",
                    "source",
                    "source_namespace",
                    "source_record_id",
                ],
                name="unique_matching_cross_source_observation",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "person", "kind", "-observed_at"]),
            models.Index(fields=["org", "source", "source_namespace"]),
        ]

    def clean(self):
        super().clean()
        if not (
            self.org_id
            and self.batch_id
            and self.record_id
            and self.person_id
            and self.identity_id
        ):
            return
        if any(
            item.org_id != self.org_id
            for item in (self.batch, self.record, self.person, self.identity)
        ):
            raise ValidationError("Identity observation must belong to the same org")
        if self.record.batch_id != self.batch_id or self.identity.person_id != self.person_id:
            raise ValidationError("Identity observation relationships are inconsistent")


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
    scoring_policy_version = models.ForeignKey(
        "MatchScoringPolicyVersion",
        on_delete=models.PROTECT,
        related_name="opportunities",
        null=True,
        blank=True,
    )
    ranking_revision = models.PositiveBigIntegerField(default=0)
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
        if self.scoring_policy_version_id:
            version = self.scoring_policy_version
            if version.org_id != self.org_id:
                raise ValidationError("Opportunity scoring policy has another org")
            if version.policy.opportunity_type != self.opportunity_type:
                raise ValidationError("Opportunity scoring policy type does not match")

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
    projection_state = models.CharField(
        max_length=16,
        choices=MatchProjectionState.choices,
        default=MatchProjectionState.CURRENT,
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    retirement_reason = models.CharField(max_length=64, blank=True, default="")
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
    ranking_revision = models.PositiveBigIntegerField(default=0)
    decision_revision = models.PositiveBigIntegerField(default=0)
    feedback_revision = models.PositiveBigIntegerField(default=0)
    recommendation_verdict = models.CharField(
        max_length=24,
        choices=MatchRecommendationVerdict.choices,
        default=MatchRecommendationVerdict.UNKNOWN,
    )
    latest_outcome_code = models.CharField(
        max_length=40,
        choices=MatchOutcomeCode.choices,
        blank=True,
        default="",
    )
    latest_outcome_at = models.DateTimeField(null=True, blank=True)
    scoring_policy_version = models.ForeignKey(
        "MatchScoringPolicyVersion",
        on_delete=models.PROTECT,
        related_name="matches",
        null=True,
        blank=True,
    )
    scoring_policy_checksum = models.CharField(max_length=64, blank=True)
    decision_reason = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="decided_matches",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "matching_match"
        ordering = ("rank", "-overall_score", "person__display_name")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "person", "opportunity"],
                name="unique_person_opportunity_match",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        projection_state=MatchProjectionState.CURRENT,
                        retired_at__isnull=True,
                        retirement_reason="",
                    )
                    | (
                        models.Q(
                            projection_state=MatchProjectionState.RETIRED,
                            retired_at__isnull=False,
                            rank__isnull=True,
                        )
                        & ~models.Q(retirement_reason="")
                    )
                ),
                name="matching_match_projection_lifecycle",
            ),
            models.UniqueConstraint(
                fields=["org", "opportunity", "rank"],
                condition=(
                    models.Q(projection_state=MatchProjectionState.CURRENT)
                    & models.Q(rank__isnull=False)
                ),
                name="unique_current_match_rank_per_opportunity",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "opportunity", "-overall_score"]),
            models.Index(fields=["org", "person", "status"]),
            models.Index(
                fields=["org", "opportunity", "projection_state", "rank"],
                name="matching_current_rank_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if not (self.org_id and self.person_id and self.opportunity_id):
            return
        if self.person.org_id != self.org_id:
            raise ValidationError("Match person must belong to the same org")
        if self.opportunity.org_id != self.org_id:
            raise ValidationError("Match opportunity must belong to the same org")
        if self.projection_state == MatchProjectionState.CURRENT:
            if self.retired_at is not None or self.retirement_reason:
                raise ValidationError("Current match projection cannot be retired")
            if (
                self.person.status != PersonStatus.ACTIVE
                or self.person.governance_status != PersonGovernanceStatus.ACTIVE
            ):
                raise ValidationError("Current match projection requires an active person")
        elif self.retired_at is None or not self.retirement_reason or self.rank is not None:
            raise ValidationError("Retired match projection requires retirement metadata")
        if self.scoring_policy_version_id:
            if self.scoring_policy_version.org_id != self.org_id:
                raise ValidationError("Match scoring policy has another org")
            if (
                self.scoring_policy_version.policy.opportunity_type
                != self.opportunity.opportunity_type
            ):
                raise ValidationError("Match scoring policy type does not match")

    def __str__(self):
        return f"{self.person_id}->{self.opportunity_id}:{self.overall_score}"


class MatchRun(BaseOrgModel):
    """One tenant-owned synchronous or asynchronous ranking execution."""

    opportunity = models.ForeignKey(
        MatchOpportunity,
        on_delete=models.PROTECT,
        related_name="match_runs",
    )
    automation_job = models.OneToOneField(
        "automation.AutomationJob",
        on_delete=models.PROTECT,
        related_name="match_run",
        null=True,
        blank=True,
    )
    requested_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.SET_NULL,
        related_name="requested_match_runs",
        null=True,
        blank=True,
    )
    request_hash = models.CharField(max_length=64)
    requested_person_ids = models.JSONField(default=list)
    total_count = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    result_count = models.PositiveIntegerField(null=True, blank=True)
    ranking_revision = models.PositiveBigIntegerField(null=True, blank=True)
    engine_version = models.CharField(max_length=64, default="rules-v1")
    scoring_policy_version = models.ForeignKey(
        "MatchScoringPolicyVersion",
        on_delete=models.PROTECT,
        related_name="match_runs",
        null=True,
        blank=True,
    )
    scoring_policy_checksum = models.CharField(max_length=64, blank=True)
    dimension_weights = models.JSONField(default=dict)
    component_weights = models.JSONField(default=dict)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(
        max_length=16,
        choices=MatchRunOutcome.choices,
        blank=True,
        default="",
    )

    class Meta:
        db_table = "matching_match_run"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["org", "opportunity", "-created_at"]),
            models.Index(fields=["org", "outcome", "-created_at"]),
            models.Index(fields=["org", "request_hash"]),
        ]

    def clean(self):
        super().clean()
        if self.opportunity_id and self.org_id:
            if self.opportunity.org_id != self.org_id:
                raise ValidationError("Match run opportunity must belong to the same org")
        if self.automation_job_id and self.org_id:
            if self.automation_job.org_id != self.org_id:
                raise ValidationError("Match run job must belong to the same org")
        if self.requested_by_id and self.org_id:
            if self.requested_by.org_id != self.org_id:
                raise ValidationError("Match run requester must belong to the same org")
        if self.processed_count > self.total_count:
            raise ValidationError("processed_count cannot exceed total_count")
        if self.completed_at and self.started_at and self.completed_at < self.started_at:
            raise ValidationError("completed_at cannot precede started_at")
        if self.scoring_policy_version_id:
            if self.scoring_policy_version.org_id != self.org_id:
                raise ValidationError("Match run scoring policy has another org")
            if (
                self.scoring_policy_version.policy.opportunity_type
                != self.opportunity.opportunity_type
            ):
                raise ValidationError("Match run scoring policy type does not match")

    def __str__(self):
        return f"{self.opportunity_id}:{self.request_hash}"


class MatchRevision(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable machine-generated snapshot for one match and ranking run."""

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    run = models.ForeignKey(
        MatchRun,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision = models.PositiveBigIntegerField()
    revision_kind = models.CharField(
        max_length=16,
        choices=MatchRevisionKind.choices,
        default=MatchRevisionKind.EVALUATION,
    )
    snapshot = models.JSONField(default=dict)
    evidence_snapshot = models.JSONField(default=list)
    engine_version = models.CharField(max_length=64, default="rules-v1")
    scoring_policy_version = models.ForeignKey(
        "MatchScoringPolicyVersion",
        on_delete=models.PROTECT,
        related_name="match_revisions",
        null=True,
        blank=True,
    )
    scoring_policy_checksum = models.CharField(max_length=64, blank=True)
    dimension_weights = models.JSONField(default=dict)
    component_weights = models.JSONField(default=dict)
    evaluated_at = models.DateTimeField(default=timezone.now)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_match_revision"
        ordering = ("-revision",)
        constraints = [
            models.UniqueConstraint(
                fields=["match", "run"],
                name="unique_matching_revision_per_run",
            ),
            models.UniqueConstraint(
                fields=["match", "revision"],
                name="unique_matching_revision_number",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "run", "revision"]),
            models.Index(fields=["org", "match", "-revision"]),
        ]

    def clean(self):
        super().clean()
        if not (self.org_id and self.match_id and self.run_id):
            return
        if self.match.org_id != self.org_id or self.run.org_id != self.org_id:
            raise ValidationError("Match revision must belong to the same org")
        if self.match.opportunity_id != self.run.opportunity_id:
            raise ValidationError("Match revision run must target the match opportunity")
        if self.scoring_policy_version_id:
            if self.scoring_policy_version.org_id != self.org_id:
                raise ValidationError("Match revision scoring policy has another org")
            if (
                self.scoring_policy_version.policy.opportunity_type
                != self.match.opportunity.opportunity_type
            ):
                raise ValidationError("Match revision scoring policy type does not match")

    def __str__(self):
        return f"{self.match_id}:r{self.revision}"


class MatchDecisionEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable human decision transition for a current Match projection."""

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="decision_events",
    )
    from_status = models.CharField(max_length=24, choices=MatchStatus.choices)
    to_status = models.CharField(max_length=24, choices=MatchStatus.choices)
    reason_code = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    expected_decision_revision = models.PositiveBigIntegerField()
    resulting_decision_revision = models.PositiveBigIntegerField()
    based_on_ranking_revision = models.PositiveBigIntegerField(default=0)
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="match_decision_events",
        null=True,
        blank=True,
    )
    idempotency_key = models.CharField(max_length=128)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_match_decision_event"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "match", "resulting_decision_revision"],
                name="unique_matching_decision_revision",
            ),
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_decision_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resulting_decision_revision=models.F(
                        "expected_decision_revision"
                    )
                    + 1
                ),
                name="matching_decision_revision_increments",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_status=models.F("to_status")),
                name="matching_decision_changes_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason_code=""),
                name="matching_decision_reason_code_present",
            ),
        ]
        indexes = [models.Index(fields=["org", "match", "-created_at"])]

    def clean(self):
        super().clean()
        if self.match_id and self.org_id and self.match.org_id != self.org_id:
            raise ValidationError("Match decision must belong to the same org")
        if self.actor_id and self.org_id and self.actor.org_id != self.org_id:
            raise ValidationError("Match decision actor must belong to the same org")
        if not self.reason_code.strip():
            raise ValidationError("reason_code is required")
        if self.resulting_decision_revision != self.expected_decision_revision + 1:
            raise ValidationError("Decision revision must increment by one")

    def __str__(self):
        return f"{self.match_id}:{self.from_status}->{self.to_status}"


class MatchFeedbackEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable recommendation feedback or lifecycle outcome."""

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="feedback_events",
    )
    match_revision = models.ForeignKey(
        MatchRevision,
        on_delete=models.PROTECT,
        related_name="feedback_events",
        null=True,
        blank=True,
    )
    event_kind = models.CharField(max_length=32, choices=MatchFeedbackEventKind.choices)
    action = models.CharField(max_length=16, choices=MatchFeedbackAction.choices)
    verdict = models.CharField(
        max_length=24,
        choices=MatchRecommendationVerdict.choices,
        blank=True,
        default="",
    )
    outcome_code = models.CharField(
        max_length=40,
        choices=MatchOutcomeCode.choices,
        blank=True,
        default="",
    )
    reason_code = models.CharField(max_length=64)
    note = models.CharField(max_length=1000, blank=True)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(
        max_length=16,
        choices=MatchFeedbackSource.choices,
        default=MatchFeedbackSource.MANUAL,
    )
    expected_feedback_revision = models.PositiveBigIntegerField()
    resulting_feedback_revision = models.PositiveBigIntegerField()
    based_on_ranking_revision = models.PositiveBigIntegerField()
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="match_feedback_events",
        null=True,
        blank=True,
    )
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="corrections",
        null=True,
        blank=True,
    )
    safe_snapshot = models.JSONField(default=dict)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_match_feedback_event"
        ordering = ("-occurred_at", "-recorded_at")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_feedback_idempotency",
            ),
            models.UniqueConstraint(
                fields=["org", "match", "resulting_feedback_revision"],
                name="unique_matching_feedback_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resulting_feedback_revision=models.F(
                        "expected_feedback_revision"
                    )
                    + 1
                ),
                name="matching_feedback_revision_increments",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason_code=""),
                name="matching_feedback_reason_present",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "match", "-occurred_at"]),
            models.Index(fields=["org", "event_kind", "-occurred_at"]),
        ]

    def clean(self):
        super().clean()
        if self.match_id and self.org_id and self.match.org_id != self.org_id:
            raise ValidationError("Match feedback must belong to the same org")
        if self.match_revision_id:
            if (
                self.match_revision.org_id != self.org_id
                or self.match_revision.match_id != self.match_id
            ):
                raise ValidationError("Match feedback revision is inconsistent")
        if self.actor_id and self.actor.org_id != self.org_id:
            raise ValidationError("Match feedback actor must belong to the same org")
        if self.supersedes_id:
            if (
                self.supersedes.org_id != self.org_id
                or self.supersedes.match_id != self.match_id
                or self.supersedes.event_kind != self.event_kind
            ):
                raise ValidationError("Superseded feedback is inconsistent")
        if self.action == MatchFeedbackAction.RECORD and self.supersedes_id:
            raise ValidationError("record must not supersede another event")
        if self.action != MatchFeedbackAction.RECORD and not self.supersedes_id:
            raise ValidationError("correct and retract require supersedes")
        if self.action == MatchFeedbackAction.RETRACT:
            if self.verdict or self.outcome_code:
                raise ValidationError("retract must not include verdict or outcome_code")
        elif self.event_kind == MatchFeedbackEventKind.RECOMMENDATION:
            if not self.verdict or self.outcome_code:
                raise ValidationError("Recommendation feedback requires only verdict")
        elif not self.outcome_code or self.verdict:
            raise ValidationError("Lifecycle outcome requires only outcome_code")


class MatchFeedbackAttribution(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable human assessment of one scoring dimension and optional citation."""

    feedback_event = models.ForeignKey(
        MatchFeedbackEvent,
        on_delete=models.PROTECT,
        related_name="attributions",
    )
    evidence = models.ForeignKey(
        Evidence,
        on_delete=models.PROTECT,
        related_name="feedback_attributions",
        null=True,
        blank=True,
    )
    dimension = models.CharField(max_length=24, choices=MatchFeedbackDimension.choices)
    assessment = models.CharField(
        max_length=24,
        choices=MatchFeedbackAssessment.choices,
    )
    reason_code = models.CharField(max_length=64, blank=True)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_match_feedback_attribution"
        ordering = ("dimension", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["feedback_event", "evidence", "dimension"],
                name="unique_matching_feedback_attribution",
            )
        ]
        indexes = [models.Index(fields=["org", "feedback_event", "dimension"])]

    def clean(self):
        super().clean()
        if self.feedback_event_id and self.org_id:
            if self.feedback_event.org_id != self.org_id:
                raise ValidationError("Feedback attribution must belong to the same org")
        if self.evidence_id:
            if self.evidence.org_id != self.org_id:
                raise ValidationError("Feedback attribution evidence has another org")
            if self.evidence.person_id != self.feedback_event.match.person_id:
                raise ValidationError("Feedback evidence must describe the matched person")


class MatchScoringPolicy(BaseOrgModel):
    """Current scoring policy projection for one opportunity type."""

    opportunity_type = models.CharField(
        max_length=24,
        choices=MatchOpportunityType.choices,
    )
    revision = models.PositiveBigIntegerField(default=0)
    active_version = models.ForeignKey(
        "MatchScoringPolicyVersion",
        on_delete=models.PROTECT,
        related_name="active_for_policies",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "matching_scoring_policy"
        ordering = ("opportunity_type",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "opportunity_type"],
                name="unique_matching_scoring_policy_type",
            )
        ]


class MatchScoringPolicyVersion(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable scoring weights. Publication is represented by policy events."""

    policy = models.ForeignKey(
        MatchScoringPolicy,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    dimension_weights = models.JSONField(default=default_scoring_weights)
    component_weights = models.JSONField(default=dict)
    checksum = models.CharField(max_length=64)
    source = models.CharField(
        max_length=24,
        choices=MatchScoringPolicyVersionSource.choices,
        default=MatchScoringPolicyVersionSource.HUMAN,
    )
    rationale = models.CharField(max_length=1000, blank=True)
    created_by_profile = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="created_match_scoring_versions",
        null=True,
        blank=True,
    )

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_scoring_policy_version"
        ordering = ("-version",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "policy", "version"],
                name="unique_matching_policy_version",
            ),
        ]
        indexes = [models.Index(fields=["org", "policy", "-version"])]

    def clean(self):
        super().clean()
        if self.policy_id and self.policy.org_id != self.org_id:
            raise ValidationError("Scoring policy version must belong to the same org")
        if self.created_by_profile_id and self.created_by_profile.org_id != self.org_id:
            raise ValidationError("Scoring policy author must belong to the same org")


class MatchScoringPolicyEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable policy draft, publication, or rejection audit event."""

    policy = models.ForeignKey(
        MatchScoringPolicy,
        on_delete=models.PROTECT,
        related_name="events",
    )
    policy_version = models.ForeignKey(
        MatchScoringPolicyVersion,
        on_delete=models.PROTECT,
        related_name="events",
    )
    action = models.CharField(max_length=24, choices=MatchScoringPolicyAction.choices)
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="match_scoring_policy_events",
        null=True,
        blank=True,
    )
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=64, blank=True)
    safe_snapshot = models.JSONField(default=dict)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_scoring_policy_event"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_policy_event_key",
            ),
            models.UniqueConstraint(
                fields=["org", "policy", "resulting_revision"],
                name="unique_matching_policy_event_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resulting_revision=models.F("expected_revision") + 1
                ),
                name="matching_policy_event_revision_increments",
            ),
        ]


class MatchWeightSuggestion(BaseOrgModel):
    """Reviewable AI/analytics weight suggestion; never an active policy."""

    policy = models.ForeignKey(
        MatchScoringPolicy,
        on_delete=models.PROTECT,
        related_name="weight_suggestions",
    )
    opportunity_type = models.CharField(
        max_length=24,
        choices=MatchOpportunityType.choices,
    )
    dimension_weights = models.JSONField(default=default_scoring_weights)
    component_weights = models.JSONField(default=dict)
    rationale = models.CharField(max_length=1000, blank=True)
    sample_count = models.PositiveIntegerField(default=0)
    analysis_hash = models.CharField(max_length=64)
    base_policy_checksum = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=MatchWeightSuggestionStatus.choices,
        default=MatchWeightSuggestionStatus.PENDING,
    )
    revision = models.PositiveBigIntegerField(default=0)
    generator = models.CharField(max_length=120, blank=True)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    reviewed_by = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="reviewed_match_weight_suggestions",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    accepted_draft = models.ForeignKey(
        MatchScoringPolicyVersion,
        on_delete=models.PROTECT,
        related_name="accepted_suggestions",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "matching_weight_suggestion"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["org", "status", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["org", "analysis_hash"],
                name="unique_matching_weight_suggestion_analysis",
            ),
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_weight_suggestion_key",
            ),
        ]


class MatchWeightSuggestionReviewEvent(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable human review of a weight suggestion."""

    suggestion = models.ForeignKey(
        MatchWeightSuggestion,
        on_delete=models.PROTECT,
        related_name="review_events",
    )
    action = models.CharField(
        max_length=16,
        choices=MatchWeightSuggestionReviewAction.choices,
    )
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="match_weight_suggestion_reviews",
    )
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=64, blank=True)
    safe_snapshot = models.JSONField(default=dict)

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_weight_suggestion_review_event"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_suggestion_review_key",
            ),
            models.UniqueConstraint(
                fields=["org", "suggestion", "resulting_revision"],
                name="unique_matching_suggestion_review_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resulting_revision=models.F("expected_revision") + 1
                ),
                name="matching_suggestion_review_revision_increments",
            ),
        ]


class PersonImportDecision(AppendOnlyHistoryMixin, BaseOrgModel):
    """Immutable operator decision resolving one import conflict."""

    batch = models.ForeignKey(
        PersonImportBatch,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    record = models.ForeignKey(
        PersonImportRecord,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    conflict = models.ForeignKey(
        PersonImportConflict,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    action = models.CharField(
        max_length=24,
        choices=PersonImportDecisionAction.choices,
    )
    target_person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="import_decisions",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        "common.Profile",
        on_delete=models.PROTECT,
        related_name="person_import_decisions",
        null=True,
        blank=True,
    )
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()

    objects = AppendOnlyManager()

    class Meta:
        db_table = "matching_person_import_decision"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "idempotency_key"],
                name="unique_matching_import_decision_key",
            ),
            models.UniqueConstraint(
                fields=["org", "conflict", "resulting_revision"],
                name="unique_matching_import_decision_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resulting_revision=models.F("expected_revision") + 1
                ),
                name="matching_import_decision_revision_increments",
            ),
        ]
        indexes = [models.Index(fields=["org", "conflict", "-created_at"])]

    def clean(self):
        super().clean()
        if not (self.org_id and self.batch_id and self.record_id and self.conflict_id):
            return
        if any(
            item.org_id != self.org_id
            for item in (self.batch, self.record, self.conflict)
        ):
            raise ValidationError("Import decision must belong to the same org")
        if (
            self.record.batch_id != self.batch_id
            or self.conflict.batch_id != self.batch_id
            or self.conflict.record_id != self.record_id
        ):
            raise ValidationError("Import decision relationships are inconsistent")
        if self.target_person_id and self.target_person.org_id != self.org_id:
            raise ValidationError("Import decision target must belong to the same org")
        if self.actor_id and self.actor.org_id != self.org_id:
            raise ValidationError("Import decision actor must belong to the same org")
        if self.action == PersonImportDecisionAction.LINK_EXISTING:
            if self.target_person_id is None:
                raise ValidationError("link_existing requires target_person")
        elif self.target_person_id is not None:
            raise ValidationError("skip must not include target_person")
        if self.resulting_revision != self.expected_revision + 1:
            raise ValidationError("Import decision revision must increment by one")


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
