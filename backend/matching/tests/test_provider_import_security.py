import json
from uuid import uuid4

import pytest
from django.test import override_settings

from automation.tenant_context import database_org_context
from contacts.models import Contact
from matching.governance import GovernanceError
from matching.import_pipeline import (
    MAX_IMPORT_ROWS,
    PersonImportServiceError,
    execute_person_import,
)
from matching.models import (
    Evidence,
    EvidenceLawfulBasis,
    EvidenceProvenance,
    Person,
    PersonContactIntent,
    PersonImportBatch,
    PersonImportRecord,
    PersonImportRecordStatus,
)
from matching.provider_import import preview_provider_person_import

CRM_CANDIDATES_PATH = "/api/matching/person-imports/crm/candidates/"
CRM_PREVIEW_PATH = "/api/matching/person-imports/crm/preview/"


def _record(**overrides):
    values = {
        "source_record_id": "crm-contact-1",
        "display_name": "Provider Import Test",
        "email": "provider-import@example.com",
        "current_title": "Security tester",
        "evidence_summary": "Approved structured CRM profile",
    }
    values.update(overrides)
    return values


def _preview(org, actor, *, key=None, records=None, source="crm"):
    return preview_provider_person_import(
        org=org,
        requested_by=actor,
        idempotency_key=key or uuid4(),
        source=source,
        source_namespace="crm:test-tenant",
        records=[_record()] if records is None else records,
    )


def _contact(org, user, *, email, first_name="CRM", description=""):
    with database_org_context(org.id):
        return Contact.objects.create(
            org=org,
            created_by=user,
            first_name=first_name,
            last_name="Candidate",
            email=email,
            description=description,
        )


@pytest.mark.django_db
def test_provider_preview_rejects_raw_or_credential_shaped_content_without_writes(
    org_a,
    admin_profile,
):
    forbidden = {
        "description": "RAW-DESCRIPTION-SENTINEL",
        "custom_fields": {"private": "CUSTOM-FIELD-SENTINEL"},
        "provider_payload": {"access_token": "PROVIDER-PAYLOAD-SENTINEL"},
        "body": "MESSAGE-BODY-SENTINEL",
        "transcript": "TRANSCRIPT-SENTINEL",
        "credential": "CREDENTIAL-SENTINEL",
    }

    for field, value in forbidden.items():
        with pytest.raises((PersonImportServiceError, GovernanceError)) as exc_info:
            _preview(org_a, admin_profile, records=[_record(**{field: value})])
        assert exc_info.value.code in {"invalid_provider_record", "raw_content_not_accepted"}

    assert not PersonImportBatch.objects.exists()
    assert not PersonImportRecord.objects.exists()
    assert not Evidence.objects.exists()


@pytest.mark.django_db
def test_provider_preview_persists_only_bounded_normalized_data_and_masked_receipt(
    org_a,
    admin_profile,
):
    secret_values = (
        "RAW-DESCRIPTION-SENTINEL",
        "CUSTOM-FIELD-SENTINEL",
        "PROVIDER-PAYLOAD-SENTINEL",
        "MESSAGE-BODY-SENTINEL",
        "TRANSCRIPT-SENTINEL",
        "CREDENTIAL-SENTINEL",
    )

    result = _preview(org_a, admin_profile)
    record = result.batch.records.get()
    persisted = json.dumps(
        {
            "batch": {
                "mapping": result.batch.mapping,
                "headers": result.batch.headers,
                "match_run_ids": result.batch.match_run_ids,
                "error": result.batch.error_code,
            },
            "record": {
                "payload": record.normalized_payload,
                "masked": record.masked_identities,
                "errors": record.field_errors,
                "error": record.error_code,
            },
        },
        default=str,
    ).lower()

    for value in secret_values:
        assert value.lower() not in persisted
    assert "provider-import@example.com" not in json.dumps(record.masked_identities).lower()
    assert record.masked_identities[0]["masked_value"] != "provider-import@example.com"


@pytest.mark.django_db
def test_provider_preview_without_any_identity_is_invalid_and_cannot_create_person(
    org_a,
    admin_profile,
):
    result = _preview(
        org_a,
        admin_profile,
        records=[_record(email="", phone="", linkedin="")],
    )
    record = result.batch.records.get()

    assert result.batch.ready_count == 0
    assert result.batch.invalid_count == 1
    assert record.status == PersonImportRecordStatus.INVALID
    assert record.normalized_payload == {}

    execute_person_import(
        org_id=org_a.id,
        batch_id=result.batch.id,
        request_hash=result.batch.request_hash,
    )
    record.refresh_from_db()
    assert record.person_id is None
    assert not Person.objects.filter(org=org_a, display_name="Provider Import Test").exists()


@pytest.mark.django_db
def test_provider_preview_idempotency_replays_same_request_and_rejects_changed_request(
    org_a,
    admin_profile,
):
    key = uuid4()
    first = _preview(org_a, admin_profile, key=key)
    replay = _preview(org_a, admin_profile, key=key)

    with pytest.raises(PersonImportServiceError) as exc_info:
        _preview(
            org_a,
            admin_profile,
            key=key,
            records=[_record(current_title="Changed title")],
        )

    assert replay.replayed is True
    assert replay.batch.id == first.batch.id
    assert exc_info.value.code == "import_idempotency_conflict"
    assert PersonImportBatch.objects.count() == 1


