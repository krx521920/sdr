from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    transaction,
)
from django.test import override_settings
from rest_framework.test import APIClient

from automation.tenant_context import database_org_context
from common.serializer import OrgAwareRefreshToken
from matching.models import Evidence, Person, PersonIdentity

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS, triggers, and row-level concurrency are required.",
    ),
    pytest.mark.django_db(transaction=True),
]


ONBOARDING_PATH = "/api/matching/people/onboard/"
CHILD_ORG_GUARD_MESSAGE = "matching child organization must match person organization"


def _onboarding_payload(
    *,
    display_name="Alice Example",
    email="alice@example.test",
    source_record_id="manual-note-1",
):
    return {
        "person": {"display_name": display_name},
        "identities": [
            {
                "kind": "email",
                "normalized_value": email,
                "display_value": email,
                "is_primary": True,
                "source": "manual",
            }
        ],
        "evidence": [
            {
                "kind": "other",
                "source": "manual",
                "summary": "Introduced during a controlled onboarding test.",
                "facts": {"skills": ["postgres-security-test"]},
                "source_record_id": source_record_id,
            }
        ],
    }


def _authorization_header(user, org, profile):
    token = OrgAwareRefreshToken.for_user_and_org(user, org, profile)
    return f"Bearer {token.access_token}"


def _post_onboarding_in_thread(
    *,
    org_id,
    authorization,
    idempotency_key,
    payload,
    start_barrier,
):
    close_old_connections()
    try:
        start_barrier.wait(timeout=10)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=authorization)
        with database_org_context(org_id):
            response = client.post(
                ONBOARDING_PATH,
                deepcopy(payload),
                format="json",
                secure=True,
                HTTP_IDEMPOTENCY_KEY=str(idempotency_key),
            )
        return response.status_code, response.json()
    finally:
        close_old_connections()


def _raw_insert_clone(model, *, source_id, replacements):
    quote = connection.ops.quote_name
    table = quote(model._meta.db_table)
    columns = []
    select_expressions = []
    params = []

    for field in model._meta.local_concrete_fields:
        column = field.column
        columns.append(quote(column))
        if column in replacements:
            select_expressions.append("%s")
            params.append(replacements[column])
        else:
            select_expressions.append(quote(column))

    params.append(source_id)
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"SELECT {', '.join(select_expressions)} "
            f"FROM {table} WHERE {quote('id')} = %s",
            params,
        )


