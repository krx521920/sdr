"""Approval-gated Feishu Base reads into the canonical Person import preview."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from automation.errors import PermanentJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from integrations.execution_safety import (
    ExecutionSafetyError,
    mark_execution_delivered,
    mark_execution_sending,
    mark_provider_accepted,
    release_execution,
    reserve_execution,
)
from integrations.models import (
    ChannelExecutionControl,
    ChannelTestTarget,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    FeishuBaseConnection,
    FeishuBasePersonImport,
    FeishuBasePersonImportStatus,
    OrganizationExecutionControl,
    hash_feishu_record_identifier,
)
from integrations.providers.feishu_base.client import (
    DATE_TIME,
    PHONE,
    SINGLE_SELECT,
    TEXT,
    URL,
    FEISHU_IMPORT_PERSON_ACTION,
    FeishuBaseAPIError,
    FeishuBaseClient,
    FeishuBaseConfigurationError,
)
from integrations.providers.feishu_base.sync import (
    FeishuBaseExecutionIntent,
    FeishuBaseSyncUnavailable,
    _connection_snapshot,
    _execution_intent,
    _require_ready_connection,
)
from matching.models import EvidenceSource
from matching.provider_import import (
    ProviderPersonRecord,
    preview_provider_person_import,
)

logger = logging.getLogger(__name__)

FEISHU_BASE_PERSON_IMPORT_JOB = "integrations.feishu_base_person_import"
MAX_FEISHU_IMPORT_ROWS = 500
MAX_FEISHU_VALUE_CHARS = 5000
SAFE_IMPORT_ERROR = "Feishu Base person import did not complete successfully."


class _ConsumedImportFailure(Exception):
    """A normal post-read failure that was durably settled as UNKNOWN."""

    def __init__(self, code: str):
        self.code = str(code or "feishu_import_outcome_unknown")[:80]
        super().__init__(self.code)


class _UnsettledPostReadFailure(Exception):
    """A post-read failure whose settlement rolled back and must not refund."""

    def __init__(self, code: str):
        self.code = str(code or "feishu_import_outcome_unknown")[:80]
        super().__init__(self.code)

FEISHU_PERSON_IMPORT_FIELD_KEYS = frozenset(
    {
        "display_name",
        "first_name",
        "last_name",
        "current_title",
        "current_company",
        "location",
        "email",
        "phone",
        "linkedin",
        "evidence_summary",
        "observed_at",
    }
)

_EXPECTED_IMPORT_FIELD_TYPES = {
    "display_name": frozenset({TEXT, SINGLE_SELECT}),
    "first_name": frozenset({TEXT}),
    "last_name": frozenset({TEXT}),
    "current_title": frozenset({TEXT, SINGLE_SELECT}),
    "current_company": frozenset({TEXT, SINGLE_SELECT}),
    "location": frozenset({TEXT, SINGLE_SELECT}),
    "email": frozenset({TEXT}),
    "phone": frozenset({TEXT, PHONE}),
    "linkedin": frozenset({TEXT, URL}),
    "evidence_summary": frozenset({TEXT}),
    "observed_at": frozenset({TEXT, DATE_TIME}),
}


@dataclass(frozen=True, slots=True)
class FeishuBaseImportEnqueueResult:
    person_import: FeishuBasePersonImport
    replayed: bool


def validate_person_import_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise FeishuBaseSyncUnavailable(
            "Feishu Base import mapping must be an object.",
            code="feishu_import_mapping_invalid",
        )
    unknown = sorted(set(mapping) - FEISHU_PERSON_IMPORT_FIELD_KEYS)
    if unknown:
        raise FeishuBaseSyncUnavailable(
            "Feishu Base import mapping contains unsupported fields.",
            code="feishu_import_mapping_invalid",
        )
    cleaned: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
            raise FeishuBaseSyncUnavailable(
                "Feishu Base import field names must be non-empty and bounded.",
                code="feishu_import_mapping_invalid",
            )
        cleaned[str(key)] = value.strip()
    if len(cleaned.values()) != len(set(cleaned.values())):
        raise FeishuBaseSyncUnavailable(
            "Each Feishu field can be mapped only once.",
            code="feishu_import_mapping_invalid",
        )
    if not {"email", "phone", "linkedin"}.intersection(cleaned):
        raise FeishuBaseSyncUnavailable(
            "Map at least one stable identity field.",
            code="feishu_import_identity_mapping_required",
        )
    if "display_name" not in cleaned and not {"first_name", "last_name"}.intersection(
        cleaned
    ):
        raise FeishuBaseSyncUnavailable(
            "Map display_name, first_name, or last_name.",
            code="feishu_import_name_mapping_required",
        )
    return cleaned


def feishu_person_import_execution_intent(
    *,
    connection: FeishuBaseConnection,
    mapping: Mapping[str, str],
    row_limit: int,
) -> FeishuBaseExecutionIntent:
    _require_ready_connection(connection)
    cleaned = validate_person_import_mapping(mapping)
    if not isinstance(row_limit, int) or not 1 <= row_limit <= MAX_FEISHU_IMPORT_ROWS:
        raise FeishuBaseSyncUnavailable(
            "Feishu Base import row limit is invalid.",
            code="feishu_import_limit_invalid",
        )
    return _execution_intent(
        connection=connection,
        action=FEISHU_IMPORT_PERSON_ACTION,
        snapshot={
            "connection": _connection_snapshot(connection),
            "mapping_sha256": _sha256(cleaned),
            "row_limit": row_limit,
        },
    )


def enqueue_feishu_person_import(
    *,
    connection: FeishuBaseConnection,
    requested_by,
    mapping: Mapping[str, str],
    row_limit: int,
    approval_id: UUID,
    idempotency_key: UUID,
) -> FeishuBaseImportEnqueueResult:
    if requested_by is None or requested_by.org_id != connection.org_id:
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base import requester belongs to another organization.",
            code="import_actor_org_conflict",
        )
    cleaned = validate_person_import_mapping(mapping)
    connection_snapshot = FeishuBaseConnection.objects.get(
        org_id=connection.org_id,
        id=connection.id,
    )
    intent = feishu_person_import_execution_intent(
        connection=connection_snapshot,
        mapping=cleaned,
        row_limit=row_limit,
    )
    mapping_sha256 = _sha256(cleaned)
    destination_sha256 = _destination_sha256(connection_snapshot)
    source_namespace = _source_namespace(connection_snapshot)

    with transaction.atomic():
        try:
            reservation = reserve_execution(
                org=connection_snapshot.org,
                channel=intent.channel,
                action=intent.action,
                target_hash=intent.target_hash,
                payload_hash=intent.payload_hash,
                units=intent.units,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )
        except ExecutionSafetyError as exc:
            raise FeishuBaseSyncUnavailable(exc.detail, code=exc.code) from exc
        request = reservation.request
        connection = FeishuBaseConnection.objects.select_for_update().get(
            org_id=connection_snapshot.org_id,
            id=connection_snapshot.id,
        )
        locked_intent = feishu_person_import_execution_intent(
            connection=connection,
            mapping=cleaned,
            row_limit=row_limit,
        )
        _assert_request_scope(request=request, intent=locked_intent)
        if reservation.replayed:
            existing = (
                FeishuBasePersonImport.objects.select_for_update()
                .filter(org=connection.org, execution_request=request)
                .first()
            )
            if existing is None:
                raise FeishuBaseSyncUnavailable(
                    "The Feishu Base import replay has no durable ledger.",
                    code="feishu_import_replay_state_missing",
                )
            if (
                existing.connection_id != connection.id
                or existing.mapping_sha256 != mapping_sha256
                or existing.destination_sha256 != destination_sha256
                or existing.row_limit != row_limit
                or existing.source_namespace != source_namespace
            ):
                raise FeishuBaseSyncUnavailable(
                    "The Feishu Base import replay scope changed.",
                    code="feishu_execution_snapshot_changed",
                )
            return FeishuBaseImportEnqueueResult(existing, True)

        person_import = FeishuBasePersonImport(
            org=connection.org,
            connection=connection,
            requested_by=requested_by,
            execution_request=request,
            status=FeishuBasePersonImportStatus.QUEUED,
            mapping_sha256=mapping_sha256,
            destination_sha256=destination_sha256,
            source_namespace=source_namespace,
            row_limit=row_limit,
        )
        person_import.set_mapping(cleaned)
        person_import.full_clean()
        person_import.save()
        enqueued = enqueue_job(
            JobRequest(
                org_id=connection.org_id,
                name=FEISHU_BASE_PERSON_IMPORT_JOB,
                idempotency_key=f"feishu-base-person-import:{request.id}",
                payload={
                    "import_id": str(person_import.id),
                    "execution_request_id": str(request.id),
                },
                max_attempts=1,
            )
        )
        if not enqueued.created:
            raise FeishuBaseSyncUnavailable(
                "The Feishu Base import job binding already exists.",
                code="feishu_import_job_conflict",
            )
        person_import.automation_job = enqueued.job
        person_import.full_clean()
        person_import.save(update_fields=["automation_job", "updated_at"])

    try:
        dispatch_job(enqueued.job)
    except Exception:
        logger.exception("Could not dispatch Feishu Base import job %s", enqueued.job.id)
    return FeishuBaseImportEnqueueResult(person_import, False)


def process_feishu_base_person_import_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "import_id",
        "execution_request_id",
    }:
        raise PermanentJobError(
            "The Feishu Base import payload is invalid.",
            code="invalid_job_payload",
        )
    try:
        import_id = UUID(str(payload["import_id"]))
        request_id = UUID(str(payload["execution_request_id"]))
    except (TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The Feishu Base import payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    person_import = (
        FeishuBasePersonImport.objects.filter(
            id=import_id,
            execution_request_id=request_id,
        )
        .select_related(
            "org",
            "connection",
            "requested_by",
            "execution_request__approval__approved_by",
            "automation_job",
        )
        .first()
    )
    if person_import is None:
        raise PermanentJobError(
            "The Feishu Base import state was not found.",
            code="feishu_import_state_not_found",
        )
    request = person_import.execution_request
    if (
        person_import.status != FeishuBasePersonImportStatus.QUEUED
        or request.status != ExternalRequestStatus.RESERVED
        or person_import.automation_job_id is None
        or person_import.automation_job.name != FEISHU_BASE_PERSON_IMPORT_JOB
        or person_import.automation_job.payload != {
            "import_id": str(person_import.id),
            "execution_request_id": str(request.id),
        }
    ):
        raise PermanentJobError(
            "This Feishu Base import was already attempted or is not safely bound.",
            code="feishu_execution_not_replayable",
        )

    try:
        mapping = validate_person_import_mapping(person_import.get_mapping())
        intent = feishu_person_import_execution_intent(
            connection=person_import.connection,
            mapping=mapping,
            row_limit=person_import.row_limit,
        )
        _assert_request_scope(request=request, intent=intent)
        if (
            person_import.mapping_sha256 != _sha256(mapping)
            or person_import.destination_sha256
            != _destination_sha256(person_import.connection)
            or person_import.source_namespace
            != _source_namespace(person_import.connection)
        ):
            raise FeishuBaseSyncUnavailable(
                "The Feishu Base import snapshot changed.",
                code="feishu_execution_snapshot_changed",
            )
        app_secret = person_import.connection.get_app_secret()
        bound_client = _client().for_execution(
            org=person_import.org,
            action=FEISHU_IMPORT_PERSON_ACTION,
            execution_request_id=request.id,
        )
    except Exception as exc:
        code = getattr(exc, "code", "feishu_credentials_unavailable")
        _settle_pre_read_failure(
            person_import=person_import,
            request=request,
            code=code,
            expected_status=ExternalRequestStatus.RESERVED,
        )
        raise PermanentJobError(SAFE_IMPORT_ERROR, code=code) from None

    try:
        request = _claim_person_import_once(
            person_import=person_import,
            request=request,
        )
    except (ExecutionSafetyError, FeishuBaseSyncUnavailable) as exc:
        raise PermanentJobError(
            getattr(exc, "detail", str(exc)),
            code=getattr(exc, "code", "feishu_execution_not_replayable"),
        ) from exc
    person_import.refresh_from_db()

    try:
        access_token = bound_client.tenant_access_token(
            app_id=person_import.connection.app_id,
            app_secret=app_secret,
        )
        provider_fields = bound_client.list_fields(
            access_token=access_token,
            app_token=person_import.connection.app_token,
            table_id=person_import.connection.table_id,
        )
        _validate_provider_mapping(mapping, provider_fields)
    except (FeishuBaseAPIError, FeishuBaseConfigurationError) as exc:
        code = getattr(exc, "error_code", "feishu_import_schema_invalid")
        _settle_pre_read_failure(
            person_import=person_import,
            request=request,
            code=code,
            expected_status=ExternalRequestStatus.SENDING,
        )
        raise PermanentJobError(SAFE_IMPORT_ERROR, code=code) from None
    except Exception:
        code = "feishu_local_pre_read_failed"
        _settle_pre_read_failure(
            person_import=person_import,
            request=request,
            code=code,
            expected_status=ExternalRequestStatus.SENDING,
        )
        raise PermanentJobError(SAFE_IMPORT_ERROR, code=code) from None

    try:
        batch = _read_and_persist_preview(
            person_import=person_import,
            request=request,
            client=bound_client,
            access_token=access_token,
            mapping=mapping,
        )
    except (_ConsumedImportFailure, _UnsettledPostReadFailure) as exc:
        # Both paths are non-replayable. The first committed UNKNOWN; the
        # second rolled back to SENDING/READING and must be reconciled stale.
        raise PermanentJobError(SAFE_IMPORT_ERROR, code=exc.code) from None
    except FeishuBaseSyncUnavailable as exc:
        # Final safety validation happens inside the read transaction but
        # before list_records. It is therefore still definitely refundable.
        _settle_pre_read_failure(
            person_import=person_import,
            request=request,
            code=exc.code,
            expected_status=ExternalRequestStatus.SENDING,
        )
        raise PermanentJobError(SAFE_IMPORT_ERROR, code=exc.code) from None
    except Exception:
        code = "feishu_local_pre_read_failed"
        _settle_pre_read_failure(
            person_import=person_import,
            request=request,
            code=code,
            expected_status=ExternalRequestStatus.SENDING,
        )
        raise PermanentJobError(SAFE_IMPORT_ERROR, code=code) from None
    return {
        "import_id": str(person_import.id),
        "status": FeishuBasePersonImportStatus.PREVIEWED,
        "batch_id": str(batch.id),
        "total_count": batch.total_count,
        "ready_count": batch.ready_count,
        "invalid_count": batch.invalid_count,
    }


def _claim_person_import_once(*, person_import, request) -> ExternalExecutionRequest:
    """Atomically claim the request and its provider-import ledger."""

    blocked_codes = {
        "environment_execution_disabled",
        "organization_execution_disabled",
        "channel_disabled",
        "target_not_allowlisted",
    }
    claim_error: ExecutionSafetyError | None = None
    with transaction.atomic():
        try:
            locked_request = mark_execution_sending(
                org=person_import.org,
                request_id=request.id,
                expected_status=ExternalRequestStatus.RESERVED,
            )
        except ExecutionSafetyError as exc:
            claim_error = exc
            if exc.code in blocked_codes:
                locked_import = FeishuBasePersonImport.objects.select_for_update().get(
                    org=person_import.org,
                    id=person_import.id,
                    execution_request_id=request.id,
                    status=FeishuBasePersonImportStatus.QUEUED,
                )
                locked_import.status = FeishuBasePersonImportStatus.FAILED
                locked_import.error_code = exc.code
                locked_import.completed_at = timezone.now()
                locked_import.save(
                    update_fields=[
                        "status",
                        "error_code",
                        "completed_at",
                        "updated_at",
                    ]
                )
        else:
            locked_import = (
                FeishuBasePersonImport.objects.select_for_update()
                .filter(
                    org=person_import.org,
                    id=person_import.id,
                    execution_request=locked_request,
                    status=FeishuBasePersonImportStatus.QUEUED,
                )
                .first()
            )
            if locked_import is None:
                raise FeishuBaseSyncUnavailable(
                    "This Feishu Base import was already claimed and cannot be replayed.",
                    code="feishu_execution_not_replayable",
                )
            locked_import.status = FeishuBasePersonImportStatus.READING
            locked_import.attempt_count += 1
            locked_import.started_at = timezone.now()
            locked_import.error_code = ""
            locked_import.save(
                update_fields=[
                    "status",
                    "attempt_count",
                    "started_at",
                    "error_code",
                    "updated_at",
                ]
            )
    if claim_error is not None:
        if claim_error.code in blocked_codes:
            raise claim_error
        raise FeishuBaseSyncUnavailable(
            "This Feishu Base execution was already claimed and cannot be replayed.",
            code="feishu_execution_not_replayable",
        ) from claim_error
    return locked_request


def _read_and_persist_preview(*, person_import, request, client, access_token, mapping):
    """Read one bounded snapshot and atomically settle its local preview."""

    provider_read_entered = False
    failure_code = "feishu_import_read_failed"
    consumed_failure: _ConsumedImportFailure | None = None
    batch = None
    try:
        with transaction.atomic():
            org_control = OrganizationExecutionControl.objects.select_for_update().filter(
                org=person_import.org
            ).first()
            channel_control = ChannelExecutionControl.objects.select_for_update().filter(
                org=person_import.org,
                channel=ExecutionChannel.FEISHU,
            ).first()
            locked_request = ExternalExecutionRequest.objects.select_for_update().get(
                org=person_import.org,
                id=request.id,
            )
            locked_import = FeishuBasePersonImport.objects.select_for_update().get(
                org=person_import.org,
                id=person_import.id,
                execution_request=locked_request,
            )
            connection = FeishuBaseConnection.objects.select_for_update().get(
                org=person_import.org,
                id=locked_import.connection_id,
            )
            if not getattr(settings, "REAL_CHANNEL_EXECUTION_ENABLED", False):
                raise FeishuBaseSyncUnavailable(
                    "Provider execution was disabled before the read.",
                    code="environment_execution_disabled",
                )
            if org_control is None or not org_control.enabled:
                raise FeishuBaseSyncUnavailable(
                    "Organization execution was disabled before the read.",
                    code="organization_execution_disabled",
                )
            if channel_control is None or not channel_control.enabled:
                raise FeishuBaseSyncUnavailable(
                    "Feishu execution was disabled before the read.",
                    code="channel_disabled",
                )
            if channel_control.test_mode and not (
                ChannelTestTarget.objects.select_for_update()
                .filter(
                    org=person_import.org,
                    channel=ExecutionChannel.FEISHU,
                    identifier_hash=locked_request.target_hash,
                    is_active=True,
                )
                .exists()
            ):
                raise FeishuBaseSyncUnavailable(
                    "The Feishu test target was disabled before the read.",
                    code="target_not_allowlisted",
                )
            current_mapping = validate_person_import_mapping(locked_import.get_mapping())
            intent = feishu_person_import_execution_intent(
                connection=connection,
                mapping=current_mapping,
                row_limit=locked_import.row_limit,
            )
            _assert_request_scope(request=locked_request, intent=intent)
            if (
                locked_request.status != ExternalRequestStatus.SENDING
                or locked_import.status != FeishuBasePersonImportStatus.READING
                or current_mapping != mapping
                or locked_import.mapping_sha256 != _sha256(current_mapping)
                or locked_import.destination_sha256 != _destination_sha256(connection)
                or locked_import.source_namespace != _source_namespace(connection)
            ):
                raise FeishuBaseSyncUnavailable(
                    "The Feishu Base import snapshot changed before the read.",
                    code="feishu_execution_snapshot_changed",
                )

            try:
                # This savepoint removes any partial preview/acceptance writes
                # while leaving the outer transaction usable for UNKNOWN
                # settlement. BaseException intentionally escapes both levels.
                with transaction.atomic():
                    provider_read_entered = True
                    provider_records = client.list_records(
                        access_token=access_token,
                        app_token=connection.app_token,
                        table_id=connection.table_id,
                        limit=locked_import.row_limit,
                    )
                    failure_code = "feishu_import_preview_persistence_failed"
                    records = [
                        _provider_person_record(
                            record=record,
                            mapping=mapping,
                            destination_sha256=locked_import.destination_sha256,
                            org_id=locked_import.org_id,
                        )
                        for record in provider_records
                    ]
                    actor = locked_import.requested_by
                    if actor.org_id != locked_import.org_id:
                        raise FeishuBaseSyncUnavailable(
                            "The Feishu Base import requester belongs to another organization.",
                            code="import_actor_org_conflict",
                        )
                    result = preview_provider_person_import(
                        org=person_import.org,
                        requested_by=actor,
                        idempotency_key=locked_request.id,
                        source=EvidenceSource.FEISHU,
                        source_namespace=locked_import.source_namespace,
                        records=records,
                    )
                    batch = result.batch
                    locked_import.import_batch = batch
                    locked_import.status = FeishuBasePersonImportStatus.PREVIEWED
                    locked_import.total_count = batch.total_count
                    locked_import.ready_count = batch.ready_count
                    locked_import.invalid_count = batch.invalid_count
                    locked_import.error_code = ""
                    locked_import.completed_at = timezone.now()
                    locked_import.full_clean()
                    locked_import.save(
                        update_fields=[
                            "import_batch",
                            "status",
                            "total_count",
                            "ready_count",
                            "invalid_count",
                            "error_code",
                            "completed_at",
                            "updated_at",
                        ]
                    )
                    accepted_request = mark_provider_accepted(
                        org=person_import.org,
                        request_id=locked_request.id,
                    )
                    if accepted_request.status != ExternalRequestStatus.ACCEPTED:
                        raise FeishuBaseSyncUnavailable(
                            "The Feishu Base import could not be accepted safely.",
                            code="feishu_execution_not_replayable",
                        )
                    delivered_request = mark_execution_delivered(
                        org=person_import.org,
                        request_id=locked_request.id,
                    )
                    if delivered_request.status != ExternalRequestStatus.DELIVERED:
                        raise FeishuBaseSyncUnavailable(
                            "The Feishu Base import could not be delivered safely.",
                            code="feishu_execution_not_replayable",
                        )
            except Exception as exc:
                if isinstance(exc, FeishuBaseAPIError):
                    failure_code = exc.error_code
                settled_request = mark_provider_accepted(
                    org=person_import.org,
                    request_id=locked_request.id,
                    local_state_uncertain=True,
                )
                if settled_request.status != ExternalRequestStatus.UNKNOWN:
                    raise FeishuBaseSyncUnavailable(
                        "The Feishu Base import outcome could not be settled safely.",
                        code="feishu_import_settlement_failed",
                    )
                locked_import.status = FeishuBasePersonImportStatus.UNKNOWN
                locked_import.error_code = str(failure_code)[:80]
                locked_import.completed_at = timezone.now()
                locked_import.save(
                    update_fields=[
                        "status",
                        "error_code",
                        "completed_at",
                        "updated_at",
                    ]
                )
                consumed_failure = _ConsumedImportFailure(failure_code)
    except Exception:
        if provider_read_entered:
            # The transaction rolled back after provider I/O. Never refund or
            # replay; stale-SENDING reconciliation is the only safe recovery.
            raise _UnsettledPostReadFailure(failure_code) from None
        raise
    if consumed_failure is not None:
        raise consumed_failure
    return batch


def _provider_person_record(*, record, mapping, destination_sha256, org_id):
    values = {
        key: _safe_scalar(record.fields.get(field_name))
        for key, field_name in mapping.items()
        if key != "observed_at"
    }
    first_name = values.get("first_name", "")[:120]
    last_name = values.get("last_name", "")[:120]
    display_name = values.get("display_name", "")[:255]
    if not display_name:
        display_name = " ".join(value for value in (first_name, last_name) if value)[:255]
    observed_at = None
    if "observed_at" in mapping:
        observed_at = _safe_observed_at(record.fields.get(mapping["observed_at"]))
    source_record_id = hash_feishu_record_identifier(
        org_id=org_id,
        record_id=f"{destination_sha256}:{record.record_id}",
    )
    return ProviderPersonRecord(
        source_record_id=source_record_id,
        display_name=display_name,
        first_name=first_name,
        last_name=last_name,
        current_title=values.get("current_title", "")[:255],
        current_company=values.get("current_company", "")[:255],
        location=values.get("location", "")[:255],
        email=values.get("email", "")[:500],
        phone=values.get("phone", "")[:500],
        linkedin=values.get("linkedin", "")[:500],
        evidence_summary=(
            values.get("evidence_summary", "")[:5000] or "Feishu Base profile"
        ),
        observed_at=observed_at,
    )


def _safe_scalar(value: Any, *, depth: int = 0) -> str:
    if value is None or depth > 2:
        return ""
    if isinstance(value, str):
        return value.strip()[:MAX_FEISHU_VALUE_CHARS]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)[:MAX_FEISHU_VALUE_CHARS]
    if isinstance(value, Mapping):
        for key in ("text", "name", "link"):
            if key in value:
                return _safe_scalar(value[key], depth=depth + 1)
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_safe_scalar(item, depth=depth + 1) for item in value[:20]]
        return "; ".join(part for part in parts if part)[:MAX_FEISHU_VALUE_CHARS]
    return ""


def _safe_observed_at(value: Any):
    if isinstance(value, (int, float)):
        try:
            seconds = float(value) / 1000 if abs(float(value)) > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(seconds, tz=datetime_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return str(value)
    cleaned = _safe_scalar(value)
    if not cleaned:
        return None
    parsed = parse_datetime(cleaned)
    return parsed or cleaned


def _validate_provider_mapping(mapping, provider_fields) -> None:
    by_name: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    for item in provider_fields:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("field_name") or "").strip()
        if not name:
            continue
        if name in by_name:
            duplicates.add(name)
        by_name[name] = item
    if duplicates:
        raise FeishuBaseConfigurationError("Feishu Base contains duplicate field names.")
    for key, field_name in mapping.items():
        item = by_name.get(field_name)
        if item is None:
            raise FeishuBaseConfigurationError("A mapped Feishu Base field is missing.")
        try:
            field_type = int(item.get("type"))
        except (TypeError, ValueError) as exc:
            raise FeishuBaseConfigurationError(
                "A mapped Feishu Base field has an invalid type."
            ) from exc
        if field_type not in _EXPECTED_IMPORT_FIELD_TYPES[key]:
            raise FeishuBaseConfigurationError(
                "A mapped Feishu Base field has an unsupported type."
            )


def _settle_pre_read_failure(
    *,
    person_import,
    request,
    code: str,
    expected_status: str,
) -> None:
    """Refund a definitely pre-read failure and project its ledger atomically."""

    with transaction.atomic():
        settled_request = release_execution(
            org=person_import.org,
            request_id=request.id,
            error_code=str(code)[:80],
            expected_status=expected_status,
        )
        locked_import = FeishuBasePersonImport.objects.select_for_update().get(
            org=person_import.org,
            id=person_import.id,
            execution_request_id=request.id,
        )
        if locked_import.status not in {
            FeishuBasePersonImportStatus.QUEUED,
            FeishuBasePersonImportStatus.READING,
            FeishuBasePersonImportStatus.FAILED,
        }:
            raise FeishuBaseSyncUnavailable(
                "The Feishu Base import cannot be refunded in its current state.",
                code="feishu_execution_not_replayable",
            )
        locked_import.status = FeishuBasePersonImportStatus.FAILED
        locked_import.error_code = str(
            settled_request.error_code or code or "feishu_import_failed"
        )[:80]
        locked_import.completed_at = timezone.now()
        locked_import.save(
            update_fields=[
                "status",
                "error_code",
                "completed_at",
                "updated_at",
            ]
        )


def _assert_request_scope(*, request, intent) -> None:
    if (
        request.channel != intent.channel
        or request.action != intent.action
        or request.target_hash != intent.target_hash
        or request.payload_hash != intent.payload_hash
        or request.units != intent.units
    ):
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base import snapshot changed.",
            code="feishu_execution_snapshot_changed",
        )


def _destination_sha256(connection: FeishuBaseConnection) -> str:
    return _sha256(
        {
            "connection_id": str(connection.id),
            "app_token": connection.app_token,
            "table_id": connection.table_id,
        }
    )


def _source_namespace(connection: FeishuBaseConnection) -> str:
    digest = hash_feishu_record_identifier(
        org_id=connection.org_id,
        record_id=f"destination:{_destination_sha256(connection)}",
    )
    return f"feishu:base:{digest[:32]}"


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _client() -> FeishuBaseClient:
    return FeishuBaseClient(
        base_url=settings.FEISHU_OPEN_API_BASE_URL,
        timeout=settings.FEISHU_OPEN_API_TIMEOUT,
    )