@pytest.mark.django_db
def test_empty_provider_preview_has_stable_hashes_and_idempotent_replay(
    org_a,
    admin_profile,
):
    key = uuid4()
    first = _preview(org_a, admin_profile, key=key, records=[])
    replay = _preview(org_a, admin_profile, key=key, records=[])
    separate = _preview(org_a, admin_profile, key=uuid4(), records=[])

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.batch.id == first.batch.id
    assert separate.replayed is False
    assert separate.batch.id != first.batch.id
    assert separate.batch.request_hash == first.batch.request_hash
    assert separate.batch.content_hash == first.batch.content_hash
    for batch in (first.batch, separate.batch):
        assert batch.status == "previewed"
        assert batch.total_count == 0
        assert batch.ready_count == 0
        assert batch.invalid_count == 0
        assert not batch.records.exists()
    assert PersonImportBatch.objects.count() == 2
    assert not PersonImportRecord.objects.exists()


@pytest.mark.django_db
def test_provider_preview_still_rejects_non_lists_and_oversized_lists(
    org_a,
    admin_profile,
):
    with pytest.raises(PersonImportServiceError) as non_list:
        _preview(org_a, admin_profile, records=())
    with pytest.raises(PersonImportServiceError) as oversized:
        _preview(
            org_a,
            admin_profile,
            records=[
                _record(source_record_id=f"crm-contact-{index}")
                for index in range(MAX_IMPORT_ROWS + 1)
            ],
        )

    assert non_list.value.code == "invalid_provider_records"
    assert oversized.value.code == "invalid_provider_records"
    assert not PersonImportBatch.objects.exists()
    assert not PersonImportRecord.objects.exists()


@pytest.mark.django_db
def test_changed_provider_source_record_requires_manual_conflict_and_preserves_governance(
    org_a,
    admin_profile,
):
    first = _preview(org_a, admin_profile)
    execute_person_import(
        org_id=org_a.id,
        batch_id=first.batch.id,
        request_hash=first.batch.request_hash,
    )
    first_record = first.batch.records.get()
    first_record.refresh_from_db()
    evidence_count = Evidence.objects.count()

    changed = _preview(
        org_a,
        admin_profile,
        records=[_record(current_title="Tampered replacement title")],
    )
    execute_person_import(
        org_id=org_a.id,
        batch_id=changed.batch.id,
        request_hash=changed.batch.request_hash,
    )
    changed_record = changed.batch.records.get()
    changed_record.refresh_from_db()

    assert changed_record.status == PersonImportRecordStatus.CONFLICT
    assert changed_record.error_code == "source_record_conflict"
    assert changed_record.conflict.status == "open"
    assert Evidence.objects.count() == evidence_count

    provenance = EvidenceProvenance.objects.get(evidence__person=first_record.person)
    assert provenance.lawful_basis == EvidenceLawfulBasis.UNASSESSED
    assert not PersonContactIntent.objects.filter(person=first_record.person).exists()


