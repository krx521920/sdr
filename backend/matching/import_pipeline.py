"""Bounded, durable CSV ingestion for the unified Person graph."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from uuid import UUID, uuid5

from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError

from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from matching.governance import ensure_evidence_provenance
from matching.locking import lock_matching_org
from matching.models import (
    Evidence,
    EvidenceCollectionMethod,
    EvidenceKind,
    EvidenceSource,
    MatchOpportunity,
    MatchOpportunityStatus,
    Person,
    PersonGovernanceStatus,
    PersonIdentity,
    PersonIdentityKind,
    PersonIdentityObservation,
    PersonImportBatch,
    PersonImportBatchStatus,
    PersonImportConflict,
    PersonImportConflictStatus,
    PersonImportDecision,
    PersonImportDecisionAction,
    PersonImportImpact,
    PersonImportImpactType,
    PersonImportRecord,
    PersonImportRecordStatus,
)
from matching.serializers import PersonOnboardingRequestSerializer
from matching.services import RecomputeEnqueueError, enqueue_opportunity_recompute

logger = logging.getLogger(__name__)

IMPORT_JOB_NAME = "matching.import_people"
IMPORT_SCHEMA_VERSION = 1
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 500
MAX_IMPORT_COLUMNS = 100
SOURCE_NAMESPACE = "manual:csv"
SUCCESSFUL_RECORD_STATUSES = (
    PersonImportRecordStatus.CREATED,
    PersonImportRecordStatus.MERGED,
    PersonImportRecordStatus.REPLAYED,
)
STAGING_PAYLOAD_TERMINAL_STATUSES = frozenset(
    {
        PersonImportRecordStatus.CREATED,
        PersonImportRecordStatus.MERGED,
        PersonImportRecordStatus.REPLAYED,
        PersonImportRecordStatus.SKIPPED,
        PersonImportRecordStatus.FAILED,
    }
)
MAX_PREVIEW_EXPIRY_BATCHES = 500

IDENTITY_TARGETS = {
    "email": PersonIdentityKind.EMAIL,
    "phone": PersonIdentityKind.PHONE,
    "linkedin": PersonIdentityKind.LINKEDIN,
    "whatsapp": PersonIdentityKind.WHATSAPP,
    "wechat": PersonIdentityKind.WECHAT,
    "external_id": PersonIdentityKind.EXTERNAL,
}
PERSON_TARGETS = {
    "display_name",
    "first_name",
    "last_name",
    "headline",
    "summary",
    "current_title",
    "current_company",
    "location",
    "timezone",
    "availability",
    "skills",
    "roles",
}
EVIDENCE_TARGETS = {
    "source_record_id",
    "evidence_summary",
    "evidence_kind",
    "confidence",
    "observed_at",
    "source_uri",
}
ALLOWED_MAPPING_TARGETS = PERSON_TARGETS | set(IDENTITY_TARGETS) | EVIDENCE_TARGETS


class PersonImportServiceError(ValueError):
    def __init__(self, *, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class PersonImportPreviewResult:
    batch: PersonImportBatch
    replayed: bool


@dataclass(frozen=True)
class PersonImportCommitResult:
    batch: PersonImportBatch
    replayed: bool


@dataclass(frozen=True)
class PersonImportResolutionResult:
    record: PersonImportRecord
    decision: PersonImportDecision
    replayed: bool


@dataclass(frozen=True)
class ParsedImportRecord:
    row_number: int
    row_hash: str
    source_record_id: str
    display_name: str
    normalized_payload: dict
    masked_identities: list[dict]
    field_errors: list[dict]

    @property
    def status(self):
        return (
            PersonImportRecordStatus.INVALID
            if self.field_errors
            else PersonImportRecordStatus.READY
        )


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _json_safe(value):
    return json.loads(_canonical_json(value))


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _clean_filename(filename: str) -> str:
    filename = str(filename or "import.csv").replace("\\", "/").split("/")[-1]
    return PurePath(filename).name[:255] or "import.csv"


def validate_mapping(mapping) -> dict[str, str]:
    if not isinstance(mapping, dict):
        raise PersonImportServiceError(
            code="invalid_mapping",
            detail="mapping must be a JSON object of target fields to CSV headers.",
        )
    unknown = sorted(set(mapping) - ALLOWED_MAPPING_TARGETS)
    if unknown:
        raise PersonImportServiceError(
            code="invalid_mapping",
            detail=f"Unsupported mapping target(s): {', '.join(unknown)}.",
        )
    cleaned = {}
    for target, header in mapping.items():
        if not isinstance(header, str) or not header.strip() or len(header.strip()) > 255:
            raise PersonImportServiceError(
                code="invalid_mapping",
                detail=f"Mapping for {target} must name a non-empty CSV header.",
            )
        cleaned[target] = header.strip()
    if "display_name" not in cleaned:
        raise PersonImportServiceError(
            code="invalid_mapping",
            detail="display_name must be mapped.",
        )
    if not set(IDENTITY_TARGETS).intersection(cleaned):
        raise PersonImportServiceError(
            code="invalid_mapping",
            detail="At least one identity field must be mapped.",
        )
    duplicate_headers = {
        header for header in cleaned.values() if list(cleaned.values()).count(header) > 1
    }
    if duplicate_headers:
        raise PersonImportServiceError(
            code="invalid_mapping",
            detail="One CSV header cannot be mapped to multiple target fields.",
        )
    return dict(sorted(cleaned.items()))


def _decode_csv(file_bytes: bytes) -> str:
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise PersonImportServiceError(
            code="file_too_large",
            detail="CSV file exceeds the 5 MB upload limit.",
        )
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PersonImportServiceError(
            code="invalid_encoding",
            detail="CSV must be encoded as UTF-8.",
        ) from exc


def _mask_identity(kind: str, value: str) -> str:
    if kind == PersonIdentityKind.EMAIL and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:2]}***@{domain}"
    if kind in {PersonIdentityKind.PHONE, PersonIdentityKind.WHATSAPP}:
        return f"***{value[-4:]}"
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def _field(record: dict[str, str], mapping: dict[str, str], target: str) -> str:
    header = mapping.get(target)
    return (record.get(header, "") if header else "").strip()


def _split_values(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(";") if item.strip()))


def _build_onboarding_payload(
    *, record: dict[str, str], mapping: dict[str, str]
) -> tuple[dict, str]:
    person = {}
    for target in PERSON_TARGETS:
        value = _field(record, mapping, target)
        if not value:
            continue
        person[target] = _split_values(value) if target in {"skills", "roles"} else value

    identities = []
    for target, kind in IDENTITY_TARGETS.items():
        value = _field(record, mapping, target)
        if value:
            identities.append(
                {
                    "kind": kind,
                    "normalized_value": value,
                    "display_value": value,
                    "source": EvidenceSource.MANUAL,
                }
            )
    if not identities:
        raise PersonImportServiceError(
            code="identity_required",
            detail="Each non-empty row must contain at least one mapped identity.",
        )

    facts = {}
    if person.get("skills"):
        facts["skills"] = person["skills"]
    titles = [item for item in [person.get("current_title"), *person.get("roles", [])] if item]
    if titles:
        facts["titles"] = list(dict.fromkeys(titles))
    if person.get("location"):
        facts["locations"] = [person["location"]]
    if person.get("availability"):
        facts["availability"] = [person["availability"]]

    evidence = {
        "kind": _field(record, mapping, "evidence_kind") or EvidenceKind.PROFILE,
        "source": EvidenceSource.MANUAL,
        "summary": _field(record, mapping, "evidence_summary")
        or f"Imported profile for {person.get('display_name', '')}",
        "facts": facts,
    }
    for target in ("confidence", "observed_at", "source_uri"):
        value = _field(record, mapping, target)
        if value:
            evidence[target] = value

    base_payload = {"person": person, "identities": identities, "evidence": [evidence]}
    serializer = PersonOnboardingRequestSerializer(data=base_payload)
    serializer.is_valid(raise_exception=True)
    normalized_base = _json_safe(serializer.validated_data)
    row_hash = _sha256(_canonical_json(normalized_base))
    evidence["source_record_id"] = (
        _field(record, mapping, "source_record_id") or row_hash
    )
    serializer = PersonOnboardingRequestSerializer(data=base_payload)
    serializer.is_valid(raise_exception=True)
    return _json_safe(serializer.validated_data), row_hash


def parse_person_csv(*, file_bytes: bytes, mapping: dict[str, str]) -> list[ParsedImportRecord]:
    mapping = validate_mapping(mapping)
    text = _decode_csv(file_bytes)
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        raw_headers = next(reader)
    except StopIteration as exc:
        raise PersonImportServiceError(code="empty_csv", detail="CSV is empty.") from exc
    except csv.Error as exc:
        raise PersonImportServiceError(
            code="invalid_csv",
            detail="CSV contains malformed quoting or row structure.",
        ) from exc
    headers = [header.strip() for header in raw_headers]
    if len(headers) > MAX_IMPORT_COLUMNS:
        raise PersonImportServiceError(
            code="too_many_columns",
            detail=f"CSV cannot contain more than {MAX_IMPORT_COLUMNS} columns.",
        )
    folded_headers = [header.casefold() for header in headers]
    if (
        not headers
        or any(not header or len(header) > 128 for header in headers)
        or len(set(folded_headers)) != len(folded_headers)
    ):
        raise PersonImportServiceError(
            code="invalid_headers",
            detail="CSV headers must be non-empty, unique, and at most 128 characters.",
        )
    missing_headers = sorted(set(mapping.values()) - set(headers))
    if missing_headers:
        raise PersonImportServiceError(
            code="missing_headers",
            detail=f"Mapped CSV header(s) are missing: {', '.join(missing_headers)}.",
        )

    raw_rows = []
    try:
        for line_number, values in enumerate(reader, start=2):
            if not any((value or "").strip() for value in values):
                continue
            raw_rows.append((line_number, values))
            if len(raw_rows) > MAX_IMPORT_ROWS:
                raise PersonImportServiceError(
                    code="too_many_rows",
                    detail=f"CSV cannot contain more than {MAX_IMPORT_ROWS} non-empty rows.",
                )
    except csv.Error as exc:
        raise PersonImportServiceError(
            code="invalid_csv",
            detail="CSV contains malformed quoting or row structure.",
        ) from exc
    if not raw_rows:
        raise PersonImportServiceError(code="empty_csv", detail="CSV has no data rows.")

    parsed = []
    seen_identities: dict[tuple[str, str], int] = {}
    for line_number, values in raw_rows:
        record = {
            header: (values[index].strip() if index < len(values) else "")
            for index, header in enumerate(headers)
        }
        errors = []
        if len(values) > len(headers):
            errors.append(
                {
                    "field": "row",
                    "code": "extra_columns",
                    "detail": "Row contains more columns than the CSV header.",
                }
            )
        payload = {}
        row_hash = _sha256(_canonical_json(record))
        try:
            payload, row_hash = _build_onboarding_payload(record=record, mapping=mapping)
        except PersonImportServiceError as exc:
            errors.append({"field": "identities", "code": exc.code, "detail": exc.detail})
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            errors.append(
                {
                    "field": "row",
                    "code": "invalid_row",
                    "detail": _json_safe(detail) if detail is not None else "Row validation failed.",
                }
            )

        masked = []
        if payload:
            for identity in payload["identities"]:
                key = (identity["kind"], identity["normalized_value"])
                if key in seen_identities:
                    errors.append(
                        {
                            "field": "identities",
                            "code": "duplicate_identity_in_file",
                            "detail": f"Identity duplicates CSV row {seen_identities[key]}.",
                        }
                    )
                else:
                    seen_identities[key] = line_number
                masked.append(
                    {
                        "kind": identity["kind"],
                        "masked_value": _mask_identity(
                            identity["kind"], identity["normalized_value"]
                        ),
                    }
                )
        source_record_id = (
            payload.get("evidence", [{}])[0].get("source_record_id", row_hash)
            if payload
            else row_hash
        )
        parsed.append(
            ParsedImportRecord(
                row_number=line_number,
                row_hash=row_hash,
                source_record_id=source_record_id,
                display_name=payload.get("person", {}).get("display_name", "") if payload else "",
                normalized_payload=payload,
                masked_identities=masked,
                field_errors=errors,
            )
        )
    return parsed


def _preview_hash(*, file_bytes: bytes, mapping: dict) -> tuple[str, str]:
    content_hash = _sha256(file_bytes)
    request_hash = _sha256(
        _canonical_json(
            {
                "schema_version": IMPORT_SCHEMA_VERSION,
                "content_hash": content_hash,
                "mapping": mapping,
                "source": EvidenceSource.MANUAL,
                "source_namespace": SOURCE_NAMESPACE,
            }
        )
    )
    return content_hash, request_hash


def _existing_preview(*, org, idempotency_key, request_hash):
    batch = PersonImportBatch.objects.filter(
        org=org,
        idempotency_key=idempotency_key,
    ).first()
    if batch is None:
        return None
    if batch.request_hash != request_hash:
        raise PersonImportServiceError(
            code="import_idempotency_conflict",
            detail="Idempotency-Key was already used with a different import payload.",
        )
    return PersonImportPreviewResult(batch=batch, replayed=True)


def preview_person_import(
    *, org, requested_by, idempotency_key: UUID, file_bytes: bytes, filename: str, mapping
) -> PersonImportPreviewResult:
    mapping = validate_mapping(mapping)
    content_hash, request_hash = _preview_hash(file_bytes=file_bytes, mapping=mapping)
    replay = _existing_preview(
        org=org,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    if requested_by is not None and requested_by.org_id != org.id:
        raise PersonImportServiceError(
            code="import_actor_org_conflict",
            detail="Requester belongs to another organization.",
        )
    records = parse_person_csv(file_bytes=file_bytes, mapping=mapping)
    ready_count = sum(record.status == PersonImportRecordStatus.READY for record in records)
    invalid_count = len(records) - ready_count
    try:
        with transaction.atomic():
            batch = PersonImportBatch.objects.create(
                org=org,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                content_hash=content_hash,
                original_filename=_clean_filename(filename),
                file_size=len(file_bytes),
                mapping=mapping,
                headers=next(csv.reader(io.StringIO(_decode_csv(file_bytes)))),
                total_count=len(records),
                ready_count=ready_count,
                invalid_count=invalid_count,
            )
            PersonImportRecord.objects.bulk_create(
                [
                    PersonImportRecord(
                        org=org,
                        batch=batch,
                        row_number=record.row_number,
                        row_hash=record.row_hash,
                        source_record_id=record.source_record_id,
                        display_name=record.display_name,
                        normalized_payload=record.normalized_payload,
                        masked_identities=record.masked_identities,
                        field_errors=record.field_errors,
                        status=record.status,
                    )
                    for record in records
                ]
            )
    except IntegrityError:
        replay = _existing_preview(
            org=org,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise
    return PersonImportPreviewResult(batch=batch, replayed=False)


def _safe_dispatch(job) -> None:
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Person import job %s was persisted but not dispatched", job.id)


def _scrub_terminal_record_payload(record: PersonImportRecord) -> bool:
    """Remove executable staging data once a row no longer needs processing."""

    if (
        record.status not in STAGING_PAYLOAD_TERMINAL_STATUSES
        or not record.normalized_payload
    ):
        return False
    record.normalized_payload = {}
    return True


@transaction.atomic
def _expire_stale_preview_batch(*, org, batch_id: UUID, older_than: datetime) -> bool:
    # Import processing and conflict resolution lock a record before their batch.
    # Preserve that global order here so an expiry request cannot deadlock a
    # concurrent commit or worker that changed state after candidate selection.
    records = PersonImportRecord.objects.select_for_update().filter(
        org=org,
        batch_id=batch_id,
    ).order_by("id")
    list(records.values_list("id", flat=True))
    batch = PersonImportBatch.objects.select_for_update().filter(
        org=org,
        id=batch_id,
        status=PersonImportBatchStatus.PREVIEWED,
        created_at__lte=older_than,
    ).first()
    if batch is None:
        return False

    # An expired preview is no longer actionable, so all staging-only PII can be
    # removed while row identity, status, counts, and audit relationships remain.
    records.update(
        display_name="",
        normalized_payload={},
        masked_identities=[],
        field_errors=[],
        updated_at=timezone.now(),
    )
    batch.status = PersonImportBatchStatus.FAILED
    batch.error_code = "preview_expired"
    batch.completed_at = timezone.now()
    batch.revision += 1
    batch.save(
        update_fields=[
            "status",
            "error_code",
            "completed_at",
            "revision",
            "updated_at",
        ]
    )
    return True


def expire_stale_import_previews(
    *, org, older_than: datetime, limit: int = 100
) -> dict[str, object]:
    """Explicitly scrub a bounded set of stale, uncommitted import previews.

    Callers choose and pass the cutoff explicitly.  The periodic task applies
    the deployment retention setting independently inside each tenant context.
    """

    if org is None or not getattr(org, "id", None):
        raise PersonImportServiceError(
            code="invalid_expiry_scope",
            detail="An organization is required to expire import previews.",
        )
    if not isinstance(older_than, datetime) or timezone.is_naive(older_than):
        raise PersonImportServiceError(
            code="invalid_expiry_cutoff",
            detail="older_than must be a timezone-aware datetime.",
        )
    if older_than > timezone.now():
        raise PersonImportServiceError(
            code="invalid_expiry_cutoff",
            detail="older_than cannot be in the future.",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PREVIEW_EXPIRY_BATCHES:
        raise PersonImportServiceError(
            code="invalid_expiry_limit",
            detail=f"limit must be between 1 and {MAX_PREVIEW_EXPIRY_BATCHES}.",
        )

    # Candidate selection is deliberately lock-free and bounded. Each candidate
    # is rechecked under row locks so concurrent commits win or lose atomically.
    candidate_ids = list(
        PersonImportBatch.objects.filter(
            org=org,
            status=PersonImportBatchStatus.PREVIEWED,
            created_at__lte=older_than,
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    expired_ids = [
        batch_id
        for batch_id in candidate_ids
        if _expire_stale_preview_batch(
            org=org,
            batch_id=batch_id,
            older_than=older_than,
        )
    ]
    return {
        "expired_count": len(expired_ids),
        "batch_ids": [str(value) for value in expired_ids],
    }


def _commit_hash(*, batch_id: UUID, expected_revision: int) -> str:
    return _sha256(
        _canonical_json(
            {
                "batch_id": str(batch_id),
                "expected_revision": expected_revision,
                "schema_version": IMPORT_SCHEMA_VERSION,
            }
        )
    )


@transaction.atomic
def commit_person_import(
    *, org, requested_by, batch, expected_revision: int, idempotency_key: UUID
) -> PersonImportCommitResult:
    batch = PersonImportBatch.objects.select_for_update().get(org=org, id=batch.id)
    request_hash = _commit_hash(batch_id=batch.id, expected_revision=expected_revision)
    if batch.commit_idempotency_key is not None:
        if (
            batch.commit_idempotency_key == idempotency_key
            and batch.commit_request_hash == request_hash
        ):
            return PersonImportCommitResult(batch=batch, replayed=True)
        raise PersonImportServiceError(
            code="import_already_committed",
            detail="This preview has already been committed.",
        )
    if batch.status != PersonImportBatchStatus.PREVIEWED:
        raise PersonImportServiceError(
            code="invalid_import_state",
            detail="Only a previewed import can be committed.",
        )
    if batch.revision != expected_revision:
        raise PersonImportServiceError(
            code="import_revision_conflict",
            detail="Import revision changed; refresh before committing.",
        )
    if requested_by is not None and requested_by.org_id != org.id:
        raise PersonImportServiceError(
            code="import_actor_org_conflict",
            detail="Requester belongs to another organization.",
        )
    enqueued = enqueue_job(
        JobRequest(
            org_id=org.id,
            name=IMPORT_JOB_NAME,
            idempotency_key=f"matching-import:{batch.id}:{idempotency_key}",
            payload={
                "schema_version": IMPORT_SCHEMA_VERSION,
                "org_id": str(org.id),
                "batch_id": str(batch.id),
                "request_hash": batch.request_hash,
            },
            max_attempts=3,
        )
    )
    batch.automation_job = enqueued.job
    batch.commit_idempotency_key = idempotency_key
    batch.commit_request_hash = request_hash
    batch.status = PersonImportBatchStatus.QUEUED
    batch.revision += 1
    batch.error_code = ""
    batch.save(
        update_fields=[
            "automation_job",
            "commit_idempotency_key",
            "commit_request_hash",
            "status",
            "revision",
            "error_code",
            "updated_at",
        ]
    )
    transaction.on_commit(lambda: _safe_dispatch(enqueued.job))
    return PersonImportCommitResult(batch=batch, replayed=False)


def _record_payload(record: PersonImportRecord) -> dict:
    serializer = PersonOnboardingRequestSerializer(data=record.normalized_payload)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _merge_unique(existing: list, incoming: list) -> list:
    result = list(existing or [])
    seen = {str(item).casefold() for item in result}
    for item in incoming or []:
        if str(item).casefold() not in seen:
            result.append(item)
            seen.add(str(item).casefold())
    return result


def _candidate_people(*, org, identities_data) -> tuple[list[PersonIdentity], set[UUID]]:
    query = None
    from django.db.models import Q

    for identity in identities_data:
        clause = Q(
            kind=identity["kind"],
            normalized_value=identity["normalized_value"],
        )
        query = clause if query is None else query | clause
    if query is None:
        return [], set()
    identities = list(
        PersonIdentity.objects.select_for_update()
        .filter(query, org=org)
        .select_related("person")
    )
    return identities, {identity.person_id for identity in identities}


def _mark_conflict(*, record, code: str, person_ids) -> PersonImportRecord:
    record.status = PersonImportRecordStatus.CONFLICT
    record.error_code = code
    record.revision += 1
    record.save(update_fields=["status", "error_code", "revision", "updated_at"])
    conflict, created = PersonImportConflict.objects.get_or_create(
        org=record.org,
        batch=record.batch,
        record=record,
        defaults={
            "code": code,
            "person_ids": sorted(str(value) for value in person_ids),
            "revision": record.revision,
        },
    )
    if not created and conflict.status == PersonImportConflictStatus.OPEN:
        conflict.code = code
        conflict.person_ids = sorted(str(value) for value in person_ids)
        conflict.revision = record.revision
        conflict.save(update_fields=["code", "person_ids", "revision", "updated_at"])
    return record


def _desired_evidence(*, person, batch, evidence_data) -> Evidence:
    data = dict(evidence_data)
    data.pop("source", None)
    return Evidence(
        org=batch.org,
        person=person,
        source=batch.source,
        source_namespace=batch.source_namespace,
        **data,
    )


def _prior_successful_record(
    *,
    record: PersonImportRecord,
    source_record_id: str | None = None,
    row_hash: str | None = None,
) -> PersonImportRecord | None:
    """Find a completed source-ledger row without trusting Evidence.content_hash.

    Import row hashes cover the complete normalized request, including confidence and
    timestamps, but intentionally exclude source_record_id.  This lets an identical
    row replay after a harmless upstream record-id rename while still detecting a
    changed payload under the same source record id.
    """

    queryset = (
        PersonImportRecord.objects.select_for_update()
        .select_related("person", "batch")
        .filter(
            org=record.org,
            batch__source=record.batch.source,
            batch__source_namespace=record.batch.source_namespace,
            person__isnull=False,
            status__in=SUCCESSFUL_RECORD_STATUSES,
        )
        .exclude(id=record.id)
    )
    if source_record_id is not None:
        queryset = queryset.filter(source_record_id=source_record_id)
    if row_hash is not None:
        queryset = queryset.filter(row_hash=row_hash)
    return queryset.order_by("created_at", "row_number", "id").first()


def _resolved_identities_for_person(*, person, identities_data) -> list[PersonIdentity]:
    query = None
    from django.db.models import Q

    for identity in identities_data:
        clause = Q(
            kind=identity["kind"],
            normalized_value=identity["normalized_value"],
        )
        query = clause if query is None else query | clause
    if query is None:
        return []
    return list(
        PersonIdentity.objects.select_for_update().filter(
            query,
            org=person.org,
            person=person,
        )
    )


def _apply_payload_to_person(
    *, record, payload, person: Person | None, existing_identities, forced=False
) -> tuple[Person, str, list[str], list[PersonIdentity], Evidence | None]:
    batch = record.batch
    person_data = dict(payload["person"])
    identities_data = [dict(value) for value in payload["identities"]]
    evidence_data = dict(payload["evidence"][0])
    evidence_data["source_record_id"] = record.source_record_id

    prior_source_record = _prior_successful_record(
        record=record,
        source_record_id=record.source_record_id,
    )
    if prior_source_record is not None:
        if prior_source_record.row_hash != record.row_hash:
            raise PersonImportServiceError(
                code="source_record_conflict",
                detail="Source record was already imported with different content.",
            )
        replay_person = prior_source_record.person
        return (
            replay_person,
            PersonImportRecordStatus.REPLAYED,
            [],
            _resolved_identities_for_person(
                person=replay_person,
                identities_data=identities_data,
            ),
            None,
        )

    existing_evidence = Evidence.objects.select_for_update().filter(
        org=batch.org,
        source=batch.source,
        source_namespace=batch.source_namespace,
        source_record_id=record.source_record_id,
    ).first()
    evidence_person = person or (existing_evidence.person if existing_evidence else None)
    if existing_evidence is not None:
        desired = _desired_evidence(
            person=evidence_person,
            batch=batch,
            evidence_data=evidence_data,
        )
        if existing_evidence.content_hash != desired.compute_content_hash():
            raise PersonImportServiceError(
                code="source_record_conflict",
                detail="Source record was already imported with different content.",
            )
        return (
            existing_evidence.person,
            PersonImportRecordStatus.REPLAYED,
            [],
            _resolved_identities_for_person(
                person=existing_evidence.person,
                identities_data=identities_data,
            ),
            None,
        )

    prior_semantic_record = _prior_successful_record(
        record=record,
        row_hash=record.row_hash,
    )
    if prior_semantic_record is not None:
        replay_person = prior_semantic_record.person
        return (
            replay_person,
            PersonImportRecordStatus.REPLAYED,
            [],
            _resolved_identities_for_person(
                person=replay_person,
                identities_data=identities_data,
            ),
            None,
        )

    created = person is None
    changed_fields = []
    if person is None:
        person = Person.objects.create(org=batch.org, **person_data)
        changed_fields = sorted(person_data)
    else:
        scalar_fields = (
            "first_name",
            "last_name",
            "headline",
            "summary",
            "current_title",
            "current_company",
            "location",
            "timezone",
        )
        for field in scalar_fields:
            incoming = person_data.get(field)
            if incoming and not getattr(person, field):
                setattr(person, field, incoming)
                changed_fields.append(field)
        if person.availability == "unknown" and person_data.get("availability") not in (
            None,
            "unknown",
        ):
            person.availability = person_data["availability"]
            changed_fields.append("availability")
        for field in ("skills", "roles"):
            merged = _merge_unique(getattr(person, field), person_data.get(field, []))
            if merged != getattr(person, field):
                setattr(person, field, merged)
                changed_fields.append(field)
        if changed_fields:
            person.save(update_fields=[*changed_fields, "updated_at"])

    by_key = {
        (identity.kind, identity.normalized_value): identity
        for identity in existing_identities
        if identity.person_id == person.id
    }
    resolved_identities = []
    for data in identities_data:
        data.pop("source", None)
        key = (data["kind"], data["normalized_value"])
        identity = by_key.get(key)
        if identity is None:
            collision = PersonIdentity.objects.filter(
                org=batch.org,
                kind=data["kind"],
                normalized_value=data["normalized_value"],
            ).first()
            if collision is not None:
                if not forced or collision.person_id == person.id:
                    identity = collision
                else:
                    continue
            else:
                identity = PersonIdentity.objects.create(
                    org=batch.org,
                    person=person,
                    source=batch.source,
                    **data,
                )
                changed_fields.append("identities")
        resolved_identities.append(identity)

    evidence = _desired_evidence(
        person=person,
        batch=batch,
        evidence_data=evidence_data,
    )
    evidence.save()
    ensure_evidence_provenance(
        evidence=evidence,
        actor=batch.requested_by,
        collection_method=(
            EvidenceCollectionMethod.INBOUND_EMAIL
            if batch.source == EvidenceSource.EMAIL
            else EvidenceCollectionMethod.CSV_IMPORT
            if batch.source == EvidenceSource.MANUAL
            and batch.source_namespace == SOURCE_NAMESPACE
            else EvidenceCollectionMethod.PROVIDER_API
        ),
    )
    matching_fields = {"current_title", "location", "availability", "skills", "roles"}
    if created:
        impact_fields = list(dict.fromkeys(changed_fields))
    else:
        impact_fields = [field for field in changed_fields if field in matching_fields]
        impact_fields.append("evidence")
    impact_type = (
        PersonImportImpactType.CREATED if created else PersonImportImpactType.MERGED
    )
    return (
        person,
        impact_type,
        list(dict.fromkeys(impact_fields)),
        resolved_identities,
        evidence,
    )


@transaction.atomic
def _process_record_once(*, org, record_id: UUID):
    # Import processing may lock and update an existing Person. Match writers
    # take the tenant mutex before Person rows, so imports must follow the same
    # order to avoid a deferred-FK commit cycle with concurrent recompute.
    lock_matching_org(org.id)
    record = (
        PersonImportRecord.objects.select_for_update()
        .select_related("batch", "batch__org")
        .get(org=org, id=record_id)
    )
    if record.status != PersonImportRecordStatus.READY:
        if _scrub_terminal_record_payload(record):
            record.save(update_fields=["normalized_payload", "updated_at"])
        return record
    payload = _record_payload(record)
    identities, person_ids = _candidate_people(
        org=org,
        identities_data=payload["identities"],
    )
    if len(person_ids) > 1:
        return _mark_conflict(
            record=record,
            code="identity_split_conflict",
            person_ids=person_ids,
        )
    person = Person.objects.select_for_update().filter(
        org=org,
        id=next(iter(person_ids), None),
    ).first()
    if person is not None and person.governance_status != PersonGovernanceStatus.ACTIVE:
        record.status = PersonImportRecordStatus.SKIPPED
        record.error_code = "person_governance_restricted"
        record.revision += 1
        _scrub_terminal_record_payload(record)
        record.save(
            update_fields=[
                "status",
                "error_code",
                "revision",
                "normalized_payload",
                "updated_at",
            ]
        )
        return record
    try:
        person, outcome, changed_fields, resolved_identities, _evidence = (
            _apply_payload_to_person(
                record=record,
                payload=payload,
                person=person,
                existing_identities=identities,
            )
        )
    except PersonImportServiceError as exc:
        if exc.code == "source_record_conflict":
            existing = Evidence.objects.filter(
                org=org,
                source=record.batch.source,
                source_namespace=record.batch.source_namespace,
                source_record_id=record.source_record_id,
            ).first()
            candidates = set(person_ids)
            if existing is not None:
                candidates.add(existing.person_id)
            return _mark_conflict(record=record, code=exc.code, person_ids=candidates)
        raise

    record.person = person
    record.status = outcome
    record.error_code = ""
    record.revision += 1
    _scrub_terminal_record_payload(record)
    record.save(
        update_fields=[
            "person",
            "status",
            "error_code",
            "revision",
            "normalized_payload",
            "updated_at",
        ]
    )
    if changed_fields:
        impact, created = PersonImportImpact.objects.get_or_create(
            org=org,
            batch=record.batch,
            person=person,
            defaults={
                "record": record,
                "impact_type": outcome,
                "changed_fields": changed_fields,
            },
        )
        if not created:
            merged_fields = _merge_unique(impact.changed_fields, changed_fields)
            if merged_fields != impact.changed_fields:
                impact.changed_fields = merged_fields
                impact.save(update_fields=["changed_fields", "updated_at"])
    for identity in resolved_identities:
        PersonIdentityObservation.objects.get_or_create(
            org=org,
            identity=identity,
            source=record.batch.source,
            source_namespace=record.batch.source_namespace,
            source_record_id=record.source_record_id,
            defaults={
                "batch": record.batch,
                "record": record,
                "person": person,
                "kind": identity.kind,
                "normalized_value_hash": _sha256(identity.normalized_value),
            },
        )
    return record


def _process_record(*, org, record_id: UUID):
    try:
        return _process_record_once(org=org, record_id=record_id)
    except IntegrityError:
        # A concurrent identity/source insert may have won. The first transaction
        # was rolled back; one fresh resolution pass converges to replay/merge/conflict.
        return _process_record_once(org=org, record_id=record_id)


@transaction.atomic
def _mark_record_failed(*, org, record_id: UUID, code: str):
    record = PersonImportRecord.objects.select_for_update().get(org=org, id=record_id)
    if record.status != PersonImportRecordStatus.READY:
        return record
    safe_code = (code or "invalid_persisted_row")[:80]
    record.status = PersonImportRecordStatus.FAILED
    record.error_code = safe_code
    record.normalized_payload = {}
    record.field_errors = [
        {
            "field": "row",
            "code": safe_code,
            "detail": "The persisted row could not be processed.",
        }
    ]
    record.revision += 1
    record.save(
        update_fields=[
            "status",
            "error_code",
            "normalized_payload",
            "field_errors",
            "revision",
            "updated_at",
        ]
    )
    return record


def _batch_counts(batch) -> dict[str, int]:
    statuses = list(batch.records.values_list("status", flat=True))
    return {
        "total_count": len(statuses),
        "processed_count": sum(
            status != PersonImportRecordStatus.READY for status in statuses
        ),
        "ready_count": statuses.count(PersonImportRecordStatus.READY),
        "created_count": statuses.count(PersonImportRecordStatus.CREATED),
        "merged_count": statuses.count(PersonImportRecordStatus.MERGED),
        "conflict_count": statuses.count(PersonImportRecordStatus.CONFLICT),
        "invalid_count": statuses.count(PersonImportRecordStatus.INVALID),
        "skipped_count": statuses.count(PersonImportRecordStatus.SKIPPED),
        "replayed_count": statuses.count(PersonImportRecordStatus.REPLAYED),
        "failed_count": statuses.count(PersonImportRecordStatus.FAILED),
    }


def _enqueue_affected_recomputes(batch) -> list[str]:
    affected_ids = list(
        Person.objects.filter(
            org=batch.org,
            status="active",
            import_impacts__batch=batch,
        )
        .order_by("id")
        .values_list("id", flat=True)
        .distinct()
    )
    if not affected_ids:
        return []
    run_ids = []
    opportunities = MatchOpportunity.objects.filter(
        org=batch.org,
        status=MatchOpportunityStatus.OPEN,
    ).order_by("id")
    for opportunity in opportunities:
        try:
            run = enqueue_opportunity_recompute(
                org=batch.org,
                opportunity=opportunity,
                requested_by=batch.requested_by,
                person_ids=affected_ids,
                idempotency_key=uuid5(batch.id, str(opportunity.id)),
            )
        except RecomputeEnqueueError:
            logger.exception(
                "Could not enqueue affected import recompute for opportunity %s",
                opportunity.id,
            )
            continue
        run_ids.append(str(run.id))
    return run_ids


def execute_person_import(*, org_id: UUID, batch_id: UUID, request_hash: str) -> dict:
    from common.models import Org

    org = Org.objects.get(id=org_id)
    with transaction.atomic():
        batch = PersonImportBatch.objects.select_for_update().get(org=org, id=batch_id)
        if batch.request_hash != request_hash:
            raise PersonImportServiceError(
                code="import_snapshot_changed",
                detail="Import request hash no longer matches its persisted preview.",
            )
        if batch.status in {
            PersonImportBatchStatus.COMPLETED,
            PersonImportBatchStatus.PARTIAL,
        }:
            return {
                "batch_id": str(batch.id),
                "status": batch.status,
                "processed_count": batch.processed_count,
            }
        batch.status = PersonImportBatchStatus.RUNNING
        batch.started_at = batch.started_at or timezone.now()
        batch.revision += 1
        batch.save(update_fields=["status", "started_at", "revision", "updated_at"])

    ready_ids = list(
        PersonImportRecord.objects.filter(
            org=org,
            batch_id=batch_id,
            status=PersonImportRecordStatus.READY,
        )
        .order_by("row_number")
        .values_list("id", flat=True)
    )
    for record_id in ready_ids:
        try:
            _process_record(org=org, record_id=record_id)
        except OperationalError:
            raise
        except PersonImportServiceError as exc:
            _mark_record_failed(org=org, record_id=record_id, code=exc.code)
        except DRFValidationError:
            _mark_record_failed(
                org=org,
                record_id=record_id,
                code="invalid_persisted_row",
            )

    with transaction.atomic():
        batch = PersonImportBatch.objects.select_for_update().get(org=org, id=batch_id)
        counts = _batch_counts(batch)
        for field, value in counts.items():
            setattr(batch, field, value)
        has_issues = bool(
            counts["invalid_count"]
            or counts["conflict_count"]
            or counts["skipped_count"]
            or counts["failed_count"]
        )
        batch.status = (
            PersonImportBatchStatus.PARTIAL
            if has_issues
            else PersonImportBatchStatus.COMPLETED
        )
        batch.completed_at = timezone.now()
        batch.revision += 1
        batch.save(
            update_fields=[
                *counts,
                "status",
                "completed_at",
                "revision",
                "updated_at",
            ]
        )
    run_ids = _enqueue_affected_recomputes(batch)
    if run_ids:
        PersonImportBatch.objects.filter(org=org, id=batch.id).update(match_run_ids=run_ids)
        batch.match_run_ids = run_ids
    return {
        "batch_id": str(batch.id),
        "status": batch.status,
        "processed_count": batch.processed_count,
        "created_count": batch.created_count,
        "merged_count": batch.merged_count,
        "conflict_count": batch.conflict_count,
        "invalid_count": batch.invalid_count,
        "match_run_ids": run_ids,
    }


def _resolution_hash(*, record_id, action, person_id, expected_revision):
    return _sha256(
        _canonical_json(
            {
                "record_id": str(record_id),
                "action": action,
                "person_id": str(person_id or ""),
                "expected_revision": expected_revision,
            }
        )
    )


def _enqueue_resolution_recomputes(*, org_id, record_id, revision):
    record = (
        PersonImportRecord.objects.filter(org_id=org_id, id=record_id)
        .select_related("batch", "person", "batch__requested_by", "batch__org")
        .first()
    )
    if record is None or record.person is None or record.person.status != "active":
        return
    run_ids = []
    for opportunity in MatchOpportunity.objects.filter(
        org_id=org_id,
        status=MatchOpportunityStatus.OPEN,
    ).order_by("id"):
        try:
            run = enqueue_opportunity_recompute(
                org=record.batch.org,
                opportunity=opportunity,
                requested_by=record.batch.requested_by,
                person_ids=[record.person_id],
                idempotency_key=uuid5(record.id, f"{revision}:{opportunity.id}"),
            )
        except RecomputeEnqueueError:
            logger.exception(
                "Could not enqueue conflict-resolution recompute for opportunity %s",
                opportunity.id,
            )
            continue
        run_ids.append(str(run.id))
    if not run_ids:
        return
    with transaction.atomic():
        batch = PersonImportBatch.objects.select_for_update().get(
            org_id=org_id,
            id=record.batch_id,
        )
        batch.match_run_ids = list(dict.fromkeys([*batch.match_run_ids, *run_ids]))
        batch.save(update_fields=["match_run_ids", "updated_at"])


def _refresh_batch_after_resolution(batch):
    batch = PersonImportBatch.objects.select_for_update().get(
        org=batch.org,
        id=batch.id,
    )
    counts = _batch_counts(batch)
    for field, value in counts.items():
        setattr(batch, field, value)
    has_issues = bool(
        counts["invalid_count"]
        or counts["conflict_count"]
        or counts["skipped_count"]
        or counts["failed_count"]
    )
    batch.status = (
        PersonImportBatchStatus.PARTIAL
        if has_issues
        else PersonImportBatchStatus.COMPLETED
    )
    batch.completed_at = batch.completed_at or timezone.now()
    batch.revision += 1
    batch.save(
        update_fields=[
            *counts,
            "status",
            "completed_at",
            "revision",
            "updated_at",
        ]
    )
    return batch


@transaction.atomic
def resolve_person_import_record(
    *,
    org,
    actor,
    record,
    action: str,
    person_id: UUID | None,
    expected_revision: int,
    idempotency_key: UUID,
) -> PersonImportResolutionResult:
    request_hash = _resolution_hash(
        record_id=record.id,
        action=action,
        person_id=person_id,
        expected_revision=expected_revision,
    )
    existing = PersonImportDecision.objects.filter(
        org=org,
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise PersonImportServiceError(
                code="decision_idempotency_conflict",
                detail="Idempotency-Key was already used for another decision.",
            )
        return PersonImportResolutionResult(
            record=existing.record,
            decision=existing,
            replayed=True,
        )
    # Linking a conflict can update an existing Person; acquire the same first
    # lock as projection writers before the record, conflict, and Person rows.
    lock_matching_org(org.id)
    record = (
        PersonImportRecord.objects.select_for_update()
        .select_related("batch")
        .get(org=org, id=record.id)
    )
    conflict = PersonImportConflict.objects.select_for_update().filter(
        org=org,
        record=record,
        status=PersonImportConflictStatus.OPEN,
    ).first()
    if conflict is None or record.status != PersonImportRecordStatus.CONFLICT:
        concurrent = PersonImportDecision.objects.filter(
            org=org,
            idempotency_key=idempotency_key,
        ).first()
        if concurrent is not None and concurrent.request_hash == request_hash:
            return PersonImportResolutionResult(
                record=concurrent.record,
                decision=concurrent,
                replayed=True,
            )
        raise PersonImportServiceError(
            code="conflict_not_open",
            detail="This import record has no open conflict.",
        )
    if record.revision != expected_revision or conflict.revision != expected_revision:
        raise PersonImportServiceError(
            code="import_revision_conflict",
            detail="Import record revision changed; refresh before resolving.",
        )
    if actor is not None and actor.org_id != org.id:
        raise PersonImportServiceError(
            code="import_actor_org_conflict",
            detail="Decision actor belongs to another organization.",
        )
    target = None
    if action == PersonImportDecisionAction.LINK_EXISTING:
        if person_id is None or str(person_id) not in conflict.person_ids:
            raise PersonImportServiceError(
                code="invalid_conflict_target",
                detail="person_id must identify one of the conflict candidates.",
            )
        target = Person.objects.select_for_update().filter(org=org, id=person_id).first()
        if target is None:
            raise PersonImportServiceError(
                code="invalid_conflict_target",
                detail="Conflict target does not exist in this organization.",
            )
        if target.governance_status != PersonGovernanceStatus.ACTIVE:
            raise PersonImportServiceError(
                code="person_governance_restricted",
                detail="Conflict target is not available for import.",
            )
        if conflict.code == "source_record_conflict":
            raise PersonImportServiceError(
                code="source_record_conflict_requires_skip",
                detail="A conflicting immutable source record can only be skipped.",
            )
        payload = _record_payload(record)
        existing_identities, _ = _candidate_people(
            org=org,
            identities_data=payload["identities"],
        )
        target, outcome, changed_fields, resolved_identities, _evidence = (
            _apply_payload_to_person(
                record=record,
                payload=payload,
                person=target,
                existing_identities=existing_identities,
                forced=True,
            )
        )
        if outcome == PersonImportRecordStatus.REPLAYED:
            record.status = PersonImportRecordStatus.REPLAYED
        else:
            record.status = PersonImportRecordStatus.MERGED
            if changed_fields:
                impact, created = PersonImportImpact.objects.get_or_create(
                    org=org,
                    batch=record.batch,
                    person=target,
                    defaults={
                        "record": record,
                        "impact_type": PersonImportImpactType.MERGED,
                        "changed_fields": changed_fields,
                    },
                )
                if not created:
                    merged_fields = _merge_unique(impact.changed_fields, changed_fields)
                    if merged_fields != impact.changed_fields:
                        impact.changed_fields = merged_fields
                        impact.save(update_fields=["changed_fields", "updated_at"])
            for identity in resolved_identities:
                if identity.person_id != target.id:
                    continue
                PersonIdentityObservation.objects.get_or_create(
                    org=org,
                    batch=record.batch,
                    record=record,
                    person=target,
                    identity=identity,
                    kind=identity.kind,
                    normalized_value_hash=_sha256(identity.normalized_value),
                    defaults={
                        "source": record.batch.source,
                        "source_namespace": record.batch.source_namespace,
                        "source_record_id": record.source_record_id,
                    },
                )
        record.person = target
    elif action == PersonImportDecisionAction.SKIP:
        if person_id is not None:
            raise PersonImportServiceError(
                code="invalid_conflict_target",
                detail="skip must not include person_id.",
            )
        record.status = PersonImportRecordStatus.SKIPPED
        record.person = None
    else:
        raise PersonImportServiceError(
            code="invalid_decision_action",
            detail="action must be link_existing or skip.",
        )

    resulting_revision = expected_revision + 1
    record.error_code = ""
    record.revision = resulting_revision
    _scrub_terminal_record_payload(record)
    record.save(
        update_fields=[
            "status",
            "person",
            "error_code",
            "revision",
            "normalized_payload",
            "updated_at",
        ]
    )
    conflict.status = PersonImportConflictStatus.RESOLVED
    conflict.revision = resulting_revision
    conflict.resolved_at = timezone.now()
    conflict.save(update_fields=["status", "revision", "resolved_at", "updated_at"])
    decision = PersonImportDecision.objects.create(
        org=org,
        batch=record.batch,
        record=record,
        conflict=conflict,
        action=action,
        target_person=target,
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        expected_revision=expected_revision,
        resulting_revision=resulting_revision,
    )
    _refresh_batch_after_resolution(record.batch)
    if record.status == PersonImportRecordStatus.MERGED and changed_fields:
        transaction.on_commit(
            lambda: _enqueue_resolution_recomputes(
                org_id=org.id,
                record_id=record.id,
                revision=resulting_revision,
            )
        )
    return PersonImportResolutionResult(record=record, decision=decision, replayed=False)
