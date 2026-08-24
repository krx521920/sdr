import pytest
from django.test import override_settings

from matching.models import Person


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_people_api_requires_sales_access_and_is_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
):
    denied = user_client.post(
        "/api/matching/people/",
        {"display_name": "Denied"},
        format="json",
    )
    created = admin_client.post(
        "/api/matching/people/",
        {
            "display_name": "Alice Zhang",
            "skills": ["Python", "Django"],
            "roles": ["SDR"],
            "availability": "available",
        },
        format="json",
    )
    own_list = admin_client.get("/api/matching/people/")
    other_list = org_b_client.get("/api/matching/people/")

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["display_name"] == "Alice Zhang"
    assert own_list.status_code == 200
    assert own_list.json()["count"] == 1
    assert other_list.status_code == 200
    assert other_list.json()["count"] == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_end_to_end_person_evidence_opportunity_match(admin_client):
    person_response = admin_client.post(
        "/api/matching/people/",
        {
            "display_name": "Alice Zhang",
            "current_title": "Growth Engineer",
            "location": "Shanghai",
            "availability": "open_to_offers",
        },
        format="json",
    )
    person_id = person_response.json()["id"]
    identity_response = admin_client.post(
        "/api/matching/identities/",
        {
            "person": person_id,
            "kind": "email",
            "normalized_value": " ALICE@EXAMPLE.COM ",
            "source": "crm",
            "is_primary": True,
        },
        format="json",
    )
    evidence_response = admin_client.post(
        "/api/matching/evidence/",
        {
            "person": person_id,
            "kind": "experience",
            "source": "linkedin",
            "summary": "Built Django-based outbound automation.",
            "facts": {
                "skills": ["Python", "Django"],
                "titles": ["Growth Engineer"],
            },
            "source_record_id": "li-123",
            "confidence": "0.900",
        },
        format="json",
    )
    opportunity_response = admin_client.post(
        "/api/matching/opportunities/",
        {
            "opportunity_type": "employment",
            "status": "open",
            "title": "AI SDR Engineer",
            "required_criteria": {"skills": ["python", "django"]},
            "preferred_criteria": {
                "titles": ["growth engineer"],
                "locations": ["shanghai"],
            },
        },
        format="json",
    )
    opportunity_id = opportunity_response.json()["id"]
    match_response = admin_client.post(
        f"/api/matching/opportunities/{opportunity_id}/matches/",
        {"person_ids": [person_id]},
        format="json",
    )

    assert person_response.status_code == 201
    assert identity_response.status_code == 201
    assert identity_response.json()["normalized_value"] == "alice@example.com"
    duplicate_identity = admin_client.post(
        "/api/matching/identities/",
        {
            "person": person_id,
            "kind": "email",
            "normalized_value": "alice@example.com",
        },
        format="json",
    )
    assert duplicate_identity.status_code == 400
    assert evidence_response.status_code == 201
    assert len(evidence_response.json()["content_hash"]) == 64
    assert opportunity_response.status_code == 201
    assert match_response.status_code == 200
    body = match_response.json()
    assert body["count"] == 1
    assert body["results"][0]["eligibility_score"] == 100
    assert body["results"][0]["rank"] == 1
    assert (
        body["results"][0]["evidence_links"][0]["evidence"]["id"]
        == evidence_response.json()["id"]
    )

    match_id = body["results"][0]["id"]
    reviewed = admin_client.patch(
        f"/api/matching/matches/{match_id}/",
        {"status": "shortlisted"},
        format="json",
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "shortlisted"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_evidence_cannot_reference_a_person_from_another_org(
    admin_client,
    org_b,
):
    foreign_person = Person.objects.create(org=org_b, display_name="Foreign")

    response = admin_client.post(
        "/api/matching/evidence/",
        {
            "person": str(foreign_person.id),
            "kind": "profile",
            "source": "manual",
            "summary": "Should not cross tenant boundaries.",
            "facts": {},
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["person"]
