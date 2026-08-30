"""Tenant-scoped evidence and person governance workflows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from matching.locking import lock_matching_org
from matching.models import (
    Evidence,
    EvidenceCollectionMethod,
    EvidenceConfirmationStatus,
    EvidenceGovernanceAction,
    EvidenceGovernanceEvent,
    EvidenceLawfulBasis,
    EvidenceProcessingStatus,
    EvidenceProvenance,
    EvidenceSource,
    GovernanceContactChannel,
    MatchOpportunity,
    MatchOpportunityStatus,
    Person,
    PersonContactIntent,
    PersonContactIntentEvent,
    PersonContactIntentPurpose,
    PersonContactIntentState,
    PersonGovernanceEvent,
    PersonGovernanceEventType,
    PersonGovernanceStatus,
    PersonIdentity,
    PersonImportRecord,
    default_governance_channels,
)
from matching.services import RecomputeEnqueueError, enqueue_opportunity_recompute

GOVERNANCE_NAMESPACE = UUID("72f951bd-ebfc-4c35-a06a-b3d830925c35")
RAW_CONTENT_KEYS = {
    "raw_content",
    "message_body",
    "body_text",
    "body_html",
    "transcript",
    "chat_text",
    "conversation",
    "provider_payload",
}


class GovernanceError(ValueError):
    def __init__(self, *, code: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class GovernanceMutationResult:
    value: object
    event: object
    replayed: bool
    match_run_ids: tuple[UUID, ...] = ()


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(type(value).__name__)


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def _request_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def reject_raw_content_keys(value) -> None:
    """Fail closed if a public request attempts to persist raw source content."""

    if not isinstance(value, dict):
        return
    found = {str(key).casefold() for key in value} & RAW_CONTENT_KEYS
    if found:
        raise GovernanceError(
            code="raw_content_not_accepted",
            detail="Raw messages, transcripts, chats, and provider payloads are not accepted.",
        )


def _collection_method(source: str, requested: str | None = None) -> str:
    if requested:
        return requested
    if source == EvidenceSource.AI:
        return EvidenceCollectionMethod.AI_EXTRACTION
    if source == EvidenceSource.MANUAL:
        return EvidenceCollectionMethod.MANUAL
    return EvidenceCollectionMethod.PROVIDER_API


def _safe_provenance_snapshot(provenance: EvidenceProvenance) -> dict:
    return {
        "confirmation_status": provenance.confirmation_status,
        "processing_status": provenance.processing_status,
        "lawful_basis": provenance.lawful_basis,
        "country_code": provenance.country_code,
        "allowed_channels": list(provenance.allowed_channels or []),
        "allowed_purposes": list(provenance.allowed_purposes or []),
        "retention_until": (
            provenance.retention_until.isoformat()
            if provenance.retention_until
            else None
        ),
        "revision": provenance.revision,
    }


def ensure_evidence_provenance(
    *,
    evidence: Evidence,
    actor=None,
    collection_method: str | None = None,
    source_content_sha256: str = "",
) -> EvidenceProvenance:
    """Create the mandatory projection and its immutable initial receipt."""

    if actor is not None and actor.org_id != evidence.org_id:
        raise GovernanceError(
            code="actor_org_conflict",
            detail="Governance actor belongs to another organization.",
            status_code=409,
        )
    collection_method = _collection_method(evidence.source, collection_method)
    requires_confirmation = (
        evidence.source == EvidenceSource.AI
        or collection_method == EvidenceCollectionMethod.INBOUND_EMAIL
    )
    now = timezone.now()
    provenance, created = EvidenceProvenance.objects.get_or_create(
        org_id=evidence.org_id,
        evidence=evidence,
        defaults={
            "collection_method": collection_method,
            "confirmation_status": (
                EvidenceConfirmationStatus.PENDING
                if requires_confirmation
                else EvidenceConfirmationStatus.CONFIRMED
            ),
            "source_content_sha256": source_content_sha256,
            "confirmed_by": None if requires_confirmation else actor,
            "confirmed_at": None if requires_confirmation else now,
            "revision": 1,
        },
    )
    if not created:
        return provenance
    key = uuid5(GOVERNANCE_NAMESPACE, f"evidence-created:{evidence.id}")
    EvidenceGovernanceEvent.objects.create(
        org_id=evidence.org_id,
        provenance=provenance,
        evidence=evidence,
        action=EvidenceGovernanceAction.CREATED,
        actor=actor,
        expected_revision=0,
        resulting_revision=1,
        idempotency_key=key,
        request_hash=_request_hash(
            {
                "evidence_id": evidence.id,
                "source": evidence.source,
                "confirmation_status": provenance.confirmation_status,
            }
        ),
        safe_snapshot=_safe_provenance_snapshot(provenance),
    )
    return provenance


def _validate_provenance_changes(changes: dict) -> dict:
    reject_raw_content_keys(changes)
    allowed = {
        "collection_method",
        "lawful_basis",
        "lawful_basis_notes",
        "consent_at",
        "consent_evidence_ref",
        "country_code",
        "allowed_channels",
        "allowed_purposes",
        "retention_until",
        "processing_status",
        "source_content_sha256",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise GovernanceError(
            code="unsupported_governance_field",
            detail=f"Unsupported governance field(s): {', '.join(sorted(unknown))}.",
        )
    cleaned = dict(changes)
    if "allowed_channels" in cleaned:
        values = list(dict.fromkeys(cleaned["allowed_channels"] or []))
        if any(value not in GovernanceContactChannel.values for value in values):
            raise GovernanceError(
                code="invalid_allowed_channels",
                detail="allowed_channels contains an unsupported channel.",
            )
        cleaned["allowed_channels"] = values
    if "allowed_purposes" in cleaned:
        values = list(dict.fromkeys(cleaned["allowed_purposes"] or []))
        if any(value not in PersonContactIntentPurpose.values for value in values):
            raise GovernanceError(
                code="invalid_allowed_purposes",
                detail="allowed_purposes contains an unsupported purpose.",
            )
        cleaned["allowed_purposes"] = values
    if "country_code" in cleaned:
        cleaned["country_code"] = str(cleaned["country_code"] or "").upper()[:3]
    digest = cleaned.get("source_content_sha256")
    if digest and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
        raise GovernanceError(
            code="invalid_source_content_sha256",
            detail="source_content_sha256 must be a lowercase SHA-256 digest.",
        )
    return cleaned


def _existing_evidence_event(*, org, idempotency_key, request_hash, evidence_id):
    event = EvidenceGovernanceEvent.objects.filter(
        org=org,
        idempotency_key=idempotency_key,
    ).select_related("provenance", "evidence").first()
    if event is None:
        return None
    if event.request_hash != request_hash or event.evidence_id != evidence_id:
        raise GovernanceError(
            code="governance_idempotency_conflict",
            detail="Idempotency-Key was already used with a different request.",
            status_code=409,
        )
    return event


def _enqueue_person_recompute(*, person: Person, key_seed: str) -> tuple[UUID, ...]:
    if person.governance_status != PersonGovernanceStatus.ACTIVE:
        return ()
    run_ids = []
    for opportunity in MatchOpportunity.objects.filter(
        org=person.org,
        status=MatchOpportunityStatus.OPEN,
    ).order_by("id"):
        key = uuid5(GOVERNANCE_NAMESPACE, f"{key_seed}:{opportunity.id}")
        try:
            run = enqueue_opportunity_recompute(
                org=person.org,
                opportunity=opportunity,
                requested_by=None,
                idempotency_key=key,
                person_ids=[person.id],
            )
        except RecomputeEnqueueError:
            continue
        run_ids.append(run.id)
    return tuple(run_ids)


@transaction.atomic
def update_evidence_provenance(
    *, org, evidence_id, actor, idempotency_key: UUID, expected_revision: int, changes
) -> GovernanceMutationResult:
    request_hash = _request_hash(
        {
            "action": "update_provenance",
            "evidence_id": evidence_id,
            "expected_revision": expected_revision,
            "changes": changes,
        }
    )
    replay = _existing_evidence_event(
        org=org,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        evidence_id=evidence_id,
    )
    if replay:
        return GovernanceMutationResult(replay.provenance, replay, True)
    cleaned = _validate_provenance_changes(changes)
    provenance = EvidenceProvenance.objects.select_for_update().filter(
        org=org,
        evidence_id=evidence_id,
    ).select_related("evidence__person").first()
    if provenance is None:
        raise GovernanceError(code="evidence_not_found", detail="Evidence was not found.", status_code=404)
    if provenance.revision != expected_revision:
        raise GovernanceError(code="governance_revision_conflict", detail="Governance revision is stale.", status_code=409)
    for field, value in cleaned.items():
        setattr(provenance, field, value)
    if provenance.lawful_basis == EvidenceLawfulBasis.CONSENT and (
        not provenance.consent_at or not provenance.consent_evidence_ref.strip()
    ):
        raise GovernanceError(
            code="consent_evidence_required",
            detail="Consent requires a timestamp and an evidence reference.",
        )
    provenance.revision += 1
    provenance.full_clean()
    provenance.save(update_fields=[*cleaned, "revision", "updated_at"])
    event = EvidenceGovernanceEvent.objects.create(
        org=org,
        provenance=provenance,
        evidence=provenance.evidence,
        action=EvidenceGovernanceAction.PROVENANCE_UPDATED,
        actor=actor,
        expected_revision=expected_revision,
        resulting_revision=provenance.revision,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        safe_snapshot=_safe_provenance_snapshot(provenance),
    )
    run_ids = _enqueue_person_recompute(
        person=provenance.evidence.person,
        key_seed=f"provenance:{event.id}",
    )
    return GovernanceMutationResult(provenance, event, False, run_ids)


@transaction.atomic
def review_evidence(
    *, org, evidence_id, actor, idempotency_key: UUID, expected_revision: int, action: str, reason_code: str = ""
) -> GovernanceMutationResult:
    if action not in {"confirm", "reject"}:
        raise GovernanceError(code="invalid_review_action", detail="action must be confirm or reject.")
    request_hash = _request_hash(
        {
            "action": action,
            "evidence_id": evidence_id,
            "expected_revision": expected_revision,
            "reason_code": reason_code,
        }
    )
    replay = _existing_evidence_event(
        org=org,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        evidence_id=evidence_id,
    )
    if replay:
        return GovernanceMutationResult(replay.provenance, replay, True)
    provenance = EvidenceProvenance.objects.select_for_update().filter(
        org=org,
        evidence_id=evidence_id,
    ).select_related("evidence__person").first()
    if provenance is None:
        raise GovernanceError(code="evidence_not_found", detail="Evidence was not found.", status_code=404)
    if actor is None or actor.org_id != org.id:
        raise GovernanceError(code="review_actor_required", detail="A same-organization reviewer is required.", status_code=403)
    if provenance.revision != expected_revision:
        raise GovernanceError(code="governance_revision_conflict", detail="Governance revision is stale.", status_code=409)
    provenance.confirmation_status = (
        EvidenceConfirmationStatus.CONFIRMED
        if action == "confirm"
        else EvidenceConfirmationStatus.REJECTED
    )
    provenance.confirmed_by = actor
    provenance.confirmed_at = timezone.now()
    provenance.revision += 1
    provenance.full_clean()
    provenance.save(
        update_fields=[
            "confirmation_status",
            "confirmed_by",
            "confirmed_at",
            "revision",
            "updated_at",
        ]
    )
    event = EvidenceGovernanceEvent.objects.create(
        org=org,
        provenance=provenance,
        evidence=provenance.evidence,
        action=(
            EvidenceGovernanceAction.CONFIRMED
            if action == "confirm"
            else EvidenceGovernanceAction.REJECTED
        ),
        actor=actor,
        expected_revision=expected_revision,
        resulting_revision=provenance.revision,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        reason_code=reason_code,
        safe_snapshot=_safe_provenance_snapshot(provenance),
    )
    run_ids = _enqueue_person_recompute(
        person=provenance.evidence.person,
        key_seed=f"evidence-review:{event.id}",
    )
    return GovernanceMutationResult(provenance, event, False, run_ids)


def _existing_intent_event(*, org, idempotency_key, request_hash, person_id):
    event = PersonContactIntentEvent.objects.filter(
        org=org,
        idempotency_key=idempotency_key,
    ).select_related("intent").first()
    if event is None:
        return None
    if event.request_hash != request_hash or event.intent.person_id != person_id:
        raise GovernanceError(
            code="intent_idempotency_conflict",
            detail="Idempotency-Key was already used with a different request.",
            status_code=409,
        )
    return event


def _sync_objected_intent(intent: PersonContactIntent):
    if intent.state != PersonContactIntentState.OBJECTED:
        return
    from sdr.compliance import block_contact

    identity_kind = {
        GovernanceContactChannel.EMAIL: "email",
        GovernanceContactChannel.WHATSAPP: "whatsapp",
        GovernanceContactChannel.LINKEDIN: "linkedin",
        GovernanceContactChannel.PHONE: "phone",
        GovernanceContactChannel.WECHAT: "wechat",
    }.get(intent.channel)
    if intent.identity_id:
        identities = [intent.identity]
    elif identity_kind:
        identities = PersonIdentity.objects.filter(
            org_id=intent.org_id,
            person_id=intent.person_id,
            kind=identity_kind,
        )
    else:
        identities = []
    for identity in identities:
        block_contact(
            org_id=intent.org_id,
            channel=intent.channel,
            identifier=identity.normalized_value,
            reason="unsubscribed",
            source="data_subject",
            details={"matching_contact_intent_id": str(intent.id)},
        )


@transaction.atomic
def upsert_contact_intent(
    *,
    org,
    person_id,
    actor,
    idempotency_key: UUID,
    expected_revision: int,
    channel: str,
    purpose: str,
    state: str,
    source: str,
    identity_id=None,
    evidence_id=None,
    opportunity_id=None,
    confidence=Decimal("0.500"),
    observed_at=None,
    valid_until=None,
    reason_code="",
) -> GovernanceMutationResult:
    payload = {
        "person_id": person_id,
        "expected_revision": expected_revision,
        "channel": channel,
        "purpose": purpose,
        "state": state,
        "source": source,
        "identity_id": identity_id,
        "evidence_id": evidence_id,
        "opportunity_id": opportunity_id,
        "confidence": confidence,
        "observed_at": observed_at,
        "valid_until": valid_until,
        "reason_code": reason_code,
    }
    request_hash = _request_hash(payload)
    replay = _existing_intent_event(
        org=org,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        person_id=person_id,
    )
    if replay:
        return GovernanceMutationResult(replay.intent, replay, True)
    person = Person.objects.filter(org=org, id=person_id).first()
    if person is None:
        raise GovernanceError(code="person_not_found", detail="Person was not found.", status_code=404)
    identity = None
    if identity_id:
        identity = PersonIdentity.objects.filter(org=org, person=person, id=identity_id).first()
        if identity is None:
            raise GovernanceError(code="identity_not_found", detail="Identity was not found.", status_code=404)
    evidence = None
    if evidence_id:
        evidence = Evidence.objects.filter(org=org, person=person, id=evidence_id).first()
        if evidence is None:
            raise GovernanceError(code="evidence_not_found", detail="Evidence was not found.", status_code=404)
    opportunity = None
    if opportunity_id:
        opportunity = MatchOpportunity.objects.filter(org=org, id=opportunity_id).first()
        if opportunity is None:
            raise GovernanceError(code="opportunity_not_found", detail="Opportunity was not found.", status_code=404)
    intent, _created = PersonContactIntent.objects.select_for_update().get_or_create(
        org=org,
        person=person,
        channel=channel,
        purpose=purpose,
        defaults={"state": PersonContactIntentState.UNKNOWN, "source": EvidenceSource.MANUAL},
    )
    if intent.revision != expected_revision:
        raise GovernanceError(code="intent_revision_conflict", detail="Contact intent revision is stale.", status_code=409)
    now_observed = observed_at or timezone.now()
    if valid_until and valid_until < now_observed:
        raise GovernanceError(code="invalid_intent_validity", detail="valid_until cannot precede observed_at.")
    from_state = intent.state
    is_ai = source == EvidenceSource.AI
    resulting_revision = intent.revision
    if not is_ai:
        if actor is not None and actor.org_id != org.id:
            raise GovernanceError(code="actor_org_conflict", detail="Actor belongs to another organization.", status_code=409)
        intent.identity = identity
        intent.evidence = evidence
        intent.opportunity = opportunity
        intent.state = state
        intent.source = source
        intent.confidence = confidence
        intent.observed_at = now_observed
        intent.valid_until = valid_until
        intent.revision += 1
        intent.full_clean()
        intent.save()
        resulting_revision = intent.revision
    event = PersonContactIntentEvent.objects.create(
        org=org,
        intent=intent,
        evidence=evidence,
        actor=actor,
        from_state=from_state,
        to_state=state,
        source=source,
        confirmation_status=(
            EvidenceConfirmationStatus.PENDING
            if is_ai
            else EvidenceConfirmationStatus.CONFIRMED
        ),
        expected_revision=expected_revision,
        resulting_revision=resulting_revision,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        reason_code=reason_code,
        safe_snapshot={
            "channel": channel,
            "purpose": purpose,
            "state": state,
            "confirmation_status": (
                "pending" if is_ai else "confirmed"
            ),
        },
    )
    if not is_ai and intent.state == PersonContactIntentState.OBJECTED:
        transaction.on_commit(lambda: _sync_objected_intent(intent))
    return GovernanceMutationResult(intent, event, False)


def safe_provenance(provenance: EvidenceProvenance) -> dict:
    data = _safe_provenance_snapshot(provenance)
    data.update(
        {
            "id": str(provenance.id),
            "evidence_id": str(provenance.evidence_id),
            "collection_method": provenance.collection_method,
            "confirmed_at": (
                provenance.confirmed_at.isoformat()
                if provenance.confirmed_at
                else None
            ),
            "expiry_processed_at": (
                provenance.expiry_processed_at.isoformat()
                if provenance.expiry_processed_at
                else None
            ),
        }
    )
    return data


def safe_governance_evidence(provenance: EvidenceProvenance) -> dict:
    """Return evidence metadata that is useful for review without raw facts/URIs."""

    evidence = provenance.evidence
    now = timezone.now()
    expired = bool(
        (evidence.valid_until and evidence.valid_until < now)
        or (provenance.retention_until and provenance.retention_until < now)
    )
    expiring_cutoff = now + timedelta(days=30)
    expiring = bool(
        not expired
        and (
            (evidence.valid_until and evidence.valid_until <= expiring_cutoff)
            or (
                provenance.retention_until
                and provenance.retention_until <= expiring_cutoff
            )
        )
    )
    return {
        "id": str(evidence.id),
        "person_id": str(evidence.person_id),
        "kind": evidence.kind,
        "source": evidence.source,
        "summary": evidence.summary,
        "observed_at": evidence.observed_at.isoformat(),
        "valid_until": evidence.valid_until.isoformat() if evidence.valid_until else None,
        "confidence": str(evidence.confidence),
        "review_status": provenance.confirmation_status,
        "freshness": "expired" if expired else ("expiring" if expiring else "active"),
        "revision": provenance.revision,
        "ai_generated": evidence.source == EvidenceSource.AI,
        "governance": safe_provenance(provenance),
    }


def _mask_identity(identity: PersonIdentity) -> str:
    value = identity.normalized_value
    if identity.kind == "email" and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    return f"***{value[-4:]}" if value else "***"


def safe_intent(intent: PersonContactIntent) -> dict:
    return {
        "id": str(intent.id),
        "person_id": str(intent.person_id),
        "identity": (
            {"id": str(intent.identity_id), "kind": intent.identity.kind, "masked_value": _mask_identity(intent.identity)}
            if intent.identity_id
            else None
        ),
        "opportunity_id": str(intent.opportunity_id) if intent.opportunity_id else None,
        "evidence_id": str(intent.evidence_id) if intent.evidence_id else None,
        "channel": intent.channel,
        "purpose": intent.purpose,
        "state": intent.state,
        "source": intent.source,
        "confidence": str(intent.confidence),
        "observed_at": intent.observed_at.isoformat(),
        "valid_until": intent.valid_until.isoformat() if intent.valid_until else None,
        "revision": intent.revision,
    }


def safe_person_governance(person: Person) -> dict:
    evidence = [
        item.provenance
        for item in person.evidence.all()
        if hasattr(item, "provenance")
    ]
    evidence.sort(key=lambda item: item.evidence.observed_at, reverse=True)
    intents = list(person.contact_intents.all())
    intents.sort(key=lambda item: (item.channel, item.purpose))
    now = timezone.now()
    expiring_cutoff = now + timedelta(days=30)

    def is_expired(item):
        return bool(
            (item.evidence.valid_until and item.evidence.valid_until < now)
            or (item.retention_until and item.retention_until < now)
        )

    def is_expiring(item):
        if is_expired(item):
            return False
        return bool(
            (
                item.evidence.valid_until
                and item.evidence.valid_until <= expiring_cutoff
            )
            or (item.retention_until and item.retention_until <= expiring_cutoff)
        )

    observed = [item.evidence.observed_at for item in evidence]
    return {
        "id": str(person.id),
        "display_name": person.display_name,
        "current_title": person.current_title,
        "current_company": person.current_company,
        "governance_status": person.governance_status,
        "governance_revision": person.governance_revision,
        "retention_until": person.retention_until.isoformat() if person.retention_until else None,
        "deletion_requested_at": person.deletion_requested_at.isoformat() if person.deletion_requested_at else None,
        "anonymized_at": person.anonymized_at.isoformat() if person.anonymized_at else None,
        "evidence_summary": {
            "total": len(evidence),
            "confirmed": sum(
                item.confirmation_status == EvidenceConfirmationStatus.CONFIRMED
                for item in evidence
            ),
            "pending": sum(
                item.confirmation_status == EvidenceConfirmationStatus.PENDING
                for item in evidence
            ),
            "rejected": sum(
                item.confirmation_status == EvidenceConfirmationStatus.REJECTED
                for item in evidence
            ),
            "restricted": sum(
                item.processing_status != EvidenceProcessingStatus.ACTIVE
                for item in evidence
            ),
            "expiring": sum(is_expiring(item) for item in evidence),
            "expired": sum(is_expired(item) for item in evidence),
            "last_observed_at": max(observed).isoformat() if observed else None,
        },
        "contact_intents": [safe_intent(item) for item in intents],
    }


def governance_revision_for_org(org) -> int:
    """Monotonic aggregate used as the retention-scan compare-and-swap token."""

    person_total = Person.objects.filter(org=org).aggregate(
        total=Sum("governance_revision")
    )["total"] or 0
    evidence_total = EvidenceProvenance.objects.filter(org=org).aggregate(
        total=Sum("revision")
    )["total"] or 0
    return int(person_total) + int(evidence_total)


def contact_eligibility(
    *, org, person_id, identity_id, channel, purpose, idempotency_key: UUID, expected_revision: int
) -> dict:
    person = Person.objects.filter(org=org, id=person_id).first()
    if person is None:
        raise GovernanceError(code="person_not_found", detail="Person was not found.", status_code=404)
    if person.governance_revision != expected_revision:
        raise GovernanceError(code="person_revision_conflict", detail="Person governance revision is stale.", status_code=409)
    if person.governance_status != PersonGovernanceStatus.ACTIVE:
        return {"allowed": False, "code": f"person_{person.governance_status}", "intent": "unknown"}
    identity = PersonIdentity.objects.filter(org=org, person=person, id=identity_id).first()
    if identity is None:
        raise GovernanceError(code="identity_not_found", detail="Identity was not found.", status_code=404)
    now = timezone.now()
    intents = list(PersonContactIntent.objects.filter(
        org=org,
        person=person,
        channel=channel,
        purpose__in=[purpose, PersonContactIntentPurpose.GENERAL_CONTACT],
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=now)))
    intent_priority = {
        PersonContactIntentState.OBJECTED: 0,
        PersonContactIntentState.WITHDRAWN: 1,
        PersonContactIntentState.NOT_OPEN: 2,
        PersonContactIntentState.CONDITIONAL: 3,
        PersonContactIntentState.OPEN: 4,
        PersonContactIntentState.UNKNOWN: 5,
    }
    intents.sort(
        key=lambda item: (
            intent_priority.get(item.state, 99),
            0 if item.purpose == purpose else 1,
        )
    )
    intent = intents[0] if intents else None
    if intent and intent.state in {
        PersonContactIntentState.NOT_OPEN,
        PersonContactIntentState.WITHDRAWN,
        PersonContactIntentState.OBJECTED,
    }:
        return {"allowed": False, "code": f"intent_{intent.state}", "intent": intent.state}
    provenance = None
    for candidate in EvidenceProvenance.objects.filter(
        org=org,
        evidence__person=person,
        processing_status=EvidenceProcessingStatus.ACTIVE,
        confirmation_status=EvidenceConfirmationStatus.CONFIRMED,
    ).filter(Q(retention_until__isnull=True) | Q(retention_until__gte=now)).order_by("-evidence__observed_at"):
        if channel in (candidate.allowed_channels or []) and purpose in (candidate.allowed_purposes or []):
            provenance = candidate
            break
    governance = {
        "lawful_basis": provenance.lawful_basis if provenance else "unassessed",
        "notes": provenance.lawful_basis_notes if provenance else "",
        "consent": {
            "granted": bool(provenance and provenance.lawful_basis == "consent"),
            "recorded_at": provenance.consent_at if provenance else None,
            "evidence": provenance.consent_evidence_ref if provenance else "",
        },
        "country": provenance.country_code if provenance else "",
        "allowed_channels": provenance.allowed_channels if provenance else default_governance_channels(),
        "processing_status": provenance.processing_status if provenance else "active",
    }
    from sdr.compliance import evaluate_contact

    decision = evaluate_contact(
        org_id=org.id,
        channel=channel,
        identifier=identity.normalized_value,
        country_code=governance["country"],
        governance=governance,
        event_key=f"matching:eligibility:{idempotency_key}",
    )
    return {
        "allowed": decision.allowed,
        "code": decision.code,
        "reason": decision.reason,
        "intent": intent.state if intent else "unknown",
        "channel": channel,
        "purpose": purpose,
    }


def _existing_person_event(*, org, idempotency_key, request_hash, person_id):
    event = PersonGovernanceEvent.objects.filter(
        org=org,
        idempotency_key=idempotency_key,
    ).select_related("person").first()
    if event is None:
        return None
    if event.request_hash != request_hash or event.person_id != person_id:
        raise GovernanceError(code="person_governance_idempotency_conflict", detail="Idempotency-Key was already used with a different request.", status_code=409)
    return event


@transaction.atomic
def export_person(*, org, person_id, actor, idempotency_key: UUID, expected_revision: int) -> GovernanceMutationResult:
    request_hash = _request_hash({"action": "export", "person_id": person_id, "expected_revision": expected_revision})
    replay = _existing_person_event(org=org, idempotency_key=idempotency_key, request_hash=request_hash, person_id=person_id)
    if replay:
        return GovernanceMutationResult(safe_person_export(replay.person), replay, True)
    person = Person.objects.select_for_update().filter(org=org, id=person_id).first()
    if person is None:
        raise GovernanceError(code="person_not_found", detail="Person was not found.", status_code=404)
    if person.governance_revision != expected_revision:
        raise GovernanceError(code="person_revision_conflict", detail="Person governance revision is stale.", status_code=409)
    payload = safe_person_export(person)
    payload_hash = _request_hash(payload)
    event = PersonGovernanceEvent.objects.create(
        org=org,
        person=person,
        event_type=PersonGovernanceEventType.EXPORT_REQUESTED,
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        expected_revision=expected_revision,
        resulting_revision=expected_revision,
        safe_snapshot={"payload_sha256": payload_hash, "evidence_count": len(payload["evidence"]), "intent_count": len(payload["contact_intents"])},
    )
    return GovernanceMutationResult(payload, event, False)


def safe_person_export(person: Person) -> dict:
    provenances = EvidenceProvenance.objects.filter(org=person.org, evidence__person=person).select_related("evidence")
    intents = PersonContactIntent.objects.filter(org=person.org, person=person).select_related("identity")
    return {
        "schema_version": 1,
        "person": {
            "id": str(person.id),
            "display_name": person.display_name,
            "headline": person.headline,
            "current_title": person.current_title,
            "current_company": person.current_company,
            "location": person.location,
            "availability": person.availability,
            "governance_status": person.governance_status,
            "governance_revision": person.governance_revision,
        },
        "identities": [
            {"id": str(item.id), "kind": item.kind, "masked_value": _mask_identity(item), "verified": bool(item.verified_at)}
            for item in PersonIdentity.objects.filter(org=person.org, person=person)
        ],
        "evidence": [
            {"id": str(item.evidence_id), "kind": item.evidence.kind, "source": item.evidence.source, "summary": item.evidence.summary, "observed_at": item.evidence.observed_at.isoformat(), "valid_until": item.evidence.valid_until.isoformat() if item.evidence.valid_until else None, "governance": safe_provenance(item)}
            for item in provenances
        ],
        "contact_intents": [safe_intent(item) for item in intents],
    }


def _anonymize_person_graph(
    *,
    person: Person,
    actor,
    event_seed: UUID,
    prelocked_import_record_ids: tuple[UUID, ...] = (),
):
    # A record-processing worker locks its import record before the Person. The
    # caller prelocks all records that were already linked before locking the
    # Person, and this second pass catches a worker that committed while the
    # caller was waiting for the Person lock. No new worker can link this Person
    # until the surrounding transaction releases it.
    current_import_record_ids = tuple(
        PersonImportRecord.objects.select_for_update()
        .filter(org=person.org, person=person)
        .order_by("id")
        .values_list("id", flat=True)
    )
    import_record_ids = tuple(
        dict.fromkeys([*prelocked_import_record_ids, *current_import_record_ids])
    )
    if import_record_ids:
        PersonImportRecord.objects.filter(
            org=person.org,
            person=person,
            id__in=import_record_ids,
        ).update(
            display_name="",
            normalized_payload={},
            masked_identities=[],
            field_errors=[],
            updated_at=timezone.now(),
        )

    # Identity observations and intent history intentionally PROTECT identities.
    # Keep the durable graph intact while replacing all addressable values with
    # non-reversible, per-row markers that cannot collide inside the tenant.
    for identity in PersonIdentity.objects.filter(org=person.org, person=person):
        marker = f"anonymized:{identity.id}"
        identity.normalized_value = marker
        identity.display_value = "Anonymized"
        identity.is_primary = False
        identity.verified_at = None
        identity.save(
            update_fields=[
                "normalized_value",
                "display_value",
                "is_primary",
                "verified_at",
                "updated_at",
            ]
        )
    for evidence in Evidence.objects.select_for_update().filter(
        org=person.org,
        person=person,
    ):
        evidence.summary = "Anonymized"
        evidence.facts = {}
        evidence.source_uri = ""
        evidence.source_record_id = ""
        evidence.save(update_fields=["summary", "facts", "source_uri", "source_record_id", "content_hash", "updated_at"])
        provenance = EvidenceProvenance.objects.select_for_update().filter(
            org=person.org,
            evidence=evidence,
        ).first()
        if provenance is not None:
            expected = provenance.revision
            provenance.lawful_basis_notes = ""
            provenance.consent_at = None
            provenance.consent_evidence_ref = ""
            provenance.country_code = ""
            provenance.allowed_channels = []
            provenance.allowed_purposes = []
            provenance.source_content_sha256 = ""
            provenance.processing_status = EvidenceProcessingStatus.ANONYMIZED
            provenance.revision += 1
            provenance.save()
            EvidenceGovernanceEvent.objects.create(
                org=person.org,
                provenance=provenance,
                evidence=evidence,
                action=EvidenceGovernanceAction.ANONYMIZED,
                actor=actor,
                expected_revision=expected,
                resulting_revision=provenance.revision,
                idempotency_key=uuid5(
                    GOVERNANCE_NAMESPACE,
                    f"person-anonymize-evidence:{event_seed}:{evidence.id}",
                ),
                request_hash=_request_hash(
                    {"action": "anonymize", "evidence_id": evidence.id}
                ),
                safe_snapshot=_safe_provenance_snapshot(provenance),
            )
    for intent in PersonContactIntent.objects.select_for_update().filter(
        org=person.org,
        person=person,
    ):
        expected = intent.revision
        from_state = intent.state
        intent.identity = None
        intent.evidence = None
        intent.state = PersonContactIntentState.WITHDRAWN
        intent.source = EvidenceSource.MANUAL
        intent.revision += 1
        intent.save()
        PersonContactIntentEvent.objects.create(
            org=person.org,
            intent=intent,
            evidence=None,
            actor=actor,
            from_state=from_state,
            to_state=PersonContactIntentState.WITHDRAWN,
            source=EvidenceSource.MANUAL,
            confirmation_status=EvidenceConfirmationStatus.CONFIRMED,
            expected_revision=expected,
            resulting_revision=intent.revision,
            idempotency_key=uuid5(
                GOVERNANCE_NAMESPACE,
                f"person-anonymize-intent:{event_seed}:{intent.id}",
            ),
            request_hash=_request_hash(
                {"action": "anonymize", "intent_id": intent.id}
            ),
            safe_snapshot={
                "channel": intent.channel,
                "purpose": intent.purpose,
                "state": intent.state,
                "confirmation_status": EvidenceConfirmationStatus.CONFIRMED,
            },
        )
    person.display_name = "Anonymized"
    for field in ("first_name", "last_name", "headline", "summary", "current_title", "current_company", "location", "timezone"):
        setattr(person, field, "")
    person.skills = []
    person.roles = []
    person.attributes = {}
    person.status = "archived"


@transaction.atomic
def mutate_person_governance(
    *, org, person_id, actor, idempotency_key: UUID, expected_revision: int, action: str
) -> GovernanceMutationResult:
    if action not in {"request", "cancel", "anonymize"}:
        raise GovernanceError(code="invalid_person_governance_action", detail="action must be request, cancel, or anonymize.")
    request_hash = _request_hash({"action": action, "person_id": person_id, "expected_revision": expected_revision})
    replay = _existing_person_event(org=org, idempotency_key=idempotency_key, request_hash=request_hash, person_id=person_id)
    if replay:
        return GovernanceMutationResult(replay.person, replay, True)
    # Projection mutations use the organization row as their first lock. Take
    # it before anonymization locks import records or the target Person so a
    # concurrent recompute cannot form Person/Opportunity/FK lock cycles.
    lock_matching_org(org.id)
    prelocked_import_record_ids: tuple[UUID, ...] = ()
    if action == "anonymize":
        prelocked_import_record_ids = tuple(
            PersonImportRecord.objects.select_for_update()
            .filter(org=org, person_id=person_id)
            .order_by("id")
            .values_list("id", flat=True)
        )
    person = Person.objects.select_for_update().filter(org=org, id=person_id).first()
    if person is None:
        raise GovernanceError(code="person_not_found", detail="Person was not found.", status_code=404)
    if person.governance_revision != expected_revision:
        raise GovernanceError(code="person_revision_conflict", detail="Person governance revision is stale.", status_code=409)
    now = timezone.now()
    if action == "request":
        person.governance_status = PersonGovernanceStatus.DELETION_REQUESTED
        person.deletion_requested_at = now
        event_type = PersonGovernanceEventType.DELETION_REQUESTED
    elif action == "cancel":
        if person.governance_status != PersonGovernanceStatus.DELETION_REQUESTED:
            raise GovernanceError(code="deletion_not_requested", detail="The person has no pending deletion request.", status_code=409)
        person.governance_status = PersonGovernanceStatus.ACTIVE
        person.deletion_requested_at = None
        event_type = PersonGovernanceEventType.DELETION_CANCELLED
    else:
        _anonymize_person_graph(
            person=person,
            actor=actor,
            event_seed=idempotency_key,
            prelocked_import_record_ids=prelocked_import_record_ids,
        )
        person.governance_status = PersonGovernanceStatus.ANONYMIZED
        person.anonymized_at = now
        event_type = PersonGovernanceEventType.ANONYMIZED
    person.governance_revision += 1
    person.save()
    event = PersonGovernanceEvent.objects.create(
        org=org,
        person=person,
        event_type=event_type,
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        expected_revision=expected_revision,
        resulting_revision=person.governance_revision,
        safe_snapshot={"governance_status": person.governance_status, "governance_revision": person.governance_revision},
    )
    if action in {"request", "anonymize"}:
        _enqueue_person_recompute(person=person, key_seed=f"person-governance:{event.id}")
    return GovernanceMutationResult(person, event, False)


def scan_governance_retention(*, org, execute: bool, limit: int = 200, actor=None) -> dict:
    now = timezone.now()
    due_evidence = list(
        EvidenceProvenance.objects.filter(org=org, expiry_processed_at__isnull=True)
        .filter(Q(evidence__valid_until__lte=now) | Q(retention_until__lte=now))
        .select_related("evidence__person")[:limit]
    )
    due_people = list(
        Person.objects.filter(
            org=org,
            retention_until__lte=now,
        ).exclude(governance_status=PersonGovernanceStatus.ANONYMIZED)[:limit]
    )
    processed_evidence = 0
    processed_people = 0
    restricted = 0
    expired = 0
    recomputed = 0
    if execute:
        for item in due_evidence:
            with transaction.atomic():
                current = EvidenceProvenance.objects.select_for_update().get(org=org, id=item.id)
                if current.expiry_processed_at is not None:
                    continue
                expected = current.revision
                retention_due = bool(current.retention_until and current.retention_until <= now)
                if retention_due:
                    current.processing_status = EvidenceProcessingStatus.RESTRICTED
                    restricted += 1
                else:
                    expired += 1
                current.expiry_processed_at = now
                current.revision += 1
                current.save(update_fields=["processing_status", "expiry_processed_at", "revision", "updated_at"])
                event = EvidenceGovernanceEvent.objects.create(
                    org=org,
                    provenance=current,
                    evidence=current.evidence,
                    action=(EvidenceGovernanceAction.RESTRICTED if retention_due else EvidenceGovernanceAction.EXPIRED),
                    actor=actor,
                    expected_revision=expected,
                    resulting_revision=current.revision,
                    idempotency_key=uuid5(GOVERNANCE_NAMESPACE, f"evidence-expiry:{current.id}:{current.evidence.valid_until}:{current.retention_until}"),
                    request_hash=_request_hash({"evidence_id": current.evidence_id, "valid_until": current.evidence.valid_until, "retention_until": current.retention_until}),
                    safe_snapshot=_safe_provenance_snapshot(current),
                )
                recomputed += len(
                    _enqueue_person_recompute(
                        person=current.evidence.person,
                        key_seed=f"evidence-expiry:{event.id}",
                    )
                )
                processed_evidence += 1
        for person in due_people:
            try:
                mutate_person_governance(
                    org=org,
                    person_id=person.id,
                    actor=actor,
                    idempotency_key=uuid5(GOVERNANCE_NAMESPACE, f"person-retention:{person.id}:{person.retention_until}"),
                    expected_revision=person.governance_revision,
                    action="anonymize",
                )
            except GovernanceError as exc:
                if exc.code != "person_revision_conflict":
                    raise
            else:
                processed_people += 1
    return {
        "execute": execute,
        "due": len(due_evidence) + len(due_people),
        "restricted": restricted,
        "anonymized": processed_people,
        "expired": expired,
        "recomputed": recomputed,
    }