@pytest.mark.django_db
def test_provider_service_rejects_cross_org_actor_and_keeps_record_ids_tenant_local(
    org_a,
    org_b,
    admin_profile,
):
    with pytest.raises(PersonImportServiceError) as exc_info:
        _preview(org_b, admin_profile)
    assert exc_info.value.code == "import_actor_org_conflict"

    owned = _preview(org_a, admin_profile)
    assert PersonImportRecord.objects.filter(
        org=org_b,
        id=owned.batch.records.get().id,
    ).first() is None


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls", SECURE_SSL_REDIRECT=False)
def test_crm_public_api_cannot_forge_source_namespace_or_raw_payload(
    admin_client,
    admin_user,
    org_a,
):
    contact = _contact(
        org_a,
        admin_user,
        email="api-source-boundary@example.com",
        description="RAW-CRM-DESCRIPTION-SENTINEL",
    )
    payload = {
        "entity_type": "contact",
        "record_ids": [str(contact.id)],
        "source": "apollo",
        "source_namespace": "attacker:namespace",
        "provider_payload": {"credential": "SECRET-SENTINEL"},
    }

    response = admin_client.post(
        CRM_PREVIEW_PATH,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 400
    assert set(response.json()) >= {"source", "source_namespace", "provider_payload"}
    assert not PersonImportBatch.objects.exists()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls", SECURE_SSL_REDIRECT=False)
def test_crm_cross_org_record_id_returns_404_without_disclosure(
    admin_client,
    user_b,
    org_b,
):
    foreign = _contact(org_b, user_b, email="foreign-crm-record@example.com")

    response = admin_client.post(
        CRM_PREVIEW_PATH,
        {"entity_type": "contact", "record_ids": [str(foreign.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 404
    assert response.content in {b"", b"{}"}
    assert not PersonImportBatch.objects.exists()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls", SECURE_SSL_REDIRECT=False)
def test_crm_candidate_api_requires_manage_and_returns_only_masked_identities(
    admin_client,
    user_client,
    admin_user,
    org_a,
):
    contact = _contact(
        org_a,
        admin_user,
        email="masked-candidate@example.com",
        description="PRIVATE-DESCRIPTION-SENTINEL",
    )

    denied = user_client.get(CRM_CANDIDATES_PATH, {"entity_type": "contact"})
    response = admin_client.get(CRM_CANDIDATES_PATH, {"entity_type": "contact"})

    assert denied.status_code == 403
    assert response.status_code == 200
    candidate = next(item for item in response.json()["results"] if item["id"] == str(contact.id))
    serialized = json.dumps(candidate).lower()
    assert "masked-candidate@example.com" not in serialized
    assert "private-description-sentinel" not in serialized
    assert set(candidate) == {
        "id",
        "entity_type",
        "display_name",
        "current_title",
        "current_company",
        "location",
        "identities",
        "updated_at",
    }
    assert candidate["identities"][0]["masked_value"] != "masked-candidate@example.com"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls", SECURE_SSL_REDIRECT=False)
def test_crm_preview_does_not_copy_description_or_custom_fields_into_receipts(
    admin_client,
    admin_user,
    org_a,
):
    contact = _contact(
        org_a,
        admin_user,
        email="safe-crm-import@example.com",
        description="RAW-CRM-DESCRIPTION-SENTINEL",
    )
    contact.custom_fields = {"secret": "CUSTOM-FIELD-SENTINEL"}
    contact.save(update_fields=["custom_fields", "updated_at"])

    response = admin_client.post(
        CRM_PREVIEW_PATH,
        {"entity_type": "contact", "record_ids": [str(contact.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 201, response.content
    batch = PersonImportBatch.objects.get(id=response.json()["id"])
    record = batch.records.get()
    public_and_durable = json.dumps(
        {
            "response": response.json(),
            "batch": {"mapping": batch.mapping, "headers": batch.headers},
            "record": {
                "payload": record.normalized_payload,
                "errors": record.field_errors,
                "masked": record.masked_identities,
            },
        },
        default=str,
    ).lower()
    assert "raw-crm-description-sentinel" not in public_and_durable
    assert "custom-field-sentinel" not in public_and_durable
    assert "safe-crm-import@example.com" not in json.dumps(response.json()).lower()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls", SECURE_SSL_REDIRECT=False)
def test_email_import_public_projection_hides_correspondent_and_mailbox_scope(
    admin_client,
    org_a,
    admin_profile,
):
    email_address = "private-correspondent@example.com"
    scope_digest = "a" * 64
    result = preview_provider_person_import(
        org=org_a,
        requested_by=admin_profile,
        idempotency_key=uuid4(),
        source="email",
        source_namespace=f"email:inbound:{scope_digest}",
        records=[
            _record(
                source_record_id="private-email-source-record",
                email=email_address,
                display_name="Email correspondent",
                current_title="",
                evidence_summary="Inbound email received",
            )
        ],
    )

    detail = admin_client.get(f"/api/matching/person-imports/{result.batch.id}/")
    records = admin_client.get(
        f"/api/matching/person-imports/{result.batch.id}/records/"
    )

    assert detail.status_code == 200, detail.content
    assert records.status_code == 200, records.content
    assert detail.json()["source"] == "email"
    assert detail.json()["source_namespace"] == "email:inbound"
    assert detail.json()["original_filename"] == "Inbound email preview"
    assert records.json()["results"][0]["masked_identities"] == [
        {"kind": "email", "present": True}
    ]
    public_payload = json.dumps(
        {"detail": detail.json(), "records": records.json()}
    ).lower()
    assert email_address not in public_payload
    assert "pr***@example.com" not in public_payload
    assert scope_digest not in public_payload
    assert "private-email-source-record" not in public_payload


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls", SECURE_SSL_REDIRECT=False)
def test_import_batch_list_filters_email_source_and_rejects_unknown_source(
    admin_client,
    org_a,
    admin_profile,
):
    email_batch = preview_provider_person_import(
        org=org_a,
        requested_by=admin_profile,
        idempotency_key=uuid4(),
        source="email",
        source_namespace=f"email:inbound:{'b' * 64}",
        records=[
            _record(
                source_record_id="email-record",
                email="list-filter@example.com",
            )
        ],
    ).batch
    _preview(org_a, admin_profile)

    response = admin_client.get(
        "/api/matching/person-imports/",
        {"source": "email", "limit": 20},
    )
    rejected = admin_client.get(
        "/api/matching/person-imports/",
        {"source": "not-a-source"},
    )

    assert response.status_code == 200, response.content
    assert response.json()["count"] == 1
    assert [item["id"] for item in response.json()["results"]] == [
        str(email_batch.id)
    ]
    assert rejected.status_code == 400
    assert rejected.json() == {"source": ["Unknown import source."]}