@pytest.mark.parametrize("child_model", [PersonIdentity, Evidence])
def test_postgres_child_org_guard_rejects_raw_insert_and_update(
    transactional_db,
    org_a,
    org_b,
    child_model,
):
    with database_org_context(org_b.id):
        foreign_person = Person.objects.create(
            org=org_b,
            display_name="Foreign person",
        )

    with database_org_context(org_a.id):
        own_person = Person.objects.create(org=org_a, display_name="Own person")
        if child_model is PersonIdentity:
            child = PersonIdentity.objects.create(
                org=org_a,
                person=own_person,
                kind="email",
                normalized_value="own@example.test",
                display_value="own@example.test",
                source="manual",
            )
            insert_replacements = {
                "id": uuid4(),
                "person_id": foreign_person.id,
                "normalized_value": "cross-org@example.test",
                "display_value": "cross-org@example.test",
            }
        else:
            child = Evidence.objects.create(
                org=org_a,
                person=own_person,
                kind="other",
                source="manual",
                summary="Own evidence",
                facts={},
                source_record_id="own-evidence",
            )
            insert_replacements = {
                "id": uuid4(),
                "person_id": foreign_person.id,
                "source_record_id": "cross-org-evidence",
            }

        with pytest.raises(DatabaseError, match=CHILD_ORG_GUARD_MESSAGE):
            with transaction.atomic():
                _raw_insert_clone(
                    child_model,
                    source_id=child.id,
                    replacements=insert_replacements,
                )

        table = connection.ops.quote_name(child_model._meta.db_table)
        with pytest.raises(DatabaseError, match=CHILD_ORG_GUARD_MESSAGE):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE {table} SET person_id = %s WHERE id = %s",
                        [foreign_person.id, child.id],
                    )

        child.refresh_from_db()
        assert child.person_id == own_person.id
        assert child_model.objects.filter(id=insert_replacements["id"]).count() == 0


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_postgres_different_orgs_can_reuse_identity_and_idempotency_key(
    transactional_db,
    admin_client,
    org_b_client,
    org_a,
    org_b,
):
    idempotency_key = uuid4()
    payload = _onboarding_payload()

    response_a = admin_client.post(
        ONBOARDING_PATH,
        deepcopy(payload),
        format="json",
        secure=True,
        HTTP_IDEMPOTENCY_KEY=str(idempotency_key),
    )
    response_b = org_b_client.post(
        ONBOARDING_PATH,
        deepcopy(payload),
        format="json",
        secure=True,
        HTTP_IDEMPOTENCY_KEY=str(idempotency_key),
    )

    assert response_a.status_code == 201
    assert response_b.status_code == 201
    assert response_a.json()["replayed"] is False
    assert response_b.json()["replayed"] is False
    assert response_a.json()["person_id"] != response_b.json()["person_id"]

    for org in (org_a, org_b):
        with database_org_context(org.id):
            assert (
                Person.objects.filter(
                    org=org,
                    onboarding_idempotency_key=idempotency_key,
                ).count()
                == 1
            )
            assert (
                PersonIdentity.objects.filter(
                    org=org,
                    kind="email",
                    normalized_value="alice@example.test",
                ).count()
                == 1
            )


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_postgres_concurrent_same_key_creates_one_graph_and_replays(
    transactional_db,
    org_a,
    admin_user,
    admin_profile,
):
    idempotency_key = uuid4()
    payload = _onboarding_payload()
    authorization = _authorization_header(admin_user, org_a, admin_profile)
    start_barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _post_onboarding_in_thread,
                org_id=org_a.id,
                authorization=authorization,
                idempotency_key=idempotency_key,
                payload=payload,
                start_barrier=start_barrier,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]

    assert sorted(status for status, _ in results) == [200, 201]
    bodies = [body for _, body in results]
    assert sorted(body["replayed"] for body in bodies) == [False, True]
    assert len({body["person_id"] for body in bodies}) == 1
    assert len({tuple(body["identity_ids"]) for body in bodies}) == 1
    assert len({tuple(body["evidence_ids"]) for body in bodies}) == 1

    with database_org_context(org_a.id):
        person = Person.objects.get(
            org=org_a,
            onboarding_idempotency_key=idempotency_key,
        )
        assert Person.objects.filter(org=org_a).count() == 1
        assert PersonIdentity.objects.filter(org=org_a, person=person).count() == 1
        assert Evidence.objects.filter(org=org_a, person=person).count() == 1


@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_postgres_different_keys_racing_for_identity_leave_no_orphan(
    transactional_db,
    org_a,
    admin_user,
    admin_profile,
):
    authorization = _authorization_header(admin_user, org_a, admin_profile)
    start_barrier = Barrier(2)
    requests = [
        (
            uuid4(),
            _onboarding_payload(
                display_name="Identity contender A",
                source_record_id="contender-a",
            ),
        ),
        (
            uuid4(),
            _onboarding_payload(
                display_name="Identity contender B",
                source_record_id="contender-b",
            ),
        ),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _post_onboarding_in_thread,
                org_id=org_a.id,
                authorization=authorization,
                idempotency_key=idempotency_key,
                payload=payload,
                start_barrier=start_barrier,
            )
            for idempotency_key, payload in requests
        ]
        results = [future.result(timeout=15) for future in futures]

    assert sorted(status for status, _ in results) == [201, 409]
    conflict = next(body for status, body in results if status == 409)
    assert conflict["code"] == "identity_conflict"

    with database_org_context(org_a.id):
        assert Person.objects.filter(org=org_a).count() == 1
        person = Person.objects.get(org=org_a)
        assert (
            PersonIdentity.objects.filter(
                org=org_a,
                person=person,
                kind="email",
                normalized_value="alice@example.test",
            ).count()
            == 1
        )
        assert Evidence.objects.filter(org=org_a, person=person).count() == 1
