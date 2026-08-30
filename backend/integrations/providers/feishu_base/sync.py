"""Approval-gated, one-attempt synchronization with Feishu Base."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job
from integrations.execution_safety import (
    ExecutionSafetyError,
    hash_target_identifier,
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
    FeishuBaseSync,
    FeishuBaseSyncStatus,
    OrganizationExecutionControl,
)
from integrations.providers.feishu_base.client import (
    DATE_TIME,
    FEISHU_DELETE_RESEARCH_ACTION,
    FEISHU_SYNC_RESEARCH_ACTION,
    FEISHU_VALIDATE_SCHEMA_ACTION,
    URL,
    FeishuBaseAPIError,
    FeishuBaseClient,
    FeishuBaseConfigurationError,
    validate_field_mapping,
)
from sdr.compliance import PROVENANCE_PROCESSING_RESTRICTIONS, intake_data_restriction
from sdr.models import (
    LeadInspection,
    LeadIntake,
    LeadIntakeStatus,
    SDRDataProvenance,
)

logger = logging.getLogger(__name__)

FEISHU_BASE_SYNC_JOB = "integrations.feishu_base_sync"
LEGACY_FEISHU_BASE_SYNC_JOB = "feishu_base.sync_research_result"
MAX_TEXT_LENGTH = 20_000
SAFE_PROVIDER_ERROR = "Feishu Base execution did not complete successfully."


class FeishuBaseSyncUnavailable(ValueError):
    def __init__(self, message: str, *, code: str = "feishu_base_sync_unavailable"):
        super().__init__(message)
        self.code = code


class FeishuMutationOutcomeUncertain(RuntimeError):
    """A provider mutation ran but its durable local projection did not finish."""


@dataclass(frozen=True, slots=True)
class FeishuBaseExecutionIntent:
    channel: str
    action: str
    target_hash: str
    payload_hash: str
    units: int = 1
    test_target_identifier: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "action": self.action,
            "target_hash": self.target_hash,
            "payload_hash": self.payload_hash,
            "units": self.units,
            "test_target_identifier": self.test_target_identifier,
        }


def active_feishu_base_connection(*, org_id: UUID) -> FeishuBaseConnection | None:
    return (
        FeishuBaseConnection.objects.filter(org_id=org_id, is_active=True)
        .exclude(app_secret_ciphertext="")
        .first()
    )


def feishu_schema_execution_intent(
    connection: FeishuBaseConnection,
) -> FeishuBaseExecutionIntent:
    _require_ready_connection(connection)
    _require_outbound_mapping(connection)
    return _execution_intent(
        connection=connection,
        action=FEISHU_VALIDATE_SCHEMA_ACTION,
        snapshot={"connection": _connection_snapshot(connection)},
    )


def feishu_research_sync_execution_intent(
    *, intake: LeadIntake, connection: FeishuBaseConnection | None = None
) -> FeishuBaseExecutionIntent:
    if intake.status != LeadIntakeStatus.COMPLETED:
        raise FeishuBaseSyncUnavailable("Only completed intakes can be synchronized.")
    restriction = intake_data_restriction(intake)
    if restriction:
        raise FeishuBaseSyncUnavailable(restriction.reason, code=restriction.code)
    connection = connection or active_feishu_base_connection(org_id=intake.org_id)
    if connection is None:
        raise FeishuBaseSyncUnavailable(
            "Configure and enable a Feishu Base connection first.",
            code="feishu_connection_unavailable",
        )
    _require_ready_connection(connection)
    _require_outbound_mapping(connection)
    return _execution_intent(
        connection=connection,
        action=FEISHU_SYNC_RESEARCH_ACTION,
        snapshot=_research_snapshot(connection=connection, intake=intake),
    )


def feishu_delete_execution_intent(
    sync: FeishuBaseSync,
) -> FeishuBaseExecutionIntent:
    _require_ready_connection(sync.connection)
    if not sync.has_remote_record:
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base sync has no deletable remote record.",
            code="feishu_record_not_found",
        )
    if sync.status not in {
        FeishuBaseSyncStatus.SUCCEEDED,
        FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING,
    }:
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base record is not awaiting deletion.",
            code="feishu_record_not_deletable",
        )
    return _execution_intent(
        connection=sync.connection,
        action=FEISHU_DELETE_RESEARCH_ACTION,
        snapshot={
            "connection": _connection_snapshot(sync.connection),
            "sync_id": str(sync.id),
            "record_id_hash": sync.record_id_hash,
        },
    )


def enqueue_feishu_base_sync(
    *,
    intake: LeadIntake,
    approval_id: UUID | None = None,
    idempotency_key: UUID | None = None,
):
    """Reserve and queue one explicitly approved research-result upsert.

    Legacy adapter calls omit the grant and therefore fail before creating a
    sync row, reserving quota, or persisting an automation job.
    """

    if approval_id is None or idempotency_key is None:
        raise FeishuBaseSyncUnavailable(
            "An exact, single-use Feishu Base approval is required.",
            code="feishu_approval_required",
        )
    # Compute the approval scope without row locks. The exact same scope is
    # recomputed after quota reservation while mutable rows are locked in the
    # global worker order: controls/request -> intake -> provenance -> sync ->
    # connection. This also matches the privacy-governance path.
    intake_snapshot = (
        LeadIntake.objects.select_related("crm_lead", "assigned_profile__user")
        .get(org_id=intake.org_id, id=intake.id)
    )
    connection_snapshot = active_feishu_base_connection(org_id=intake.org_id)
    intent = feishu_research_sync_execution_intent(
        intake=intake_snapshot,
        connection=connection_snapshot,
    )
    with transaction.atomic():
        request = _reserve_intent(
            org=intake_snapshot.org,
            intent=intent,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
        )
        intake = (
            LeadIntake.objects.select_for_update(of=("self",))
            .select_related("crm_lead", "assigned_profile__user")
            .get(org_id=intake_snapshot.org_id, id=intake_snapshot.id)
        )
        SDRDataProvenance.objects.select_for_update().filter(
            org_id=intake_snapshot.org_id,
            intake_id=intake.id,
        ).first()
        sync = (
            FeishuBaseSync.objects.select_for_update()
            .filter(org_id=intake_snapshot.org_id, intake_id=intake_snapshot.id)
            .first()
        )
        connection = FeishuBaseConnection.objects.select_for_update().get(
            org_id=intake_snapshot.org_id,
            id=connection_snapshot.id,
        )
        intent = feishu_research_sync_execution_intent(
            intake=intake,
            connection=connection,
        )
        _assert_request_matches_intent(request=request, intent=intent)
        destination_sha256 = _destination_sha256(connection)
        if sync is not None and sync.status == FeishuBaseSyncStatus.UNKNOWN:
            raise FeishuBaseSyncUnavailable(
                "Resolve the previous unknown Feishu Base outcome first.",
                code="feishu_unknown_outcome",
            )
        _assert_no_conflicting_execution(sync=sync, request=request)
        if (
            sync is not None
            and sync.has_remote_record
            and sync.destination_sha256 != destination_sha256
        ):
            raise FeishuBaseSyncUnavailable(
                "Delete the existing remote record before changing destinations.",
                code="feishu_destination_change_requires_delete",
            )
        if sync is None:
            sync = FeishuBaseSync.objects.create(
                org_id=intake.org_id,
                connection=connection,
                intake=intake,
                execution_request=request,
                status=FeishuBaseSyncStatus.QUEUED,
                destination_sha256=destination_sha256,
                payload_sha256=intent.payload_hash,
            )
        else:
            sync.connection = connection
            sync.execution_request = request
            sync.status = FeishuBaseSyncStatus.QUEUED
            sync.destination_sha256 = destination_sha256
            sync.payload_sha256 = intent.payload_hash
            sync.error_code = ""
            sync.error_message = ""
            sync.failed_at = None
            sync.save(
                update_fields=[
                    "connection",
                    "execution_request",
                    "status",
                    "destination_sha256",
                    "payload_sha256",
                    "error_code",
                    "error_message",
                    "failed_at",
                    "updated_at",
                ]
            )
        enqueued = _enqueue_execution_job(
            org_id=intake.org_id,
            action=intent.action,
            connection_id=connection.id,
            sync_id=sync.id,
            execution_request_id=request.id,
            payload_hash=intent.payload_hash,
        )
    _dispatch_if_new(enqueued)
    return enqueued.job


def enqueue_feishu_schema_validation(
    *, connection: FeishuBaseConnection, approval_id: UUID, idempotency_key: UUID
):
    connection_snapshot = FeishuBaseConnection.objects.get(
        org_id=connection.org_id,
        id=connection.id,
    )
    intent = feishu_schema_execution_intent(connection_snapshot)
    with transaction.atomic():
        request = _reserve_intent(
            org=connection_snapshot.org,
            intent=intent,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
        )
        connection = FeishuBaseConnection.objects.select_for_update().get(
            org_id=connection_snapshot.org_id,
            id=connection_snapshot.id,
        )
        intent = feishu_schema_execution_intent(connection)
        _assert_request_matches_intent(request=request, intent=intent)
        enqueued = _enqueue_execution_job(
            org_id=connection.org_id,
            action=intent.action,
            connection_id=connection.id,
            execution_request_id=request.id,
            payload_hash=intent.payload_hash,
        )
    _dispatch_if_new(enqueued)
    return enqueued.job


def enqueue_feishu_remote_delete(
    *, sync: FeishuBaseSync, approval_id: UUID, idempotency_key: UUID
):
    sync_snapshot = (
            FeishuBaseSync.objects
            .select_related("connection", "intake")
            .get(org_id=sync.org_id, id=sync.id)
    )
    intent = feishu_delete_execution_intent(sync_snapshot)
    with transaction.atomic():
        request = _reserve_intent(
            org=sync_snapshot.org,
            intent=intent,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
        )
        intake = LeadIntake.objects.select_for_update(of=("self",)).get(
            org_id=sync_snapshot.org_id,
            id=sync_snapshot.intake_id,
        )
        SDRDataProvenance.objects.select_for_update().filter(
            org_id=sync_snapshot.org_id,
            intake_id=intake.id,
        ).first()
        sync = FeishuBaseSync.objects.select_for_update().get(
            org_id=sync_snapshot.org_id,
            id=sync_snapshot.id,
            intake_id=intake.id,
        )
        connection = FeishuBaseConnection.objects.select_for_update().get(
            org_id=sync_snapshot.org_id,
            id=sync.connection_id,
        )
        sync.connection = connection
        sync.intake = intake
        intent = feishu_delete_execution_intent(sync)
        _assert_request_matches_intent(request=request, intent=intent)
        _assert_no_conflicting_execution(sync=sync, request=request)
        sync.execution_request = request
        sync.status = FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING
        sync.error_code = ""
        sync.error_message = ""
        sync.failed_at = None
        sync.save(
            update_fields=[
                "execution_request",
                "status",
                "error_code",
                "error_message",
                "failed_at",
                "updated_at",
            ]
        )
        enqueued = _enqueue_execution_job(
            org_id=sync.org_id,
            action=intent.action,
            connection_id=sync.connection_id,
            sync_id=sync.id,
            execution_request_id=request.id,
            payload_hash=intent.payload_hash,
        )
    _dispatch_if_new(enqueued)
    return enqueued.job


def process_feishu_base_sync_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        connection_id = UUID(str(payload["connection_id"]))
        execution_request_id = UUID(str(payload["execution_request_id"]))
        action = str(payload["action"])
        expected_hash = str(payload["payload_sha256"])
        sync_id = UUID(str(payload["sync_id"])) if payload.get("sync_id") else None
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The Feishu Base execution payload is invalid.",
            code="invalid_job_payload",
        ) from exc
    if action not in {
        FEISHU_VALIDATE_SCHEMA_ACTION,
        FEISHU_SYNC_RESEARCH_ACTION,
        FEISHU_DELETE_RESEARCH_ACTION,
    }:
        raise PermanentJobError(
            "The Feishu Base execution action is invalid.",
            code="invalid_job_payload",
        )

    connection = FeishuBaseConnection.objects.filter(
        org_id=org_id, id=connection_id
    ).first()
    request = ExternalExecutionRequest.objects.filter(
        org_id=org_id,
        id=execution_request_id,
        channel=ExecutionChannel.FEISHU,
        action=action,
    ).first()
    sync = None
    if sync_id is not None:
        sync = (
            FeishuBaseSync.objects.filter(org_id=org_id, id=sync_id)
            .select_related(
                "connection",
                "intake__inspection",
                "intake__crm_lead",
                "intake__assigned_profile__user",
            )
            .first()
        )
    if connection is None or request is None:
        _release_orphan_request(
            org_id=org_id,
            request_id=execution_request_id,
            error_code="feishu_execution_state_not_found",
        )
        raise PermanentJobError(
            "The Feishu Base execution state no longer exists.",
            code="feishu_execution_state_not_found",
        )
    if request.status != ExternalRequestStatus.RESERVED:
        raise PermanentJobError(
            "This Feishu Base execution was already attempted and cannot be replayed.",
            code="feishu_execution_not_replayable",
        )
    if action != FEISHU_VALIDATE_SCHEMA_ACTION and (
        sync is None or sync.execution_request_id != request.id
    ):
        _release_request(
            org=connection.org,
            request=request,
            error_code="feishu_execution_binding_changed",
        )
        raise PermanentJobError(
            "The Feishu Base execution binding changed before provider I/O.",
            code="feishu_execution_binding_changed",
        )
    try:
        current_intent = _current_intent(
            action=action,
            connection=connection,
            sync=sync,
        )
    except FeishuBaseSyncUnavailable as exc:
        _release_request(org=connection.org, request=request, error_code=exc.code)
        _mark_preflight_failure(sync, action=action, code=exc.code)
        raise PermanentJobError(str(exc), code=exc.code) from exc
    if (
        request.target_hash != current_intent.target_hash
        or request.payload_hash != current_intent.payload_hash
        or request.payload_hash != expected_hash
        or request.units != current_intent.units
    ):
        _release_request(
            org=connection.org,
            request=request,
            error_code="feishu_execution_snapshot_changed",
        )
        _mark_preflight_failure(
            sync,
            action=action,
            code="feishu_execution_snapshot_changed",
        )
        raise PermanentJobError(
            "The Feishu Base snapshot changed before provider I/O.",
            code="feishu_execution_snapshot_changed",
        )
    try:
        app_secret = connection.get_app_secret()
        record_id = (
            sync.get_record_id()
            if action == FEISHU_DELETE_RESEARCH_ACTION and sync is not None
            else ""
        )
        bound_client = _client().for_execution(
            org=connection.org,
            action=action,
            execution_request_id=request.id,
        )
    except Exception:
        _release_request(
            org=connection.org,
            request=request,
            error_code="feishu_credentials_unavailable",
        )
        _mark_preflight_failure(
            sync,
            action=action,
            code="feishu_credentials_unavailable",
        )
        raise PermanentJobError(
            "Feishu Base credentials are unavailable.",
            code="feishu_credentials_unavailable",
        ) from None

    try:
        request = _claim_execution_once(org=connection.org, request_id=request.id)
    except FeishuBaseSyncUnavailable as exc:
        raise PermanentJobError(str(exc), code=exc.code) from exc
    except ExecutionSafetyError as exc:
        _mark_preflight_failure(sync, action=action, code=exc.code)
        raise PermanentJobError(exc.detail, code=exc.code) from exc
    _mark_attempt_started(sync, action=action)
    try:
        access_token = bound_client.tenant_access_token(
            app_id=connection.app_id,
            app_secret=app_secret,
        )
        if action == FEISHU_VALIDATE_SCHEMA_ACTION:
            return _perform_schema_validation(
                connection=connection,
                request=request,
                client=bound_client,
                access_token=access_token,
            )
        if action == FEISHU_SYNC_RESEARCH_ACTION:
            return _perform_research_sync(
                sync=sync,
                request=request,
                client=bound_client,
                access_token=access_token,
            )
        return _perform_remote_delete(
            sync=sync,
            request=request,
            client=bound_client,
            access_token=access_token,
            record_id=record_id,
        )
    except FeishuBaseSyncUnavailable as exc:
        _release_request(
            org=connection.org,
            request=request,
            error_code=exc.code,
            expected_status=ExternalRequestStatus.SENDING,
        )
        _mark_definite_failure(sync, action=action, code=exc.code)
        raise PermanentJobError(str(exc), code=exc.code) from exc
    except FeishuBaseConfigurationError as exc:
        _release_request(
            org=connection.org,
            request=request,
            error_code="feishu_field_mapping_invalid",
            expected_status=ExternalRequestStatus.SENDING,
        )
        _mark_definite_failure(
            sync,
            action=action,
            code="feishu_field_mapping_invalid",
        )
        raise PermanentJobError(
            "The Feishu Base field mapping is invalid.",
            code="feishu_field_mapping_invalid",
        ) from exc
    except FeishuBaseAPIError as exc:
        if _is_definite_rejection(exc):
            _release_request(
                org=connection.org,
                request=request,
                error_code=exc.error_code,
                expected_status=ExternalRequestStatus.SENDING,
            )
            _mark_definite_failure(sync, action=action, code=exc.error_code)
        else:
            _mark_unknown(sync=sync, request=request)
        raise PermanentJobError(SAFE_PROVIDER_ERROR, code=exc.error_code) from None
    except FeishuMutationOutcomeUncertain:
        # UNKNOWN settlement is deliberately best-effort. Once a provider
        # mutation has started, even a second local database failure must never
        # fall through to the pre-mutation refund path.
        try:
            _mark_unknown(sync=sync, request=request)
        except Exception:
            logger.exception(
                "Could not persist uncertain Feishu Base execution %s",
                request.id,
            )
        raise PermanentJobError(
            SAFE_PROVIDER_ERROR,
            code="feishu_local_persistence_uncertain",
        ) from None
    except Exception:
        _release_request(
            org=connection.org,
            request=request,
            error_code="feishu_local_pre_mutation_failed",
            expected_status=ExternalRequestStatus.SENDING,
        )
        _mark_definite_failure(
            sync,
            action=action,
            code="feishu_local_pre_mutation_failed",
        )
        raise PermanentJobError(
            SAFE_PROVIDER_ERROR,
            code="feishu_local_pre_mutation_failed",
        ) from None


def _perform_schema_validation(
    *, connection, request, client, access_token
) -> Mapping[str, Any]:
    fields = client.list_fields(
        access_token=access_token,
        app_token=connection.app_token,
        table_id=connection.table_id,
    )
    validate_field_mapping(connection.field_mapping, fields)
    validated_at = timezone.now()
    try:
        with transaction.atomic():
            mark_provider_accepted(org=connection.org, request_id=request.id)
            FeishuBaseConnection.objects.filter(
                org=connection.org, id=connection.id
            ).update(last_validated_at=validated_at)
            mark_execution_delivered(org=connection.org, request_id=request.id)
    except Exception as exc:
        raise FeishuBaseSyncUnavailable(
            "The schema validation result could not be stored locally.",
            code="feishu_local_persistence_failed",
        ) from exc
    return {
        "action": FEISHU_VALIDATE_SCHEMA_ACTION,
        "status": "succeeded",
        "valid": True,
        "field_count": len(fields),
        "mapped_field_count": len(connection.field_mapping),
        "validated_at": validated_at.isoformat(),
    }


def _perform_research_sync(
    *, sync: FeishuBaseSync, request, client, access_token
) -> Mapping[str, Any]:
    connection = sync.connection
    provider_fields = client.list_fields(
        access_token=access_token,
        app_token=connection.app_token,
        table_id=connection.table_id,
    )
    validate_field_mapping(connection.field_mapping, provider_fields)
    fields = _render_fields(
        connection.field_mapping,
        _canonical_values(sync.intake),
        provider_fields,
    )
    record = client.find_record_by_field(
        access_token=access_token,
        app_token=connection.app_token,
        table_id=connection.table_id,
        field_name=connection.field_mapping["intake_id"],
        value=str(sync.intake_id),
    )
    mutation_started = False
    try:
        with transaction.atomic():
            locked = _lock_and_revalidate_mutation(
                sync=sync,
                request=request,
                action=FEISHU_SYNC_RESEARCH_ACTION,
            )
            locked_connection = locked.connection
            mutation_started = True
            if record is None:
                record = client.create_record(
                    access_token=access_token,
                    app_token=locked_connection.app_token,
                    table_id=locked_connection.table_id,
                    fields=fields,
                )
            else:
                record = client.update_record(
                    access_token=access_token,
                    app_token=locked_connection.app_token,
                    table_id=locked_connection.table_id,
                    record_id=record.record_id,
                    fields=fields,
                )
            synced_at = timezone.now()
            locked.set_record_id(record.record_id)
            locked.status = FeishuBaseSyncStatus.SUCCEEDED
            locked.synced_field_names = sorted(fields)
            locked.error_code = ""
            locked.error_message = ""
            locked.failed_at = None
            locked.synced_at = synced_at
            locked.save(
                update_fields=[
                    "record_id_ciphertext",
                    "record_id_hash",
                    "record_safe_label",
                    "status",
                    "synced_field_names",
                    "error_code",
                    "error_message",
                    "failed_at",
                    "synced_at",
                    "updated_at",
                ]
            )
            FeishuBaseConnection.objects.filter(
                org=sync.org, id=connection.id
            ).update(last_sync_at=synced_at)
            mark_provider_accepted(
                org=sync.org,
                request_id=request.id,
                provider_reference=locked.record_safe_label,
            )
            mark_execution_delivered(org=sync.org, request_id=request.id)
    except (
        FeishuBaseSyncUnavailable,
        FeishuBaseAPIError,
        FeishuBaseConfigurationError,
    ):
        raise
    except Exception as exc:
        if mutation_started:
            raise FeishuMutationOutcomeUncertain from exc
        raise
    return _result(sync_id=sync.id, intake_id=sync.intake_id, status="succeeded")


def _perform_remote_delete(
    *, sync: FeishuBaseSync, request, client, access_token, record_id: str
) -> Mapping[str, Any]:
    mutation_started = False
    try:
        with transaction.atomic():
            locked = _lock_and_revalidate_mutation(
                sync=sync,
                request=request,
                action=FEISHU_DELETE_RESEARCH_ACTION,
            )
            locked_connection = locked.connection
            current_record_id = locked.get_record_id()
            if current_record_id != record_id:
                raise FeishuBaseSyncUnavailable(
                    "The Feishu Base deletion snapshot changed before provider I/O.",
                    code="feishu_execution_snapshot_changed",
                )
            mutation_started = True
            client.delete_record(
                access_token=access_token,
                app_token=locked_connection.app_token,
                table_id=locked_connection.table_id,
                record_id=current_record_id,
            )
            safe_reference = locked.record_safe_label
            locked.clear_record_id()
            locked.status = FeishuBaseSyncStatus.EXTERNAL_ERASURE_COMPLETED
            locked.synced_field_names = []
            locked.error_code = ""
            locked.error_message = ""
            locked.failed_at = None
            locked.save(
                update_fields=[
                    "record_id_ciphertext",
                    "record_id_hash",
                    "record_safe_label",
                    "status",
                    "synced_field_names",
                    "error_code",
                    "error_message",
                    "failed_at",
                    "updated_at",
                ]
            )
            mark_provider_accepted(
                org=sync.org,
                request_id=request.id,
                provider_reference=safe_reference,
            )
            mark_execution_delivered(org=sync.org, request_id=request.id)
    except (
        FeishuBaseSyncUnavailable,
        FeishuBaseAPIError,
        FeishuBaseConfigurationError,
    ):
        raise
    except Exception as exc:
        if mutation_started:
            raise FeishuMutationOutcomeUncertain from exc
        raise
    return _result(
        sync_id=sync.id,
        intake_id=sync.intake_id,
        status=FeishuBaseSyncStatus.EXTERNAL_ERASURE_COMPLETED,
    )


def _client() -> FeishuBaseClient:
    return FeishuBaseClient(
        base_url=settings.FEISHU_OPEN_API_BASE_URL,
        timeout=settings.FEISHU_OPEN_API_TIMEOUT,
    )


def _require_ready_connection(connection: FeishuBaseConnection | None) -> None:
    if connection is None or not connection.is_active:
        raise FeishuBaseSyncUnavailable(
            "Configure and enable a Feishu Base connection first.",
            code="feishu_connection_inactive",
        )
    if not all(
        (
            connection.app_id,
            connection.app_secret_ciphertext,
            connection.app_token,
            connection.table_id,
        )
    ):
        raise FeishuBaseSyncUnavailable(
            "Complete the Feishu Base credentials and field mapping first.",
            code="feishu_connection_incomplete",
        )


def _require_outbound_mapping(connection: FeishuBaseConnection) -> None:
    if not connection.field_mapping.get("intake_id"):
        raise FeishuBaseSyncUnavailable(
            "Map intake_id before using the outbound Feishu Base workflow.",
            code="feishu_outbound_mapping_incomplete",
        )


def _execution_intent(
    *, connection: FeishuBaseConnection, action: str, snapshot: Mapping[str, Any]
) -> FeishuBaseExecutionIntent:
    target_identifier = f"feishu-base:{connection.id}"
    return FeishuBaseExecutionIntent(
        channel=ExecutionChannel.FEISHU,
        action=action,
        target_hash=hash_target_identifier(
            org=connection.org,
            channel=ExecutionChannel.FEISHU,
            identifier=target_identifier,
        ),
        payload_hash=_sha256(snapshot),
        test_target_identifier=target_identifier,
    )


def _reserve_intent(
    *, org, intent, approval_id: UUID, idempotency_key: UUID
) -> ExternalExecutionRequest:
    try:
        reservation = reserve_execution(
            org=org,
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
    if reservation.replayed and request.status != ExternalRequestStatus.RESERVED:
        raise FeishuBaseSyncUnavailable(
            "This Feishu Base execution was already attempted and cannot be replayed.",
            code="feishu_execution_not_replayable",
        )
    return request


def _claim_execution_once(*, org, request_id: UUID) -> ExternalExecutionRequest:
    """Serialize worker claims so a durable request can enter provider I/O once."""

    with transaction.atomic():
        # Match every settlement/release path's global lock order. Acquiring the
        # request first here would deadlock against quota settlement.
        OrganizationExecutionControl.objects.select_for_update().filter(
            org=org
        ).first()
        ChannelExecutionControl.objects.select_for_update().filter(
            org=org,
            channel=ExecutionChannel.FEISHU,
        ).first()
        request = ExternalExecutionRequest.objects.select_for_update().get(
            org=org,
            id=request_id,
        )
        if request.status != ExternalRequestStatus.RESERVED:
            raise FeishuBaseSyncUnavailable(
                "This Feishu Base execution was already claimed and cannot be replayed.",
                code="feishu_execution_not_replayable",
            )
        return mark_execution_sending(org=org, request_id=request.id)


def _assert_no_conflicting_execution(*, sync, request) -> None:
    if sync is None or sync.execution_request_id in {None, request.id}:
        return
    existing_status = ExternalExecutionRequest.objects.filter(
        org_id=sync.org_id,
        id=sync.execution_request_id,
    ).values_list("status", flat=True).first()
    if existing_status in {
        ExternalRequestStatus.RESERVED,
        ExternalRequestStatus.SENDING,
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.UNKNOWN,
    }:
        raise FeishuBaseSyncUnavailable(
            "A Feishu Base execution is already active or awaiting reconciliation.",
            code="feishu_execution_in_flight",
        )


def _assert_request_matches_intent(*, request, intent) -> None:
    if (
        request.channel != intent.channel
        or request.action != intent.action
        or request.target_hash != intent.target_hash
        or request.payload_hash != intent.payload_hash
        or request.units != intent.units
    ):
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base snapshot changed while execution was reserved.",
            code="feishu_execution_snapshot_changed",
        )


def _lock_and_revalidate_mutation(*, sync, request, action) -> FeishuBaseSync:
    """Lock every mutable snapshot owner immediately before remote mutation."""

    try:
        org_control = OrganizationExecutionControl.objects.select_for_update().get(
            org=sync.org
        )
        channel_control = ChannelExecutionControl.objects.select_for_update().get(
            org=sync.org,
            channel=ExecutionChannel.FEISHU,
        )
        locked_request = ExternalExecutionRequest.objects.select_for_update().get(
            org=sync.org,
            id=request.id,
        )
        intake = (
            LeadIntake.objects.select_for_update(of=("self",))
            .select_related("crm_lead", "assigned_profile__user")
            .get(org=sync.org, id=sync.intake_id)
        )
        provenance = (
            SDRDataProvenance.objects.select_for_update()
            .filter(org=sync.org, intake_id=intake.id)
            .first()
        )
        locked = FeishuBaseSync.objects.select_for_update().get(
            org=sync.org,
            id=sync.id,
            intake_id=intake.id,
            execution_request=request,
        )
        connection = FeishuBaseConnection.objects.select_for_update().get(
            org=sync.org,
            id=locked.connection_id,
        )
    except (
        OrganizationExecutionControl.DoesNotExist,
        ChannelExecutionControl.DoesNotExist,
        ExternalExecutionRequest.DoesNotExist,
        FeishuBaseSync.DoesNotExist,
        FeishuBaseConnection.DoesNotExist,
        LeadIntake.DoesNotExist,
    ) as exc:
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base mutation snapshot no longer exists.",
            code="feishu_execution_snapshot_changed",
        ) from exc
    locked.connection = connection
    locked.intake = intake
    if not getattr(settings, "REAL_CHANNEL_EXECUTION_ENABLED", False):
        raise FeishuBaseSyncUnavailable(
            "Provider execution was disabled before the remote mutation.",
            code="environment_execution_disabled",
        )
    if not org_control.enabled:
        raise FeishuBaseSyncUnavailable(
            "Organization execution was disabled before the remote mutation.",
            code="organization_execution_disabled",
        )
    if not channel_control.enabled:
        raise FeishuBaseSyncUnavailable(
            "Feishu execution was disabled before the remote mutation.",
            code="channel_disabled",
        )
    if channel_control.test_mode:
        target = (
            ChannelTestTarget.objects.select_for_update()
            .filter(
                org=sync.org,
                channel=ExecutionChannel.FEISHU,
                identifier_hash=locked_request.target_hash,
                is_active=True,
            )
            .first()
        )
        if target is None:
            raise FeishuBaseSyncUnavailable(
                "The Feishu test target was disabled before the remote mutation.",
                code="target_not_allowlisted",
            )
    if action == FEISHU_SYNC_RESEARCH_ACTION and provenance is not None:
        restriction = PROVENANCE_PROCESSING_RESTRICTIONS.get(provenance.status)
        if restriction is not None:
            raise FeishuBaseSyncUnavailable(
                restriction.reason,
                code=restriction.code,
            )
    intent = _current_intent(
        action=action,
        connection=connection,
        sync=locked,
    )
    if (
        locked_request.status != ExternalRequestStatus.SENDING
        or locked_request.target_hash != intent.target_hash
        or locked_request.payload_hash != intent.payload_hash
        or locked_request.units != intent.units
    ):
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base snapshot changed immediately before provider mutation.",
            code="feishu_execution_snapshot_changed",
        )
    return locked


def _enqueue_execution_job(
    *,
    org_id,
    action,
    connection_id,
    execution_request_id,
    payload_hash,
    sync_id=None,
):
    enqueued = enqueue_job(
        JobRequest(
            org_id=org_id,
            name=FEISHU_BASE_SYNC_JOB,
            idempotency_key=f"feishu-base:{action}:{execution_request_id}",
            payload={
                "org_id": str(org_id),
                "action": action,
                "connection_id": str(connection_id),
                "sync_id": str(sync_id) if sync_id else "",
                "execution_request_id": str(execution_request_id),
                "payload_sha256": payload_hash,
            },
            max_attempts=1,
        )
    )
    if enqueued.job.status in {
        AutomationJobStatus.DEAD_LETTER,
        AutomationJobStatus.CANCELLED,
    }:
        raise FeishuBaseSyncUnavailable(
            "A terminal Feishu Base job cannot be replayed; issue a new approval.",
            code="feishu_execution_not_replayable",
        )
    return enqueued


def _dispatch_if_new(enqueued) -> None:
    if not enqueued.terminal_replay:
        try:
            dispatch_job(enqueued.job)
        except Exception:
            logger.exception(
                "Could not dispatch durable Feishu Base job %s", enqueued.job.id
            )


def _current_intent(*, action, connection, sync):
    if action == FEISHU_VALIDATE_SCHEMA_ACTION:
        return feishu_schema_execution_intent(connection)
    if sync is None or sync.connection_id != connection.id:
        raise FeishuBaseSyncUnavailable(
            "The Feishu Base sync binding is invalid.",
            code="feishu_execution_binding_changed",
        )
    if action == FEISHU_SYNC_RESEARCH_ACTION:
        return feishu_research_sync_execution_intent(
            intake=sync.intake,
            connection=connection,
        )
    return feishu_delete_execution_intent(sync)


def _connection_snapshot(connection: FeishuBaseConnection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "app_id": connection.app_id,
        "credential_sha256": hashlib.sha256(
            connection.app_secret_ciphertext.encode("utf-8")
        ).hexdigest(),
        "app_token": connection.app_token,
        "table_id": connection.table_id,
        "field_mapping": connection.field_mapping,
        "configuration_version": connection.updated_at.isoformat(),
    }


def _destination_sha256(connection: FeishuBaseConnection) -> str:
    return _sha256(
        {
            "app_token": connection.app_token,
            "table_id": connection.table_id,
        }
    )


def _research_snapshot(
    *, connection: FeishuBaseConnection, intake: LeadIntake
) -> dict[str, Any]:
    return {
        "connection": _connection_snapshot(connection),
        "destination": _destination_sha256(connection),
        "values": _canonical_values(intake),
    }


def _release_request(
    *, org, request, error_code: str, expected_status: str | None = None
) -> None:
    try:
        release_execution(
            org=org,
            request_id=request.id,
            error_code=error_code,
            expected_status=expected_status,
        )
    except Exception:
        logger.exception("Could not release Feishu Base execution %s", request.id)


def _release_orphan_request(*, org_id, request_id, error_code) -> None:
    request = (
        ExternalExecutionRequest.objects.filter(
            org_id=org_id,
            id=request_id,
            channel=ExecutionChannel.FEISHU,
            status=ExternalRequestStatus.RESERVED,
        )
        .select_related("org")
        .first()
    )
    if request is not None:
        _release_request(org=request.org, request=request, error_code=error_code)


def _mark_attempt_started(sync, *, action: str) -> None:
    if sync is None:
        return
    values = {
        "attempt_count": sync.attempt_count + 1,
        "last_attempted_at": timezone.now(),
        "error_code": "",
        "error_message": "",
        "failed_at": None,
    }
    if action == FEISHU_SYNC_RESEARCH_ACTION:
        values["status"] = FeishuBaseSyncStatus.SYNCING
    FeishuBaseSync.objects.filter(org=sync.org, id=sync.id).update(**values)


def _mark_preflight_failure(sync, *, action: str, code: str) -> None:
    if sync is None:
        return
    status_value = (
        FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING
        if action == FEISHU_DELETE_RESEARCH_ACTION
        else FeishuBaseSyncStatus.FAILED
    )
    FeishuBaseSync.objects.filter(org=sync.org, id=sync.id).update(
        status=status_value,
        error_code=code[:80],
        error_message=SAFE_PROVIDER_ERROR,
        failed_at=timezone.now(),
    )


def _mark_definite_failure(sync, *, action: str, code: str) -> None:
    _mark_preflight_failure(sync, action=action, code=code)


def _mark_unknown(*, sync, request) -> None:
    try:
        mark_provider_accepted(
            org=request.org,
            request_id=request.id,
            local_state_uncertain=True,
        )
    except Exception:
        logger.exception("Could not mark Feishu Base execution %s UNKNOWN", request.id)
    if sync is not None:
        try:
            FeishuBaseSync.objects.filter(org=sync.org, id=sync.id).update(
                status=FeishuBaseSyncStatus.UNKNOWN,
                error_code="feishu_outcome_unknown",
                error_message="Provider outcome requires manual reconciliation.",
                failed_at=timezone.now(),
            )
        except Exception:
            logger.exception(
                "Could not project Feishu Base sync %s to UNKNOWN",
                sync.id,
            )


def _is_definite_rejection(exc: FeishuBaseAPIError) -> bool:
    if not exc.mutation_attempted:
        return True
    if exc.status_code is not None:
        return 400 <= exc.status_code < 500 and exc.status_code not in {408, 425, 429}
    return exc.provider_code is not None and not exc.retryable


def _canonical_values(intake: LeadIntake) -> dict[str, Any]:
    inspection = LeadInspection.objects.filter(
        org_id=intake.org_id,
        intake_id=intake.id,
    ).first()
    lead = intake.crm_lead
    identity = intake.normalized_payload.get("identity", {})
    company = intake.normalized_payload.get("company", {})
    raw = intake.raw_payload
    first_name = (
        getattr(lead, "first_name", "") or identity.get("first_name") or ""
    ).strip()
    last_name = (
        getattr(lead, "last_name", "") or identity.get("last_name") or ""
    ).strip()
    contact_name = " ".join(part for part in (first_name, last_name) if part)
    assigned_sales = ""
    if intake.assigned_profile_id:
        assigned_sales = (
            intake.assigned_profile.user.name
            or intake.assigned_profile.user.email
            or str(intake.assigned_profile_id)
        )
    processed_at = (
        int(intake.processed_at.timestamp() * 1000) if intake.processed_at else None
    )
    values = {
        "intake_id": str(intake.id),
        "company_name": (
            getattr(lead, "company_name", "")
            or company.get("name")
            or raw.get("company_name")
            or ""
        ),
        "contact_name": contact_name,
        "email": (
            getattr(lead, "email", "")
            or identity.get("email")
            or raw.get("email")
            or ""
        ),
        "phone": (
            getattr(lead, "phone", "")
            or identity.get("phone")
            or raw.get("phone")
            or ""
        ),
        "linkedin_url": (
            getattr(lead, "linkedin_url", "")
            or identity.get("linkedin_url")
            or raw.get("linkedin_url")
            or ""
        ),
        "website": (
            getattr(lead, "website", "")
            or (inspection.website_url if inspection else "")
            or company.get("website")
            or raw.get("website")
            or ""
        ),
        "source": intake.source,
        "source_record_id": intake.source_record_id,
        "research_summary": inspection.research_summary if inspection else "",
        "research_facts": _json_text(inspection.research_facts if inspection else {}),
        "source_urls": _json_text(inspection.source_urls if inspection else []),
        "qualification_score": (
            inspection.qualification_score
            if inspection and inspection.qualification_score is not None
            else intake.qualification_score
        ),
        "qualification_band": (
            inspection.qualification_band
            if inspection and inspection.qualification_band
            else intake.qualification_band
        ),
        "qualification_reasons": _json_text(
            inspection.qualification_reasons if inspection else []
        ),
        "assigned_sales": assigned_sales,
        "routing_reason": intake.routing_reason,
        "crm_lead_id": str(intake.crm_lead_id or ""),
        "processed_at": processed_at,
        "inspection_status": inspection.status if inspection else "rules_only",
    }
    return {
        key: _truncate(value) if isinstance(value, str) else value
        for key, value in values.items()
    }


def _render_fields(
    mapping: Mapping[str, str],
    canonical_values: Mapping[str, Any],
    provider_fields: list[Mapping[str, Any]],
) -> dict[str, Any]:
    type_by_name = {
        str(field.get("field_name") or ""): int(field.get("type"))
        for field in provider_fields
        if str(field.get("field_name") or "") and field.get("type") is not None
    }
    rendered: dict[str, Any] = {}
    for key, field_name in mapping.items():
        value = canonical_values.get(key)
        if value is None:
            continue
        field_type = type_by_name[field_name]
        if field_type == URL and value:
            value = {"link": str(value), "text": str(value)}
        elif field_type == DATE_TIME and value is not None:
            value = int(value)
        rendered[field_name] = value
    return rendered


def _json_text(value: Any) -> str:
    return _truncate(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _truncate(value: str) -> str:
    return value[:MAX_TEXT_LENGTH]


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result(*, sync_id, intake_id, status) -> dict[str, Any]:
    return {
        "sync_id": str(sync_id),
        "intake_id": str(intake_id),
        "status": status,
    }
