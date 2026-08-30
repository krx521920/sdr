"""Atomic, idempotent creation of a person and their initial evidence graph."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q

from matching.governance import ensure_evidence_provenance
from matching.models import Evidence, EvidenceSource, Person, PersonIdentity


class PersonOnboardingConflict(Exception):
    def __init__(self, *, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class PersonOnboardingResult:
    person: Person
    identity_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    replayed: bool

    def as_response(self):
        return {
            "person_id": self.person.id,
            "identity_ids": self.identity_ids,
            "evidence_ids": self.evidence_ids,
            "replayed": self.replayed,
        }


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Unsupported onboarding value: {type(value).__name__}")


def _canonical_payload(validated_data):
    person = dict(validated_data["person"])
    for field in ("skills", "roles"):
        if field in person:
            person[field] = sorted(person[field], key=str.casefold)

    identities = [dict(item) for item in validated_data.get("identities", [])]
    evidence = [dict(item) for item in validated_data["evidence"]]

    def canonical_json(value):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        )

    return {
        "person": person,
        "identities": sorted(identities, key=canonical_json),
        "evidence": sorted(evidence, key=canonical_json),
    }


def onboarding_request_hash(validated_data) -> str:
    canonical = json.dumps(
        _canonical_payload(validated_data),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_for_person(person, *, replayed):
    return PersonOnboardingResult(
        person=person,
        identity_ids=tuple(UUID(value) for value in person.onboarding_identity_ids),
        evidence_ids=tuple(UUID(value) for value in person.onboarding_evidence_ids),
        replayed=replayed,
    )


def _replay_or_conflict(*, org, idempotency_key, request_hash):
    person = Person.objects.filter(
        org=org,
        onboarding_idempotency_key=idempotency_key,
    ).first()
    if person is None:
        return None
    if person.onboarding_request_hash != request_hash:
        raise PersonOnboardingConflict(
            code="onboarding_idempotency_conflict",
            detail="Idempotency-Key was already used with a different payload.",
        )
    return _result_for_person(person, replayed=True)


def _identity_collision_exists(*, org, identities):
    identity_filter = Q()
    for identity in identities:
        identity_filter |= Q(
            kind=identity["kind"],
            normalized_value=identity["normalized_value"],
        )
    return (
        bool(identity_filter)
        and PersonIdentity.objects.filter(
            identity_filter,
            org=org,
        ).exists()
    )


def onboard_person(
    *,
    org,
    requested_by,
    idempotency_key: UUID,
    validated_data: dict,
) -> PersonOnboardingResult:
    """Create the complete onboarding graph, or replay the prior result."""

    if requested_by is not None and requested_by.org_id != org.id:
        raise PersonOnboardingConflict(
            code="onboarding_actor_org_conflict",
            detail="Requester belongs to another organization.",
        )

    request_hash = onboarding_request_hash(validated_data)
    replay = _replay_or_conflict(
        org=org,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    identities_data = [dict(item) for item in validated_data.get("identities", [])]
    evidence_data = [dict(item) for item in validated_data["evidence"]]
    if _identity_collision_exists(org=org, identities=identities_data):
        raise PersonOnboardingConflict(
            code="identity_conflict",
            detail="An identity already belongs to a person in this organization.",
        )

    try:
        with transaction.atomic():
            person = Person.objects.create(
                org=org,
                onboarding_idempotency_key=idempotency_key,
                onboarding_request_hash=request_hash,
                **validated_data["person"],
            )
            identity_ids = []
            for identity_data in identities_data:
                identity_data.pop("source", None)
                identity = PersonIdentity.objects.create(
                    org=org,
                    person=person,
                    source=EvidenceSource.MANUAL,
                    **identity_data,
                )
                identity_ids.append(identity.id)
            evidence_ids = []
            for item_data in evidence_data:
                item_data.pop("source", None)
                item = Evidence.objects.create(
                    org=org,
                    person=person,
                    source=EvidenceSource.MANUAL,
                    **item_data,
                )
                ensure_evidence_provenance(
                    evidence=item,
                    actor=requested_by,
                    collection_method="manual",
                )
                evidence_ids.append(item.id)
            person.onboarding_identity_ids = [str(value) for value in identity_ids]
            person.onboarding_evidence_ids = [str(value) for value in evidence_ids]
            person.save(
                update_fields=[
                    "onboarding_identity_ids",
                    "onboarding_evidence_ids",
                    "updated_at",
                ]
            )
    except IntegrityError:
        replay = _replay_or_conflict(
            org=org,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        if _identity_collision_exists(org=org, identities=identities_data):
            raise PersonOnboardingConflict(
                code="identity_conflict",
                detail=(
                    "An identity already belongs to a person in this organization."
                ),
            ) from None
        raise

    return _result_for_person(person, replayed=False)
