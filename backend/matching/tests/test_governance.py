from uuid import uuid4

import pytest
from django.test import override_settings

from common.models import MatchingAccessLevel
from matching.governance import ensure_evidence_provenance
from matching.models import (
    Evidence,
    EvidenceConfirmationStatus,
    EvidenceGovernanceEvent,
    EvidenceProvenance,
    Person,
    PersonContactIntent,
    PersonContactIntentEvent,
    PersonGovernanceEvent,
    PersonIdentity,
    PersonImportBatch,
    PersonImportRecord,
)
from sdr.models import SDRDoNotContactEntry


pytestmark = pytest.mark.django_db


def _person_graph(org, actor, *, source="manual"):
    person = Person.objects.create(
        org=org,
        display_name="Governed Person",
        current_title="Engineer",
        current_company="Example",
    )
    identity = PersonIdentity.objects.create(
        org=org,
        person=person,
        kind="email",
        normalized_value="private@example.com",
        display_value="private@example.com",
        source="manual",
    )
    evidence = Evidence.objects.create(
        org=org,
        person=person,
        kind="skill",
        source=source,
        summary="Verified Python experience",
        facts={"skills": ["python"]},
    )
    provenance = ensure_evidence_provenance(evidence=evidence, actor=actor)
    return person, identity, evidence, provenance


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_manual_evidence_api_creates_provenance_and_rejects_raw_content(
    admin_client,
    org_a,
):
    person = Person.objects.create(org=org_a, display_name="Manual")
    rejected = admin_client.post(
        "/api/matching/evidence/",
        {
            "person": str(person.id),
            "kind": "other",
            "summary": "Safe summary",
            "facts": {},
            "raw_content": "must not be accepted",
        },
        format="json",
    )
    assert rejected.status_code == 400
    assert Evidence.objects.filter(org=org_a).count() == 0

    created = admin_client.post(
        "/api/matching/evidence/",
        {
            "person": str(person.id),
            "kind": "other",
            "summary": "Safe summary",
            "facts": {},
        },
        format="json",
    )
    assert created.status_code == 201
    provenance = EvidenceProvenance.objects.get(
        org=org_a,
        evidence_id=created.json()["id"],
    )
    assert provenance.confirmation_status == EvidenceConfirmationStatus.CONFIRMED
    assert provenance.revision == 1
    assert EvidenceGovernanceEvent.objects.filter(provenance=provenance).count() == 1


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_person_attributes_recursively_reject_raw_provider_content(admin_client):
    response = admin_client.post(
        "/api/matching/people/",
        {
            "display_name": "Unsafe Attributes",
            "attributes": {
                "source": {
                    "provider_payload": {"message_body": "private message"}
                }
            },
        },
        format="json",
    )

    assert response.status_code == 400
    assert Person.objects.filter(display_name="Unsafe Attributes").exists() is False


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_identity_and_governance_apis_only_return_safe_values(
    admin_client,
    admin_profile,
    org_a,
):
    person, identity, evidence, _provenance = _person_graph(org_a, admin_profile)

    identities = admin_client.get("/api/matching/identities/")
    governance = admin_client.get(
        f"/api/matching/governance/people/{person.id}/"
    )

    assert identities.status_code == 200
    identity_payload = identities.json()["results"][0]
    assert identity_payload["masked_value"] == "p***@example.com"
    assert "normalized_value" not in identity_payload
    assert "display_value" not in identity_payload
    assert governance.status_code == 200
    serialized = str(governance.json())
    assert identity.normalized_value not in serialized
    assert str(evidence.facts) not in serialized
    assert "source_uri" not in serialized
    assert governance.json()["evidence"][0]["review_status"] == "confirmed"


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_ai_evidence_review_is_cas_and_idempotent(
    admin_client,
    admin_profile,
    org_a,
):
    _person, _identity, evidence, provenance = _person_graph(
        org_a,
        admin_profile,
        source="ai",
    )
    assert provenance.confirmation_status == EvidenceConfirmationStatus.PENDING
    key = uuid4()
    payload = {
        "decision": "confirm",
        "reason_code": "confirmed_accurate",
        "expected_revision": provenance.revision,
    }
    url = f"/api/matching/evidence/{evidence.id}/review/"
    first = admin_client.post(
        url,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    replay = admin_client.post(
        url,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )

    assert first.status_code == 200
    assert first.json()["evidence"]["review_status"] == "confirmed"
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert EvidenceGovernanceEvent.objects.filter(evidence=evidence).count() == 2


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_ai_contact_intent_stays_pending_until_human_update(
    admin_client,
    admin_profile,
    org_a,
):
    person, identity, _evidence, _provenance = _person_graph(org_a, admin_profile)
    url = f"/api/matching/people/{person.id}/contact-intents/"
    base = {
        "channel": "email",
        "purpose": "employment",
        "state": "open",
        "identity_id": str(identity.id),
        "expected_revision": 0,
    }
    proposed = admin_client.post(
        url,
        {**base, "source": "ai"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert proposed.status_code == 201
    assert proposed.json()["intent"]["state"] == "unknown"
    intent = PersonContactIntent.objects.get(person=person)
    event = PersonContactIntentEvent.objects.get(intent=intent)
    assert event.confirmation_status == EvidenceConfirmationStatus.PENDING
    assert intent.revision == 0

    confirmed = admin_client.post(
        url,
        {**base, "source": "manual"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["intent"]["state"] == "open"
    intent.refresh_from_db()
    assert intent.revision == 1


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_objected_intent_without_explicit_identity_blocks_all_channel_identities(
    admin_client,
    admin_profile,
    org_a,
    django_capture_on_commit_callbacks,
):
    person, _identity, _evidence, _provenance = _person_graph(org_a, admin_profile)
    PersonIdentity.objects.create(
        org=org_a,
        person=person,
        kind="email",
        normalized_value="second@example.com",
        source="manual",
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            f"/api/matching/people/{person.id}/contact-intents/",
            {
                "channel": "email",
                "purpose": "general_contact",
                "state": "objected",
                "source": "manual",
                "expected_revision": 0,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )

    assert response.status_code == 201
    assert SDRDoNotContactEntry.objects.filter(
        org=org_a,
        channel="email",
        is_active=True,
    ).count() == 2


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_contact_eligibility_uses_risk_priority_not_purpose_sorting(
    admin_client,
    admin_profile,
    org_a,
):
    person, identity, _evidence, _provenance = _person_graph(org_a, admin_profile)
    intent_url = f"/api/matching/people/{person.id}/contact-intents/"
    for purpose, state in (("general_contact", "not_open"), ("employment", "open")):
        response = admin_client.post(
            intent_url,
            {
                "channel": "email",
                "purpose": purpose,
                "state": state,
                "source": "manual",
                "expected_revision": 0,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert response.status_code == 201

    eligibility = admin_client.post(
        f"/api/matching/people/{person.id}/contact-eligibility/",
        {
            "identity_id": str(identity.id),
            "channel": "email",
            "purpose": "employment",
            "expected_revision": person.governance_revision,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert eligibility.status_code == 200
    assert eligibility.json()["allowed"] is False
    assert eligibility.json()["code"] == "intent_not_open"


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_admin_capabilities_and_person_deletion_are_audited(
    admin_client,
    user_client,
    user_profile,
    org_a,
):
    person = Person.objects.create(org=org_a, display_name="Delete Me")
    user_profile.matching_access_level = MatchingAccessLevel.READ
    user_profile.save(update_fields=["matching_access_level"])

    admin_capabilities = admin_client.get("/api/matching/capabilities/").json()
    user_capabilities = user_client.get("/api/matching/capabilities/").json()
    assert admin_capabilities["delete"] is True
    assert admin_capabilities["export"] is True
    assert admin_capabilities["retention"] is True
    assert user_capabilities["delete"] is False

    denied = user_client.post(
        f"/api/matching/people/{person.id}/deletion/",
        {"action": "request", "expected_revision": 0},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert denied.status_code == 403
    accepted = admin_client.post(
        f"/api/matching/people/{person.id}/deletion/",
        {"action": "request", "expected_revision": 0},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert accepted.status_code == 200
    assert accepted.json()["governance_status"] == "deletion_requested"
    assert PersonGovernanceEvent.objects.filter(person=person).count() == 1


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_person_anonymization_scrubs_linked_import_staging_without_deleting_ledger(
    admin_client,
    admin_profile,
    org_a,
):
    person = Person.objects.create(org=org_a, display_name="Imported Private Person")
    batch = PersonImportBatch.objects.create(
        org=org_a,
        requested_by=admin_profile,
        idempotency_key=uuid4(),
        request_hash="a" * 64,
        content_hash="b" * 64,
        original_filename="private.csv",
        file_size=100,
        mapping={"display_name": "Name", "email": "Email"},
        status="completed",
        total_count=1,
        processed_count=1,
        merged_count=1,
    )
    record = PersonImportRecord.objects.create(
        org=org_a,
        batch=batch,
        row_number=2,
        row_hash="c" * 64,
        source_record_id="safe-source-ledger-key",
        display_name="Imported Private Person",
        normalized_payload={
            "person": {"display_name": "Imported Private Person"},
            "identities": [
                {"kind": "email", "normalized_value": "private-import@example.com"}
            ],
        },
        masked_identities=[
            {"kind": "email", "masked_value": "pr***@example.com"}
        ],
        field_errors=[
            {"field": "row", "code": "legacy", "detail": "Private detail"}
        ],
        status="merged",
        person=person,
    )

    response = admin_client.post(
        f"/api/matching/people/{person.id}/deletion/",
        {
            "action": "anonymize",
            "expected_revision": 0,
            "confirm_person_id": str(person.id),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 200, response.content
    record.refresh_from_db()
    batch.refresh_from_db()
    assert record.person_id == person.id
    assert record.status == "merged"
    assert record.source_record_id == "safe-source-ledger-key"
    assert record.display_name == ""
    assert record.normalized_payload == {}
    assert record.masked_identities == []
    assert record.field_errors == []
    assert batch.status == "completed"
    assert batch.total_count == 1
    assert batch.processed_count == 1
    assert batch.merged_count == 1
    assert batch.records.count() == 1
