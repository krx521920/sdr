from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import IntegrityError
from django.test import override_settings

from common.models import MatchingAccessLevel
from matching.models import Evidence, Person, PersonIdentity
from matching.onboarding import onboard_person
from matching.serializers import PersonOnboardingRequestSerializer


def onboarding_payload():
    return {
        "person": {
            "display_name": "Alice Zhang",
            "skills": ["Python", "Django"],
            "availability": "available",
        },
        "identities": [
            {
                "kind": "email",
                "normalized_value": " Alice.Zhang@EXAMPLE.COM ",
                "is_primary": True,
            }
        ],
        "evidence": [
            {
                "kind": "other",
                "summary": "Added from a verified manual conversation.",
                "facts": {"skills": ["Python", "Django"]},
                "source_uri": "https://example.com/profile/alice?view=summary",
                "source_record_id": "manual-alice-1",
                "confidence": "0.800",
            }
        ],
    }


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_onboarding_creates_the_complete_graph_with_a_minimal_response(
    admin_client,
    org_a,
):
    response = admin_client.post(
        "/api/matching/people/onboard/",
        onboarding_payload(),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 201
    assert set(response.json()) == {
        "person_id",
        "identity_ids",
        "evidence_ids",
        "replayed",
    }
    assert response.json()["replayed"] is False
    person = Person.objects.get(org=org_a, id=response.json()["person_id"])
    identity = PersonIdentity.objects.get(id=response.json()["identity_ids"][0])
    evidence = Evidence.objects.get(id=response.json()["evidence_ids"][0])
    assert person.onboarding_idempotency_key is not None
    assert len(person.onboarding_request_hash) == 64
    assert identity.person == person
    assert identity.normalized_value == "alice.zhang@example.com"
    assert identity.source == "manual"
    assert evidence.person == person
    assert evidence.source == "manual"
    assert evidence.content_hash


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_onboarding_replays_same_key_and_rejects_changed_payload(admin_client):
    idempotency_key = str(uuid4())
    payload = onboarding_payload()
    created = admin_client.post(
        "/api/matching/people/onboard/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=idempotency_key,
    )
    replayed = admin_client.post(
        "/api/matching/people/onboard/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=idempotency_key,
    )
    changed_payload = deepcopy(payload)
    changed_payload["person"]["display_name"] = "Different Person"
    conflict = admin_client.post(
        "/api/matching/people/onboard/",
        changed_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=idempotency_key,
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.json() == {**created.json(), "replayed": True}
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "onboarding_idempotency_conflict"
    assert Person.objects.count() == 1
    assert PersonIdentity.objects.count() == 1
    assert Evidence.objects.count() == 1


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_onboarding_replay_returns_the_immutable_original_receipt(
    admin_client,
    org_a,
):
    idempotency_key = str(uuid4())
    payload = onboarding_payload()
    created = admin_client.post(
        "/api/matching/people/onboard/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=idempotency_key,
    )
    person = Person.objects.get(id=created.json()["person_id"])
    PersonIdentity.objects.create(
        org=org_a,
        person=person,
        kind="external",
        normalized_value="later-identity",
    )
    Evidence.objects.create(
        org=org_a,
        person=person,
        kind="other",
        source="manual",
        summary="Added after the original onboarding transaction.",
        facts={},
    )

    replayed = admin_client.post(
        "/api/matching/people/onboard/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=idempotency_key,
    )

    assert replayed.status_code == 200
    assert replayed.json() == {**created.json(), "replayed": True}
    assert person.onboarding_identity_ids == created.json()["identity_ids"]
    assert person.onboarding_evidence_ids == created.json()["evidence_ids"]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_onboarding_requires_a_uuid_header_and_manage_access(
    admin_client,
    user_client,
    user_profile,
):
    missing_key = admin_client.post(
        "/api/matching/people/onboard/",
        onboarding_payload(),
        format="json",
    )
    invalid_key = admin_client.post(
        "/api/matching/people/onboard/",
        onboarding_payload(),
        format="json",
        HTTP_IDEMPOTENCY_KEY="not-a-uuid",
    )
    denied = user_client.post(
        "/api/matching/people/onboard/",
        onboarding_payload(),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    user_profile.matching_access_level = MatchingAccessLevel.MANAGE
    user_profile.save(update_fields=["matching_access_level"])
    allowed = user_client.post(
        "/api/matching/people/onboard/",
        onboarding_payload(),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert missing_key.status_code == 400
    assert invalid_key.status_code == 400
    assert denied.status_code == 403
    assert allowed.status_code == 201


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
@pytest.mark.parametrize(
    ("mutate", "error_field"),
    [
        (
            lambda payload: payload["person"].update({"unexpected": "value"}),
            "person",
        ),
        (
            lambda payload: payload["person"].update(
                {"attributes": {"unreviewed": True}}
            ),
            "person",
        ),
        (
            lambda payload: payload["identities"][0].update({"source": "crm"}),
            "identities",
        ),
        (
            lambda payload: payload["evidence"][0].update({"source": "linkedin"}),
            "evidence",
        ),
        (
            lambda payload: payload["evidence"][0].update(
                {"facts": {"unreviewed": ["value"]}}
            ),
            "evidence",
        ),
        (
            lambda payload: payload["evidence"][0].update(
                {"source_uri": "https://user:password@example.com/private"}
            ),
            "evidence",
        ),
        (
            lambda payload: payload["evidence"][0].update(
                {"source_uri": "https://example.com/private?access_token=secret"}
            ),
            "evidence",
        ),
        (
            lambda payload: payload["evidence"][0].update(
                {"source_uri": "https://example.com/callback#access_token=secret"}
            ),
            "evidence",
        ),
    ],
)
def test_onboarding_rejects_untrusted_manual_input(
    admin_client,
    mutate,
    error_field,
):
    payload = onboarding_payload()
    mutate(payload)

    response = admin_client.post(
        "/api/matching/people/onboard/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 400
    assert error_field in response.json()
    assert Person.objects.count() == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_onboarding_rejects_invalid_email_and_bounded_child_lists(admin_client):
    invalid_email = onboarding_payload()
    invalid_email["identities"][0]["normalized_value"] = "alice@example"
    too_many_identities = onboarding_payload()
    too_many_identities["identities"] = [
        {
            "kind": "external",
            "normalized_value": f"external-{index}",
        }
        for index in range(21)
    ]
    no_evidence = onboarding_payload()
    no_evidence["evidence"] = []
    expiry_without_observation = onboarding_payload()
    expiry_without_observation["evidence"][0]["valid_until"] = "2027-08-26T08:00:00Z"

    responses = [
        admin_client.post(
            "/api/matching/people/onboard/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        for payload in (
            invalid_email,
            too_many_identities,
            no_evidence,
            expiry_without_observation,
        )
    ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400]
    assert Person.objects.count() == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_identity_conflict_does_not_create_a_partial_person(
    admin_client,
    org_a,
):
    existing_person = Person.objects.create(org=org_a, display_name="Existing")
    PersonIdentity.objects.create(
        org=org_a,
        person=existing_person,
        kind="email",
        normalized_value="alice.zhang@example.com",
    )

    response = admin_client.post(
        "/api/matching/people/onboard/",
        onboarding_payload(),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "identity_conflict"
    assert Person.objects.count() == 1
    assert Evidence.objects.count() == 0


@pytest.mark.django_db
def test_unexpected_child_write_failure_rolls_back_the_entire_graph(
    org_a,
    admin_profile,
):
    serializer = PersonOnboardingRequestSerializer(data=onboarding_payload())
    assert serializer.is_valid(), serializer.errors

    with patch(
        "matching.onboarding.Evidence.objects.create",
        side_effect=IntegrityError("simulated evidence write failure"),
    ):
        with pytest.raises(IntegrityError):
            onboard_person(
                org=org_a,
                requested_by=admin_profile,
                idempotency_key=uuid4(),
                validated_data=serializer.validated_data,
            )

    assert Person.objects.count() == 0
    assert PersonIdentity.objects.count() == 0
    assert Evidence.objects.count() == 0
