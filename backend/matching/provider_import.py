"""Trusted, structured provider ingestion into the durable Person import ledger.

This module is intentionally an internal service boundary.  Callers choose a
server-owned source and namespace; neither value is accepted from public API
payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction

from matching.governance import reject_raw_content_keys
from matching.import_pipeline import (
    IMPORT_SCHEMA_VERSION,
    MAX_IMPORT_ROWS,
    PersonImportPreviewResult,
    PersonImportServiceError,
    _canonical_json,
    _existing_preview,
    _json_safe,
    _mask_identity,
    _sha256,
)
from matching.models import (
    EvidenceKind,
    EvidenceSource,
    PersonIdentityKind,
    PersonImportBatch,
    PersonImportRecord,
    PersonImportRecordStatus,
)
from matching.serializers import PersonOnboardingRequestSerializer

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_ALLOWED_RECORD_KEYS = {
    "source_record_id",
    "display_name",
    "first_name",
    "last_name",
    "current_title",
    "current_company",
    "location",
    "email",
    "phone",
    "linkedin",
    "evidence_kind",
    "evidence_summary",
    "observed_at",
}


@dataclass(frozen=True)
class ProviderPersonRecord:
    source_record_id: str
    display_name: str
    first_name: str = ""
    last_name: str = ""
    current_title: str = ""
    current_company: str = ""
    location: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    evidence_kind: str = EvidenceKind.PROFILE
    evidence_summary: str = "Imported provider profile"
    observed_at: datetime | None = None


def _service_error(code: str, detail: str) -> PersonImportServiceError:
    return PersonImportServiceError(code=code, detail=detail)


def _coerce_record(value: ProviderPersonRecord | dict) -> ProviderPersonRecord:
    if isinstance(value, ProviderPersonRecord):
        return value
    if not isinstance(value, dict):
        raise _service_error("invalid_provider_record", "Provider records must be objects.")
    reject_raw_content_keys(value)
    unknown = set(value) - _ALLOWED_RECORD_KEYS
    if unknown:
        raise _service_error(
            "invalid_provider_record",
            f"Unsupported provider record field(s): {', '.join(sorted(unknown))}.",
        )
    try:
        return ProviderPersonRecord(**value)
    except TypeError as exc:
        raise _service_error("invalid_provider_record", "Provider record is incomplete.") from exc


def _identity(kind: str, value: str) -> dict:
    return {
        "kind": kind,
        "normalized_value": value,
        "display_value": value,
        # The existing bounded onboarding validator deliberately accepts manual
        # only. Persistence replaces this with the trusted batch source.
        "source": EvidenceSource.MANUAL,
    }


def _normalize_record(record: ProviderPersonRecord) -> tuple[dict, str, list[dict]]:
    person = {
        key: value
        for key, value in {
            "display_name": record.display_name,
            "first_name": record.first_name,
            "last_name": record.last_name,
            "current_title": record.current_title,
            "current_company": record.current_company,
            "location": record.location,
        }.items()
        if value
    }
    identities = []
    if record.email:
        identities.append(_identity(PersonIdentityKind.EMAIL, record.email))
    if record.phone:
        identities.append(_identity(PersonIdentityKind.PHONE, record.phone))
    if record.linkedin:
        identities.append(_identity(PersonIdentityKind.LINKEDIN, record.linkedin))
    if not identities:
        raise _service_error(
            "identity_required",
            "A trusted provider record must contain at least one stable identity.",
        )

    facts = {}
    if record.current_title:
        facts["titles"] = [record.current_title]
    if record.location:
        facts["locations"] = [record.location]
    evidence = {
        "kind": record.evidence_kind,
        "source": EvidenceSource.MANUAL,
        "source_record_id": record.source_record_id,
        "summary": record.evidence_summary,
        "facts": facts,
        "confidence": Decimal("0.500"),
    }
    if record.observed_at is not None:
        evidence["observed_at"] = record.observed_at
    serializer = PersonOnboardingRequestSerializer(
        data={"person": person, "identities": identities, "evidence": [evidence]}
    )
    serializer.is_valid(raise_exception=True)
    payload = _json_safe(serializer.validated_data)
    # The trusted source is stored once on the batch. Do not persist a
    # client-shaped/manual source marker inside individual normalized rows.
    for identity in payload["identities"]:
        identity.pop("source", None)
    for item in payload["evidence"]:
        item.pop("source", None)
    # source_record_id is ledger identity, not semantic content. Excluding it
    # preserves replay when an upstream system renames an otherwise identical row.
    semantic = _json_safe(payload)
    semantic["evidence"][0].pop("source_record_id", None)
    row_hash = _sha256(_canonical_json(semantic))
    masked = [
        {
            "kind": item["kind"],
            "masked_value": _mask_identity(item["kind"], item["normalized_value"]),
        }
        for item in payload["identities"]
    ]
    return payload, row_hash, masked


def preview_provider_person_import(
    *,
    org,
    requested_by,
    idempotency_key: UUID,
    source: str,
    source_namespace: str,
    records: list[ProviderPersonRecord | dict],
) -> PersonImportPreviewResult:
    """Persist a bounded trusted-provider preview using the canonical ledger."""

    if source not in EvidenceSource.values or source in {EvidenceSource.MANUAL, EvidenceSource.AI}:
        raise _service_error("invalid_provider_source", "Unsupported trusted provider source.")
    if not _NAMESPACE_RE.fullmatch(str(source_namespace or "")):
        raise _service_error("invalid_source_namespace", "Provider namespace is invalid.")
    if not isinstance(records, list) or len(records) > MAX_IMPORT_ROWS:
        raise _service_error(
            "invalid_provider_records",
            f"Provider import must contain between 0 and {MAX_IMPORT_ROWS} records.",
        )
    if requested_by is not None and requested_by.org_id != org.id:
        raise _service_error("import_actor_org_conflict", "Requester belongs to another organization.")

    normalized = []
    for row_number, raw in enumerate(records, start=1):
        record = _coerce_record(raw)
        errors = []
        payload = {}
        row_hash = _sha256(_canonical_json({"row_number": row_number}))
        masked = []
        try:
            if not str(record.source_record_id or "").strip() or len(str(record.source_record_id)) > 255:
                raise _service_error("invalid_source_record_id", "A bounded source_record_id is required.")
            payload, row_hash, masked = _normalize_record(record)
        except PersonImportServiceError as exc:
            errors.append({"field": "row", "code": exc.code, "detail": exc.detail})
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            errors.append(
                {
                    "field": "row",
                    "code": "invalid_row",
                    "detail": _json_safe(detail) if detail is not None else "Row validation failed.",
                }
            )
        normalized.append((record, payload, row_hash, masked, errors))

    request_document = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "source": source,
        "source_namespace": source_namespace,
        "records": [
            {"source_record_id": str(item[0].source_record_id), "row_hash": item[2]}
            for item in normalized
        ],
    }
    content_hash = _sha256(_canonical_json(request_document["records"]))
    request_hash = _sha256(_canonical_json(request_document))
    replay = _existing_preview(
        org=org, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay is not None:
        return replay

    ready_count = sum(not item[4] for item in normalized)
    try:
        with transaction.atomic():
            batch = PersonImportBatch.objects.create(
                org=org,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                content_hash=content_hash,
                original_filename=f"{source_namespace}.json"[:255],
                file_size=0,
                mapping={},
                headers=[],
                source=source,
                source_namespace=source_namespace,
                total_count=len(normalized),
                ready_count=ready_count,
                invalid_count=len(normalized) - ready_count,
            )
            PersonImportRecord.objects.bulk_create(
                [
                    PersonImportRecord(
                        org=org,
                        batch=batch,
                        row_number=index,
                        row_hash=row_hash,
                        source_record_id=str(record.source_record_id)[:255],
                        display_name=str(record.display_name or "")[:255],
                        normalized_payload=payload,
                        masked_identities=masked,
                        field_errors=errors,
                        status=(
                            PersonImportRecordStatus.INVALID
                            if errors
                            else PersonImportRecordStatus.READY
                        ),
                    )
                    for index, (record, payload, row_hash, masked, errors) in enumerate(
                        normalized, start=1
                    )
                ]
            )
    except IntegrityError:
        replay = _existing_preview(
            org=org, idempotency_key=idempotency_key, request_hash=request_hash
        )
        if replay is not None:
            return replay
        raise
    return PersonImportPreviewResult(batch=batch, replayed=False)
