"""Safe internal CRM projections for the canonical Person import ledger."""

from __future__ import annotations

from django.db.models import Q

from contacts.models import Contact
from leads.models import Lead
from matching.import_pipeline import _mask_identity
from matching.models import EvidenceSource, PersonIdentityKind
from matching.provider_import import (
    ProviderPersonRecord,
    preview_provider_person_import,
)

CRM_ENTITY_TYPES = {"lead", "contact"}


def _display_name(obj, *, fallback="") -> str:
    value = " ".join(
        part.strip() for part in (obj.first_name or "", obj.last_name or "") if part
    ).strip()
    return (value or fallback or "Unnamed CRM person")[:255]


def _location(obj) -> str:
    return ", ".join(
        str(value).strip()
        for value in (obj.city, obj.state, obj.country)
        if value
    )[:255]


def _identities(obj) -> list[dict]:
    values = (
        (PersonIdentityKind.EMAIL, obj.email),
        (PersonIdentityKind.PHONE, obj.phone),
        (PersonIdentityKind.LINKEDIN, obj.linkedin_url),
    )
    return [
        {"kind": kind, "masked_value": _mask_identity(kind, str(value))}
        for kind, value in values
        if value
    ]


def crm_candidates_queryset(*, org, entity_type: str, search: str = ""):
    if entity_type == "lead":
        queryset = Lead.objects.filter(org=org, is_active=True)
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(title__icontains=search)
                | Q(job_title__icontains=search)
                | Q(company_name__icontains=search)
                | Q(email__icontains=search)
            )
    elif entity_type == "contact":
        queryset = Contact.objects.filter(org=org, is_active=True)
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(title__icontains=search)
                | Q(organization__icontains=search)
                | Q(email__icontains=search)
            )
    else:
        raise ValueError("Unsupported CRM entity type.")
    return queryset.order_by("-updated_at", "id")


def safe_crm_candidate(obj, *, entity_type: str) -> dict:
    if entity_type == "lead":
        title = obj.job_title or ""
        company = obj.company_name or ""
        fallback = "Unnamed CRM lead"
    else:
        title = obj.title or ""
        company = obj.organization or ""
        fallback = ""
    return {
        "id": obj.id,
        "entity_type": entity_type,
        "display_name": _display_name(obj, fallback=fallback),
        "current_title": title,
        "current_company": company,
        "location": _location(obj),
        "identities": _identities(obj),
        "updated_at": obj.updated_at,
    }


def _provider_record(obj, *, entity_type: str) -> ProviderPersonRecord:
    safe = safe_crm_candidate(obj, entity_type=entity_type)
    return ProviderPersonRecord(
        source_record_id=str(obj.id),
        display_name=safe["display_name"],
        first_name=(obj.first_name or "")[:120],
        last_name=(obj.last_name or "")[:120],
        current_title=safe["current_title"],
        current_company=safe["current_company"],
        location=safe["location"],
        email=obj.email or "",
        phone=obj.phone or "",
        linkedin=obj.linkedin_url or "",
        evidence_summary=f"CRM {entity_type} profile",
        observed_at=obj.updated_at,
    )


def preview_crm_person_import(
    *, org, requested_by, idempotency_key, entity_type: str, record_ids: list
):
    queryset = crm_candidates_queryset(org=org, entity_type=entity_type).filter(
        id__in=record_ids
    )
    by_id = {str(obj.id): obj for obj in queryset}
    # Do not disclose whether missing IDs belong to another tenant.
    if len(by_id) != len({str(value) for value in record_ids}):
        raise LookupError("crm_records_not_found")
    records = [_provider_record(by_id[str(record_id)], entity_type=entity_type) for record_id in record_ids]
    return preview_provider_person_import(
        org=org,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        source=EvidenceSource.CRM,
        source_namespace=f"crm:{entity_type}",
        records=records,
    )
