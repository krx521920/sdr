"""Durable, idempotent synchronization of SDR research into Feishu Base."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.services import enqueue_job
from integrations.models import (
    FeishuBaseConnection,
    FeishuBaseSync,
    FeishuBaseSyncStatus,
)
from integrations.providers.feishu_base.client import (
    DATE_TIME,
    URL,
    FeishuBaseAPIError,
    FeishuBaseClient,
    FeishuBaseConfigurationError,
    validate_field_mapping,
)
from sdr.models import LeadInspection, LeadIntake, LeadIntakeStatus

logger = logging.getLogger(__name__)

FEISHU_BASE_SYNC_JOB = "feishu_base.sync_research_result"
MAX_TEXT_LENGTH = 20_000


class FeishuBaseSyncUnavailable(ValueError):
    pass


def active_feishu_base_connection(*, org_id: UUID) -> FeishuBaseConnection | None:
    return (
        FeishuBaseConnection.objects.filter(org_id=org_id, is_active=True)
        .exclude(app_secret_ciphertext="")
        .first()
    )


def enqueue_feishu_base_sync(*, intake: LeadIntake):
    if intake.status != LeadIntakeStatus.COMPLETED:
        raise FeishuBaseSyncUnavailable("Only completed intakes can be synchronized.")
    connection = active_feishu_base_connection(org_id=intake.org_id)
    if connection is None:
        raise FeishuBaseSyncUnavailable(
            "Configure and enable a Feishu Base connection first."
        )
    canonical_values = _canonical_values(intake)
    destination_sha256 = _sha256(
        {"app_token": connection.app_token, "table_id": connection.table_id}
    )
    payload_sha256 = _sha256(
        {
            "destination": destination_sha256,
            "configuration_version": connection.updated_at.isoformat(),
            "field_mapping": connection.field_mapping,
            "values": canonical_values,
        }
    )
    with transaction.atomic():
        sync, created = FeishuBaseSync.objects.select_for_update().get_or_create(
            org_id=intake.org_id,
            intake=intake,
            defaults={
                "connection": connection,
                "destination_sha256": destination_sha256,
                "payload_sha256": payload_sha256,
            },
        )
        fields_to_update: list[str] = []
        if sync.destination_sha256 != destination_sha256:
            sync.destination_sha256 = destination_sha256
            sync.record_id = ""
            fields_to_update.extend(["destination_sha256", "record_id"])
        if sync.connection_id != connection.id:
            sync.connection = connection
            sync.record_id = ""
            fields_to_update.extend(["connection", "record_id"])
        if sync.payload_sha256 != payload_sha256:
            sync.payload_sha256 = payload_sha256
            fields_to_update.append("payload_sha256")
        if fields_to_update:
            sync.status = FeishuBaseSyncStatus.PENDING
            sync.error_code = ""
            sync.error_message = ""
            sync.failed_at = None
            fields_to_update.extend(
                ["status", "error_code", "error_message", "failed_at", "updated_at"]
            )
            sync.save(update_fields=list(dict.fromkeys(fields_to_update)))
        elif created:
            sync.full_clean()

        enqueued = enqueue_job(
            JobRequest(
                org_id=intake.org_id,
                name=FEISHU_BASE_SYNC_JOB,
                idempotency_key=f"feishu-base-sync:{sync.id}:{payload_sha256}",
                payload={
                    "org_id": str(intake.org_id),
                    "sync_id": str(sync.id),
                    "payload_sha256": payload_sha256,
                },
                max_attempts=5,
            )
        )
        if not enqueued.terminal_replay:
            FeishuBaseSync.objects.filter(
                id=sync.id,
                org_id=intake.org_id,
            ).update(status=FeishuBaseSyncStatus.QUEUED)
            sync.status = FeishuBaseSyncStatus.QUEUED
    return enqueued.job


def process_feishu_base_sync_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        sync_id = UUID(str(payload["sync_id"]))
        expected_hash = str(payload["payload_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The Feishu Base sync payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    sync = (
        FeishuBaseSync.objects.filter(id=sync_id, org_id=org_id)
        .select_related(
            "connection",
            "intake__inspection",
            "intake__crm_lead",
            "intake__assigned_profile__user",
        )
        .first()
    )
    if sync is None:
        raise PermanentJobError(
            "The Feishu Base sync state no longer exists.",
            code="feishu_sync_not_found",
        )
    if sync.payload_sha256 != expected_hash:
        return _result(sync, replayed=True, stale=True)
    if sync.status == FeishuBaseSyncStatus.SUCCEEDED:
        return _result(sync, replayed=True)

    connection = sync.connection
    if not connection.is_active:
        _mark_skipped(sync, "feishu_connection_inactive")
        return _result(sync, replayed=False)

    attempted_at = timezone.now()
    FeishuBaseSync.objects.filter(id=sync.id, org_id=org_id).update(
        status=FeishuBaseSyncStatus.SYNCING,
        attempt_count=sync.attempt_count + 1,
        last_attempted_at=attempted_at,
        error_code="",
        error_message="",
        failed_at=None,
    )
    sync.status = FeishuBaseSyncStatus.SYNCING
    sync.attempt_count += 1
    sync.last_attempted_at = attempted_at

    client = _client()
    try:
        access_token = client.tenant_access_token(
            app_id=connection.app_id,
            app_secret=connection.get_app_secret(),
        )
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
        business_key_field = connection.field_mapping["intake_id"]
        record = client.find_record_by_field(
            access_token=access_token,
            app_token=connection.app_token,
            table_id=connection.table_id,
            field_name=business_key_field,
            value=str(sync.intake_id),
        )
        if record is None:
            record = client.create_record(
                access_token=access_token,
                app_token=connection.app_token,
                table_id=connection.table_id,
                fields=fields,
            )
        else:
            record = client.update_record(
                access_token=access_token,
                app_token=connection.app_token,
                table_id=connection.table_id,
                record_id=record.record_id,
                fields=fields,
            )
    except FeishuBaseConfigurationError as exc:
        _mark_failed(sync, code="feishu_field_mapping_invalid", error=str(exc))
        raise PermanentJobError(
            str(exc),
            code="feishu_field_mapping_invalid",
        ) from exc
    except FeishuBaseAPIError as exc:
        _mark_failed(sync, code=exc.error_code, error=str(exc))
        error_type = RetryableJobError if exc.retryable else PermanentJobError
        raise error_type(str(exc), code=exc.error_code) from exc

    synced_at = timezone.now()
    field_names = sorted(fields)
    FeishuBaseSync.objects.filter(id=sync.id, org_id=org_id).update(
        status=FeishuBaseSyncStatus.SUCCEEDED,
        record_id=record.record_id,
        synced_field_names=field_names,
        error_code="",
        error_message="",
        failed_at=None,
        synced_at=synced_at,
    )
    FeishuBaseConnection.objects.filter(id=connection.id, org_id=org_id).update(
        last_sync_at=synced_at
    )
    sync.status = FeishuBaseSyncStatus.SUCCEEDED
    sync.record_id = record.record_id
    sync.synced_field_names = field_names
    sync.synced_at = synced_at
    return _result(sync, replayed=False)


def _client() -> FeishuBaseClient:
    return FeishuBaseClient(
        base_url=settings.FEISHU_OPEN_API_BASE_URL,
        timeout=settings.FEISHU_OPEN_API_TIMEOUT,
    )


def _canonical_values(intake: LeadIntake) -> dict[str, Any]:
    # Query explicitly instead of trusting the reverse one-to-one cache. A
    # long-lived/reconciled intake object may have cached "no inspection"
    # before research was added, and must still generate a new payload hash.
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


def _mark_skipped(sync: FeishuBaseSync, code: str) -> None:
    FeishuBaseSync.objects.filter(id=sync.id, org_id=sync.org_id).update(
        status=FeishuBaseSyncStatus.SKIPPED,
        error_code=code,
        error_message="The Feishu Base connection is inactive.",
    )
    sync.status = FeishuBaseSyncStatus.SKIPPED
    sync.error_code = code


def _mark_failed(sync: FeishuBaseSync, *, code: str, error: str) -> None:
    failed_at = timezone.now()
    FeishuBaseSync.objects.filter(id=sync.id, org_id=sync.org_id).update(
        status=FeishuBaseSyncStatus.FAILED,
        error_code=code[:80],
        error_message=error[:1000],
        failed_at=failed_at,
    )
    sync.status = FeishuBaseSyncStatus.FAILED
    sync.error_code = code[:80]
    sync.error_message = error[:1000]
    sync.failed_at = failed_at


def _result(
    sync: FeishuBaseSync,
    *,
    replayed: bool,
    stale: bool = False,
) -> dict[str, Any]:
    return {
        "sync_id": str(sync.id),
        "intake_id": str(sync.intake_id),
        "status": sync.status,
        "record_id": sync.record_id,
        "replayed": replayed,
        "stale": stale,
    }
