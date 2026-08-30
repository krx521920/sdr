import json
from uuid import uuid4

import pytest
from django.test import override_settings

from contacts.models import Contact
from leads.models import Lead
from matching.governance import GovernanceError
from matching.import_pipeline import execute_person_import
from matching.models import (
    Evidence,
    EvidenceLawfulBasis,
    PersonImportBatch,
    PersonImportRecordStatus,
)
from matching.provider_import import preview_provider_person_import


@pytest.fixture(autouse=True)
def _disable_ssl_redirect_for_api_tests():
    with override_settings(SECURE_SSL_REDIRECT=False):
        yield


def _execute(batch):
    return execute_person_import(
        org_id=batch.org_id, batch_id=batch.id, request_hash=batch.request_hash
    )


@pytest.mark.django_db
def test_trusted_provider_contract_reuses_ledger_and_rejects_raw_content(
    org_a, admin_profile
):
    result = preview_provider_person_import(
        org=org_a,
        requested_by=admin_profile,
        idempotency_key=uuid4(),
        source="crm",
        source_namespace="crm:lead",
        records=[
            {
                "source_record_id": "lead-1",
                "display_name": "Alice Example",
                "email": "Alice@example.com",
                "current_title": "Engineer",
                "evidence_summary": "CRM lead profile",
            }
        ],
    )
    assert result.batch.source == "crm"
    assert result.batch.source_namespace == "crm:lead"
    _execute(result.batch)
    evidence = Evidence.objects.get(source_record_id="lead-1")
    assert evidence.source == "crm"
    assert evidence.provenance.lawful_basis == EvidenceLawfulBasis.UNASSESSED
    assert evidence.provenance.collection_method == "provider_api"

    with pytest.raises(GovernanceError):
        preview_provider_person_import(
            org=org_a,
            requested_by=admin_profile,
            idempotency_key=uuid4(),
            source="crm",
            source_namespace="crm:lead",
            records=[
                {
                    "source_record_id": "lead-2",
                    "display_name": "Unsafe",
                    "email": "unsafe@example.com",
                    "provider_payload": {"access_token": "must-not-persist"},
                }
            ],
        )
    assert "must-not-persist" not in json.dumps(
        list(PersonImportBatch.objects.values("mapping", "headers"))
    )


@pytest.mark.django_db
def test_provider_preview_idempotency_and_source_record_conflict(org_a, admin_profile):
    key = uuid4()
    kwargs = {
        "org": org_a,
        "requested_by": admin_profile,
        "idempotency_key": key,
        "source": "crm",
        "source_namespace": "crm:contact",
        "records": [
            {
                "source_record_id": "contact-1",
                "display_name": "Same Contact",
                "email": "same@example.com",
            }
        ],
    }
    first = preview_provider_person_import(**kwargs)
    replay = preview_provider_person_import(**kwargs)
    assert replay.replayed is True
    assert replay.batch.id == first.batch.id
    _execute(first.batch)

    changed = preview_provider_person_import(
        **{
            **kwargs,
            "idempotency_key": uuid4(),
            "records": [
                {
                    "source_record_id": "contact-1",
                    "display_name": "Changed Contact",
                    "email": "same@example.com",
                }
            ],
        }
    )
    _execute(changed.batch)
    record = changed.batch.records.get()
    assert record.status == PersonImportRecordStatus.CONFLICT
    assert record.error_code == "source_record_conflict"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_crm_candidates_and_preview_are_masked_bounded_and_tenant_scoped(
    admin_client, org_a, org_b
):
    lead = Lead.objects.create(
        org=org_a,
        first_name="Alice",
        last_name="Example",
        email="alice@example.com",
        phone="+8613800001234",
        job_title="Engineer",
        company_name="Example Co",
        description="private narrative must never be imported",
    )
    foreign = Lead.objects.create(
        org=org_b,
        first_name="Foreign",
        last_name="Lead",
        email="foreign@example.com",
    )
    candidates = admin_client.get(
        "/api/matching/person-imports/crm/candidates/",
        {"entity_type": "lead", "search": "Alice", "page_size": 10},
    )
    assert candidates.status_code == 200
    body = json.dumps(candidates.json()).lower()
    assert "alice@example.com" not in body
    assert "private narrative" not in body
    assert "***" in body

    preview = admin_client.post(
        "/api/matching/person-imports/crm/preview/",
        {"entity_type": "lead", "record_ids": [str(lead.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert preview.status_code == 201, preview.content
    batch = PersonImportBatch.objects.get(id=preview.json()["id"])
    assert batch.source == "crm"
    assert batch.source_namespace == "crm:lead"
    persisted = json.dumps(batch.records.get().normalized_payload).lower()
    assert "private narrative" not in persisted

    hidden = admin_client.post(
        "/api/matching/person-imports/crm/preview/",
        {"entity_type": "lead", "record_ids": [str(foreign.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert hidden.status_code == 404


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_crm_record_without_stable_identity_is_previewed_invalid(admin_client, org_a):
    contact = Contact.objects.create(
        org=org_a,
        first_name="No",
        last_name="Identity",
        description="not imported",
        custom_fields={"transcript": "not imported"},
    )
    response = admin_client.post(
        "/api/matching/person-imports/crm/preview/",
        {"entity_type": "contact", "record_ids": [str(contact.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 201
    record = PersonImportBatch.objects.get(id=response.json()["id"]).records.get()
    assert record.status == PersonImportRecordStatus.INVALID
    assert record.normalized_payload == {}
    assert record.error_code == ""


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_crm_import_endpoints_require_manage_access(user_client):
    candidates = user_client.get(
        "/api/matching/person-imports/crm/candidates/",
        {"entity_type": "lead"},
    )
    preview = user_client.post(
        "/api/matching/person-imports/crm/preview/",
        {"entity_type": "lead", "record_ids": [str(uuid4())]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert candidates.status_code == 403
    assert preview.status_code == 403
