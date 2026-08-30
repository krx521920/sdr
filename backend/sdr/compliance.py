"""Tenant-scoped SDR contact eligibility, provenance, and retention workflows."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from sdr.domain import LeadCandidate, LeadSource
from sdr.models import (
    LeadDelivery,
    LeadDeliveryKind,
    LeadInspection,
    LeadIntake,
    LeadIntakeStatus,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    LeadNurtureInteraction,
    NurtureEnrollmentStatus,
    SDRChannelComplianceRule,
    SDRCollectionMethod,
    SDRComplianceChannel,
    SDRComplianceEvent,
    SDRComplianceEventType,
    SDRComplianceSettings,
    SDRDataProvenance,
    SDRDoNotContactEntry,
    SDRDoNotContactReason,
    SDRDoNotContactSource,
    SDREmailSuppression,
    SDRLawfulBasis,
    SDROutboundProspect,
    SDRProvenanceStatus,
    SDRRetentionMode,
)
from sdr.provider_ports import provider_data_governance_adapters
from sdr.routing import normalize_country

PHONE_NON_DIGITS = re.compile(r"\D")
WECHAT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{5,19}$")
SOURCE_COLLECTION_METHOD = {
    LeadSource.FACEBOOK_AD: SDRCollectionMethod.INBOUND_FORM,
    LeadSource.WEBSITE_FORM: SDRCollectionMethod.INBOUND_FORM,
    LeadSource.FACEBOOK_MESSENGER: SDRCollectionMethod.DIRECT_MESSAGE,
    LeadSource.LINKEDIN: SDRCollectionMethod.DIRECT_MESSAGE,
    LeadSource.EMAIL: SDRCollectionMethod.INBOUND_EMAIL,
    LeadSource.API: SDRCollectionMethod.PROVIDER_API,
    LeadSource.MANUAL: SDRCollectionMethod.MANUAL,
    LeadSource.OUTBOUND: SDRCollectionMethod.CSV_IMPORT,
}


class ComplianceBlocked(ValueError):
    def __init__(self, reason: str, *, code: str = "compliance_blocked"):
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True, slots=True)
class ComplianceDecision:
    allowed: bool
    code: str
    reason: str
    channel: str
    country_code: str


@dataclass(frozen=True, slots=True)
class DataProcessingRestriction:
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContactGovernanceContext:
    """Portable contact-governance facts for callers without an SDR intake."""

    lawful_basis: str = SDRLawfulBasis.UNASSESSED
    notes: str = ""
    consent: Mapping[str, Any] | None = None
    country: str = ""
    allowed_channels: tuple[str, ...] = ()
    processing_status: str = SDRProvenanceStatus.ACTIVE


PROVENANCE_PROCESSING_RESTRICTIONS = {
    SDRProvenanceStatus.RETENTION_DUE: DataProcessingRestriction(
        code="data_retention_due",
        reason="The intake's retention period has elapsed.",
    ),
    SDRProvenanceStatus.DELETION_REQUESTED: DataProcessingRestriction(
        code="data_deletion_requested",
        reason="The intake has an active data deletion request.",
    ),
    SDRProvenanceStatus.ANONYMIZED: DataProcessingRestriction(
        code="data_anonymized",
        reason="The intake's SDR-owned data has been anonymized.",
    ),
}
GOVERNANCE_PROCESSING_RESTRICTIONS = {
    **PROVENANCE_PROCESSING_RESTRICTIONS,
    "restricted": DataProcessingRestriction(
        code="data_processing_restricted",
        reason="Processing is restricted for this person.",
    ),
}


def normalize_contact_identifier(channel: str, value: str) -> str:
    cleaned = (value or "").strip()
    if channel == SDRComplianceChannel.EMAIL:
        return cleaned.lower()
    if channel in {SDRComplianceChannel.WHATSAPP, SDRComplianceChannel.PHONE}:
        return PHONE_NON_DIGITS.sub("", cleaned)
    if channel == SDRComplianceChannel.LINKEDIN:
        if "@" in cleaned and "://" not in cleaned:
            return cleaned.lower()
        parsed = urlsplit(cleaned if "://" in cleaned else f"https://{cleaned}")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/").lower()
        return urlunsplit(("https", host, path, "", "")) if host else cleaned.lower()
    if channel == SDRComplianceChannel.WECHAT:
        return cleaned.lower()
    raise ValueError("Unsupported compliance channel.")


def _identifier_hash(channel: str, identifier: str) -> str:
    return hashlib.sha256(f"{channel}:{identifier}".encode()).hexdigest()


def _valid_contact_identifier(channel: str, identifier: str) -> bool:
    if channel == SDRComplianceChannel.EMAIL:
        try:
            validate_email(identifier)
        except ValidationError:
            return False
        return True
    if channel in {SDRComplianceChannel.WHATSAPP, SDRComplianceChannel.PHONE}:
        return identifier.isdigit() and 8 <= len(identifier) <= 15
    if channel == SDRComplianceChannel.LINKEDIN:
        if "@" in identifier and "://" not in identifier:
            try:
                validate_email(identifier)
            except ValidationError:
                return False
            return True
        return bool(urlsplit(identifier).hostname)
    if channel == SDRComplianceChannel.WECHAT:
        return bool(WECHAT_ID_PATTERN.fullmatch(identifier))
    return False


def clean_channels(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,|;]", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    result = list(dict.fromkeys(str(item).strip().lower() for item in values if item))
    if any(item not in SDRComplianceChannel.values for item in result):
        raise ValueError("Allowed channels contain an unsupported channel.")
    return result


def get_compliance_settings(*, org_id: UUID) -> SDRComplianceSettings:
    settings, _ = SDRComplianceSettings.objects.get_or_create(org_id=org_id)
    return settings


def intake_data_restriction(
    intake: LeadIntake,
) -> DataProcessingRestriction | None:
    """Return the deletion lifecycle restriction for an intake, if any."""

    status = (
        SDRDataProvenance.objects.filter(
            org_id=intake.org_id,
            intake_id=intake.id,
        )
        .values_list("status", flat=True)
        .first()
    )
    return PROVENANCE_PROCESSING_RESTRICTIONS.get(status)


@transaction.atomic
def ensure_intake_provenance(
    *,
    intake: LeadIntake,
    candidate: LeadCandidate | None = None,
    raw_payload: Mapping[str, Any] | None = None,
) -> SDRDataProvenance:
    raw = dict(raw_payload or intake.raw_payload or {})
    prospect = getattr(intake, "outbound_prospect", None)
    source = candidate.source if candidate else LeadSource(intake.source)
    lawful_basis = str(
        raw.get("lawful_basis")
        or getattr(prospect, "lawful_basis", "")
        or SDRLawfulBasis.UNASSESSED
    ).strip()
    if lawful_basis not in SDRLawfulBasis.values:
        lawful_basis = SDRLawfulBasis.UNASSESSED
    allowed_channels = clean_channels(
        raw.get("allowed_channels")
        or getattr(prospect, "allowed_channels", None)
        or list(SDRComplianceChannel.values)
    )
    country = normalize_country(
        (candidate.company.country if candidate else None)
        or raw.get("country")
        or getattr(prospect, "country", "")
    )[:3]
    source_url = str(
        raw.get("source_url")
        or (candidate.attributes.get("source_url") if candidate else "")
        or getattr(prospect, "source_url", "")
    ).strip()[:1000]
    settings = get_compliance_settings(org_id=intake.org_id)
    retention_until = intake.created_at + timedelta(days=settings.retention_days)
    defaults = {
        "collection_method": SOURCE_COLLECTION_METHOD.get(
            source, SDRCollectionMethod.OTHER
        ),
        "source_url": source_url,
        "lawful_basis": lawful_basis,
        "lawful_basis_notes": str(
            raw.get("lawful_basis_notes") or getattr(prospect, "lawful_basis_notes", "")
        ).strip(),
        "consent_at": _consent_datetime(
            raw.get("consent_at") or getattr(prospect, "consent_at", None)
        ),
        "consent_evidence": str(
            raw.get("consent_evidence") or getattr(prospect, "consent_evidence", "")
        ).strip(),
        "country_code": country,
        "allowed_channels": allowed_channels,
        "retention_until": retention_until,
    }
    provenance, created = SDRDataProvenance.objects.get_or_create(
        org_id=intake.org_id,
        intake=intake,
        defaults=defaults,
    )
    if created:
        _record_event(
            org_id=intake.org_id,
            intake=intake,
            event_type=SDRComplianceEventType.PROVENANCE_RECORDED,
            event_key=f"provenance:{intake.id}",
            reason="Collection source and legal assessment recorded.",
            snapshot={
                "collection_method": provenance.collection_method,
                "lawful_basis": provenance.lawful_basis,
                "country_code": provenance.country_code,
                "allowed_channels": provenance.allowed_channels,
            },
        )
    return provenance


def evaluate_contact(
    *,
    org_id: UUID,
    channel: str,
    identifier: str,
    country_code: str = "",
    intake: LeadIntake | None = None,
    prospect: SDROutboundProspect | None = None,
    governance: ContactGovernanceContext | Mapping[str, Any] | None = None,
    event_key: str = "",
) -> ComplianceDecision:
    if channel not in SDRComplianceChannel.values:
        return _decision(
            False, "unsupported_channel", "Unsupported channel.", channel, ""
        )
    governance_context = _coerce_governance_context(governance)
    provenance = _provenance_for(intake=intake, prospect=prospect)
    country = normalize_country(
        country_code
        or (governance_context.country if governance_context else "")
        or (prospect.country if prospect else "")
    )[:3]
    normalized = normalize_contact_identifier(channel, identifier)
    identifier_hash = _identifier_hash(channel, normalized)
    restriction = (
        PROVENANCE_PROCESSING_RESTRICTIONS.get(provenance.status)
        if provenance
        else None
    ) or (
        GOVERNANCE_PROCESSING_RESTRICTIONS.get(governance_context.processing_status)
        if governance_context
        else None
    )
    if restriction:
        decision = _decision(
            False,
            restriction.code,
            restriction.reason,
            channel,
            country,
        )
        _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
        return decision
    if not normalized:
        return _decision(
            False,
            "missing_identifier",
            "A contact identifier is required.",
            channel,
            country,
        )

    if not _valid_contact_identifier(channel, normalized):
        decision = _decision(
            False,
            "invalid_identifier",
            "The contact identifier is invalid for this channel.",
            channel,
            country,
        )
        _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
        return decision

    dnc = SDRDoNotContactEntry.objects.filter(
        org_id=org_id,
        channel=channel,
        identifier_hash=identifier_hash,
        is_active=True,
    ).first()
    if dnc:
        decision = _decision(
            False,
            "do_not_contact",
            "The recipient is on the active do-not-contact list.",
            channel,
            country,
        )
        _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
        return decision
    if (
        channel == SDRComplianceChannel.EMAIL
        and SDREmailSuppression.objects.filter(
            org_id=org_id,
            email__iexact=normalized,
            is_active=True,
        ).exists()
    ):
        decision = _decision(
            False,
            "email_suppressed",
            "The email address is on the active suppression list.",
            channel,
            country,
        )
        _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
        return decision

    rule = (
        SDRChannelComplianceRule.objects.filter(
            org_id=org_id,
            channel=channel,
            country_code__in=[country, "*"] if country else ["*"],
        )
        .order_by("country_code")
        .first()
    )
    # Alphabetic ordering puts '*' before a country. Prefer the exact match.
    if country:
        rule = (
            SDRChannelComplianceRule.objects.filter(
                org_id=org_id, channel=channel, country_code=country
            ).first()
            or rule
        )
    if rule and not rule.is_allowed:
        decision = _decision(
            False,
            "country_channel_blocked",
            f"{channel} contact is disabled for {country or 'the default region'}.",
            channel,
            country,
        )
        _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
        return decision

    if governance_context:
        lawful_basis = governance_context.lawful_basis
        allowed_channels = clean_channels(governance_context.allowed_channels)
        consent_at, consent_evidence = _governance_consent(governance_context)
        lawful_basis_notes = governance_context.notes
    else:
        lawful_basis = (
            provenance.lawful_basis
            if provenance
            else getattr(prospect, "lawful_basis", SDRLawfulBasis.UNASSESSED)
        )
        allowed_channels = (
            provenance.allowed_channels
            if provenance
            else getattr(
                prospect, "allowed_channels", list(SDRComplianceChannel.values)
            )
        )
        consent_at = (
            provenance.consent_at
            if provenance
            else getattr(prospect, "consent_at", None)
        )
        consent_evidence = (
            provenance.consent_evidence
            if provenance
            else getattr(prospect, "consent_evidence", "")
        )
        lawful_basis_notes = (
            provenance.lawful_basis_notes
            if provenance
            else getattr(prospect, "lawful_basis_notes", "")
        )
    if (
        rule
        and rule.requires_consent
        and (
            lawful_basis != SDRLawfulBasis.CONSENT
            or channel not in allowed_channels
            or not consent_at
            or not consent_evidence.strip()
        )
    ):
        decision = _decision(
            False,
            "consent_required",
            f"Recorded consent is required for {channel} contact in {country or 'the default region'}.",
            channel,
            country,
        )
        _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
        return decision

    settings = get_compliance_settings(org_id=org_id)
    if settings.enforcement_enabled:
        if settings.require_lawful_basis and lawful_basis == SDRLawfulBasis.UNASSESSED:
            decision = _decision(
                False,
                "lawful_basis_unassessed",
                "A lawful basis must be assessed before outbound contact.",
                channel,
                country,
            )
            _audit_decision(
                decision, org_id, intake, prospect, event_key, identifier_hash
            )
            return decision
        if lawful_basis == SDRLawfulBasis.CONSENT and (
            not consent_at or not consent_evidence.strip()
        ):
            decision = _decision(
                False,
                "consent_evidence_missing",
                "Consent requires a timestamp and evidence reference before contact.",
                channel,
                country,
            )
            _audit_decision(
                decision, org_id, intake, prospect, event_key, identifier_hash
            )
            return decision
        if (
            lawful_basis == SDRLawfulBasis.LEGITIMATE_INTEREST
            and not lawful_basis_notes.strip()
        ):
            decision = _decision(
                False,
                "legitimate_interest_assessment_missing",
                "Legitimate interest requires a documented assessment reference.",
                channel,
                country,
            )
            _audit_decision(
                decision, org_id, intake, prospect, event_key, identifier_hash
            )
            return decision
        if channel not in allowed_channels:
            decision = _decision(
                False,
                "channel_not_permitted",
                "The provenance record does not permit this contact channel.",
                channel,
                country,
            )
            _audit_decision(
                decision, org_id, intake, prospect, event_key, identifier_hash
            )
            return decision

    decision = _decision(
        True, "allowed", "Contact is permitted by current controls.", channel, country
    )
    _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash)
    return decision


def require_contact_allowed(**kwargs) -> ComplianceDecision:
    decision = evaluate_contact(**kwargs)
    if not decision.allowed:
        raise ComplianceBlocked(decision.reason, code=decision.code)
    return decision


@transaction.atomic
def block_contact(
    *,
    org_id: UUID,
    channel: str,
    identifier: str,
    reason: str,
    source: str,
    country_code: str = "",
    details: Mapping[str, Any] | None = None,
    created_by=None,
) -> tuple[SDRDoNotContactEntry, bool]:
    if channel not in SDRComplianceChannel.values:
        raise ValueError("Unsupported compliance channel.")
    if reason not in SDRDoNotContactReason.values:
        raise ValueError("Unsupported do-not-contact reason.")
    if source not in SDRDoNotContactSource.values:
        raise ValueError("Unsupported do-not-contact source.")
    normalized = normalize_contact_identifier(channel, identifier)
    if not normalized:
        raise ValueError("A contact identifier is required.")
    digest = _identifier_hash(channel, normalized)
    now = timezone.now()
    entry = (
        SDRDoNotContactEntry.objects.select_for_update()
        .filter(
            org_id=org_id,
            channel=channel,
            identifier_hash=digest,
        )
        .first()
    )
    created = entry is None
    values = {
        "identifier": normalized,
        "country_code": normalize_country(country_code)[:3],
        "reason": reason,
        "source": source,
        "is_active": True,
        "blocked_at": now,
        "released_at": None,
        "details": dict(details or {}),
        "created_by": created_by,
        "updated_by": created_by,
    }
    if entry is None:
        entry = SDRDoNotContactEntry.objects.create(
            org_id=org_id,
            channel=channel,
            identifier_hash=digest,
            **values,
        )
    else:
        for field, value in values.items():
            setattr(entry, field, value)
        entry.save()
    _record_event(
        org_id=org_id,
        event_type=SDRComplianceEventType.DNC_ADDED,
        channel=channel,
        allowed=False,
        event_key=f"dnc:add:{entry.id}:{entry.blocked_at.isoformat()}",
        reason=reason,
        snapshot={"identifier_hash": digest, "source": source},
        created_by=created_by,
    )
    return entry, created


@transaction.atomic
def release_contact_block(entry: SDRDoNotContactEntry, *, updated_by=None):
    entry = SDRDoNotContactEntry.objects.select_for_update().get(
        id=entry.id, org_id=entry.org_id
    )
    if not entry.is_active:
        return entry
    entry.is_active = False
    entry.released_at = timezone.now()
    entry.updated_by = updated_by
    entry.save(update_fields=["is_active", "released_at", "updated_by", "updated_at"])
    _record_event(
        org_id=entry.org_id,
        event_type=SDRComplianceEventType.DNC_RELEASED,
        channel=entry.channel,
        allowed=True,
        event_key=f"dnc:release:{entry.id}:{entry.released_at.isoformat()}",
        reason="Do-not-contact entry released by an organization administrator.",
        snapshot={"identifier_hash": entry.identifier_hash},
        created_by=updated_by,
    )
    return entry


@transaction.atomic
def request_intake_deletion(intake: LeadIntake, *, requested_by=None):
    intake = LeadIntake.objects.select_for_update().get(
        id=intake.id,
        org_id=intake.org_id,
    )
    provenance = ensure_intake_provenance(intake=intake)
    provenance.status = SDRProvenanceStatus.DELETION_REQUESTED
    provenance.deletion_requested_at = timezone.now()
    provenance.updated_by = requested_by
    provenance.save(
        update_fields=["status", "deletion_requested_at", "updated_by", "updated_at"]
    )
    _record_event(
        org_id=intake.org_id,
        intake=intake,
        event_type=SDRComplianceEventType.DELETION_REQUESTED,
        event_key=f"deletion:request:{intake.id}:{provenance.deletion_requested_at.isoformat()}",
        reason="A data deletion request was recorded.",
        created_by=requested_by,
    )
    return provenance


@transaction.atomic
def cancel_intake_deletion(intake: LeadIntake, *, updated_by=None):
    intake = LeadIntake.objects.select_for_update().get(
        id=intake.id,
        org_id=intake.org_id,
    )
    provenance = ensure_intake_provenance(intake=intake)
    if provenance.status != SDRProvenanceStatus.DELETION_REQUESTED:
        return provenance
    provenance.status = SDRProvenanceStatus.ACTIVE
    provenance.deletion_requested_at = None
    provenance.updated_by = updated_by
    provenance.save(
        update_fields=["status", "deletion_requested_at", "updated_by", "updated_at"]
    )
    _record_event(
        org_id=intake.org_id,
        intake=intake,
        event_type=SDRComplianceEventType.DELETION_CANCELLED,
        event_key=f"deletion:cancel:{intake.id}:{timezone.now().isoformat()}",
        reason="The data deletion request was cancelled.",
        created_by=updated_by,
    )
    return provenance


def scan_retention(
    *, org_id: UUID, execute: bool = False, limit: int = 200
) -> dict[str, Any]:
    settings = get_compliance_settings(org_id=org_id)
    now = timezone.now()
    cutoff = now - timedelta(days=settings.retention_days)
    missing = LeadIntake.objects.filter(
        org_id=org_id,
        created_at__lte=cutoff,
        status__in=[LeadIntakeStatus.COMPLETED, LeadIntakeStatus.FAILED],
        data_provenance__isnull=True,
    ).order_by("created_at")[:limit]
    for intake in missing:
        ensure_intake_provenance(intake=intake)

    deletion_filter = Q(
        status=SDRProvenanceStatus.DELETION_REQUESTED,
        deletion_requested_at__lte=now - timedelta(days=settings.deletion_grace_days),
    )
    due_filter = deletion_filter
    if settings.retention_mode != SDRRetentionMode.DISABLED:
        due_filter |= Q(retention_until__lte=now)
    due = list(
        SDRDataProvenance.objects.filter(org_id=org_id)
        .filter(due_filter)
        .exclude(status=SDRProvenanceStatus.ANONYMIZED)
        .select_related("intake")
        .order_by("retention_until", "created_at")[:limit]
    )
    anonymized = 0
    marked_due = 0
    for provenance in due:
        deletion_due = provenance.status == SDRProvenanceStatus.DELETION_REQUESTED
        should_anonymize = execute and (
            deletion_due or settings.retention_mode == SDRRetentionMode.ANONYMIZE_SDR
        )
        if should_anonymize:
            anonymize_intake(provenance.intake, performed_by=None)
            anonymized += 1
        elif (
            not deletion_due and provenance.status != SDRProvenanceStatus.RETENTION_DUE
        ):
            provenance.status = SDRProvenanceStatus.RETENTION_DUE
            provenance.save(update_fields=["status", "updated_at"])
            _record_event(
                org_id=org_id,
                intake=provenance.intake,
                event_type=SDRComplianceEventType.RETENTION_DUE,
                event_key=f"retention:due:{provenance.intake_id}",
                reason="The configured SDR retention period has elapsed.",
            )
            marked_due += 1
    SDRComplianceSettings.objects.filter(id=settings.id, org_id=org_id).update(
        last_retention_scan_at=now
    )
    return {
        "retention_mode": settings.retention_mode,
        "execute": execute,
        "due": len(due),
        "marked_due": marked_due,
        "anonymized": anonymized,
        "crm_records_changed": 0,
    }


@transaction.atomic
def anonymize_intake(intake: LeadIntake, *, performed_by=None) -> SDRDataProvenance:
    intake = LeadIntake.objects.select_for_update().get(
        id=intake.id, org_id=intake.org_id
    )
    provenance = ensure_intake_provenance(intake=intake)
    if provenance.status == SDRProvenanceStatus.ANONYMIZED:
        return provenance
    now = timezone.now()
    marker = hashlib.sha256(f"{intake.org_id}:{intake.id}".encode()).hexdigest()[:20]
    LeadInspection.objects.filter(org_id=intake.org_id, intake=intake).update(
        website_url="",
        source_urls=[],
        research_summary="",
        research_facts={},
        provider_response_id="",
        qualification_reasons=[],
        provider_attempts=[],
        error_message="",
    )
    LeadDelivery.objects.filter(
        org_id=intake.org_id,
        intake=intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
    ).update(
        recipient=f"redacted:{marker}",
        last_error_message="",
    )
    LeadNurtureInteraction.objects.filter(
        org_id=intake.org_id,
        delivery__enrollment__intake=intake,
    ).update(target_url="")
    LeadNurtureDelivery.objects.filter(
        org_id=intake.org_id,
        enrollment__intake=intake,
    ).update(
        recipient=f"redacted+{marker}@invalid.local",
        subject_template="",
        body_template="",
        last_error_message="",
        last_clicked_url="",
        reply_message_id="",
    )
    LeadNurtureEnrollment.objects.filter(
        org_id=intake.org_id,
        intake=intake,
        status__in=[NurtureEnrollmentStatus.ACTIVE, NurtureEnrollmentStatus.PAUSED],
    ).update(
        status=NurtureEnrollmentStatus.CANCELLED,
        stop_reason="SDR-owned personal data was anonymized.",
        next_run_at=None,
        completed_at=now,
    )
    SDROutboundProspect.objects.filter(org_id=intake.org_id, intake=intake).update(
        first_name="",
        last_name="",
        email="",
        phone="",
        job_title="",
        linkedin_url="",
        company_name="Anonymized",
        website="",
        source_url="",
        notes="",
        lawful_basis_notes="",
        consent_at=None,
        consent_evidence="",
        allowed_channels=[],
    )
    provider_governance: dict[str, int] = {}
    for adapter in provider_data_governance_adapters():
        result = adapter.anonymize_intake_data(
            org_id=intake.org_id,
            intake_id=intake.id,
            marker=marker,
        )
        for key, value in result.items():
            provider_governance[str(key)] = provider_governance.get(str(key), 0) + int(
                value
            )
    LeadIntake.objects.filter(id=intake.id, org_id=intake.org_id).update(
        source_record_id=f"anon:{marker}",
        raw_payload={},
        normalized_payload={},
        error_message="",
        routing_reason="",
    )
    provenance.source_url = ""
    provenance.lawful_basis_notes = ""
    provenance.consent_at = None
    provenance.consent_evidence = ""
    provenance.allowed_channels = []
    provenance.status = SDRProvenanceStatus.ANONYMIZED
    provenance.anonymized_at = now
    provenance.updated_by = performed_by
    provenance.save(
        update_fields=[
            "source_url",
            "lawful_basis_notes",
            "consent_at",
            "consent_evidence",
            "allowed_channels",
            "status",
            "anonymized_at",
            "updated_by",
            "updated_at",
        ]
    )
    _record_event(
        org_id=intake.org_id,
        intake=intake,
        event_type=SDRComplianceEventType.ANONYMIZED,
        event_key=f"anonymized:{intake.id}",
        reason="SDR-owned personal data was anonymized; linked CRM data was not changed.",
        snapshot={
            "crm_lead_review_required": bool(intake.crm_lead_id),
            "provider_governance": provider_governance,
        },
        created_by=performed_by,
    )
    return provenance


def _provenance_for(*, intake=None, prospect=None):
    target_intake = intake or (
        prospect.intake if prospect and prospect.intake_id else None
    )
    if not target_intake:
        return None
    # Do not trust the reverse one-to-one cache for an execution-time decision.
    # A worker can hold an intake/prospect instance across a deletion-state update.
    provenance = SDRDataProvenance.objects.filter(
        org_id=target_intake.org_id,
        intake_id=target_intake.id,
    ).first()
    return provenance or ensure_intake_provenance(intake=target_intake)


def _consent_datetime(value):
    if not value or not isinstance(value, str):
        return value or None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _coerce_governance_context(
    value: ContactGovernanceContext | Mapping[str, Any] | None,
) -> ContactGovernanceContext | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        if not isinstance(value, ContactGovernanceContext):
            raise TypeError("governance must be a mapping or ContactGovernanceContext")
        values = {
            "lawful_basis": value.lawful_basis,
            "notes": value.notes,
            "consent": value.consent,
            "country": value.country,
            "allowed_channels": value.allowed_channels,
            "processing_status": value.processing_status,
        }
    else:
        values = value
    lawful_basis = str(values.get("lawful_basis") or SDRLawfulBasis.UNASSESSED).strip()
    if lawful_basis not in SDRLawfulBasis.values:
        lawful_basis = SDRLawfulBasis.UNASSESSED
    processing_status = str(
        values.get("processing_status") or SDRProvenanceStatus.ACTIVE
    ).strip()
    if processing_status not in {
        *SDRProvenanceStatus.values,
        "restricted",
    }:
        processing_status = "restricted"
    return ContactGovernanceContext(
        lawful_basis=lawful_basis,
        notes=str(values.get("notes") or "").strip(),
        consent=(
            values.get("consent")
            if isinstance(values.get("consent"), Mapping)
            else None
        ),
        country=str(values.get("country") or "").strip(),
        allowed_channels=tuple(clean_channels(values.get("allowed_channels") or [])),
        processing_status=processing_status,
    )


def _governance_consent(
    context: ContactGovernanceContext,
) -> tuple[Any | None, str]:
    consent = context.consent
    if not isinstance(consent, Mapping) or consent.get("granted") is False:
        return None, ""
    recorded_at = _consent_datetime(
        consent.get("recorded_at") or consent.get("consent_at") or consent.get("at")
    )
    evidence = str(
        consent.get("evidence")
        or consent.get("evidence_reference")
        or consent.get("reference")
        or ""
    ).strip()
    return recorded_at, evidence


def _decision(allowed, code, reason, channel, country_code):
    return ComplianceDecision(
        allowed=allowed,
        code=code,
        reason=reason,
        channel=channel,
        country_code=country_code,
    )


def _audit_decision(decision, org_id, intake, prospect, event_key, identifier_hash):
    if not event_key:
        return
    _record_event(
        org_id=org_id,
        intake=intake,
        prospect=prospect,
        event_type=(
            SDRComplianceEventType.CONTACT_ALLOWED
            if decision.allowed
            else SDRComplianceEventType.CONTACT_BLOCKED
        ),
        channel=decision.channel,
        allowed=decision.allowed,
        event_key=f"contact:{event_key}:{decision.code}",
        reason=decision.reason,
        snapshot={
            "code": decision.code,
            "country_code": decision.country_code,
            "identifier_hash": identifier_hash,
        },
    )


def _record_event(
    *,
    org_id,
    event_type,
    event_key,
    intake=None,
    prospect=None,
    channel="",
    allowed=None,
    reason="",
    snapshot=None,
    created_by=None,
):
    event, _ = SDRComplianceEvent.objects.get_or_create(
        org_id=org_id,
        event_key=event_key[:255],
        defaults={
            "intake": intake,
            "prospect": prospect,
            "event_type": event_type,
            "channel": channel,
            "allowed": allowed,
            "reason": reason[:500],
            "snapshot": dict(snapshot or {}),
            "created_by": created_by,
        },
    )
    return event
