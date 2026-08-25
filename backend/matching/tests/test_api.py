from uuid import uuid4

import pytest
from django.test import override_settings

from matching.jobs import process_recompute_opportunity_job
from matching.models import (
    Match,
    MatchDecisionEvent,
    MatchOpportunity,
    MatchRun,
    Person,
)


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
            "source_uri": "https://example.com/private/profile?token=secret",
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
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
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
    assert match_response.status_code == 202
    run = MatchRun.objects.get(id=match_response.json()["id"])
    process_recompute_opportunity_job(run.automation_job.payload)
    match_response = admin_client.get(
        f"/api/matching/opportunities/{opportunity_id}/matches/"
    )
    body = match_response.json()
    assert body["count"] == 1
    assert body["results"][0]["eligibility_score"] == 100
    assert body["results"][0]["rank"] == 1
    assert body["results"][0]["person_summary"] == {
        "id": person_id,
        "display_name": "Alice Zhang",
        "current_title": "Growth Engineer",
        "current_company": "",
        "location": "Shanghai",
        "availability": "open_to_offers",
    }
    assert (
        body["results"][0]["evidence_links"][0]["evidence"]["id"]
        == evidence_response.json()["id"]
    )
    safe_evidence = body["results"][0]["evidence_links"][0]["evidence"]
    assert "facts" not in safe_evidence
    assert "source_uri" not in safe_evidence
    assert "source_record_id" not in safe_evidence

    match_id = body["results"][0]["id"]
    reviewed = admin_client.patch(
        f"/api/matching/matches/{match_id}/",
        {
            "status": "shortlisted",
            "expected_revision": 0,
            "expected_ranking_revision": 1,
            "reason_code": "strong_fit",
            "reason": "Evidence supports the shortlist.",
            "idempotency_key": "api-end-to-end-shortlist",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="api-end-to-end-shortlist",
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "shortlisted"
    assert reviewed.json()["decision_revision"] == 1
    assert reviewed.json()["decision_reason"] == "Evidence supports the shortlist."
    assert MatchDecisionEvent.objects.filter(match_id=match_id).count() == 1

    replayed = admin_client.patch(
        f"/api/matching/matches/{match_id}/",
        {
            "status": "shortlisted",
            "expected_revision": 0,
            "expected_ranking_revision": 1,
            "reason_code": "strong_fit",
            "reason": "Evidence supports the shortlist.",
            "idempotency_key": "api-end-to-end-shortlist",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="api-end-to-end-shortlist",
    )
    assert replayed.status_code == 200
    assert MatchDecisionEvent.objects.filter(match_id=match_id).count() == 1

    stale = admin_client.patch(
        f"/api/matching/matches/{match_id}/",
        {
            "status": "accepted",
            "expected_revision": 0,
            "expected_ranking_revision": 1,
            "reason_code": "approved",
            "idempotency_key": "api-stale-accept",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="api-stale-accept",
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "decision_revision_conflict"

    revisions = admin_client.get(
        f"/api/matching/matches/{match_id}/revisions/"
    ).json()
    decisions = admin_client.get(
        f"/api/matching/matches/{match_id}/decisions/"
    ).json()
    assert revisions["count"] == 1
    assert decisions["count"] == 1
    assert "idempotency_key" not in decisions["results"][0]
    assert "facts" not in str(revisions["results"][0]["evidence_snapshot"])


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


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_cross_org_matching_details_and_recompute_are_hidden(
    admin_client,
    org_a,
    org_b,
):
    own_opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="Own opportunity",
    )
    foreign_person = Person.objects.create(org=org_b, display_name="Foreign person")
    foreign_opportunity = MatchOpportunity.objects.create(
        org=org_b,
        opportunity_type="employment",
        title="Foreign opportunity",
    )
    foreign_match = Match.objects.create(
        org=org_b,
        person=foreign_person,
        opportunity=foreign_opportunity,
    )

    assert (
        admin_client.get(f"/api/matching/people/{foreign_person.id}/").status_code
        == 404
    )
    assert (
        admin_client.get(
            f"/api/matching/opportunities/{foreign_opportunity.id}/"
        ).status_code
        == 404
    )
    assert (
        admin_client.get(f"/api/matching/matches/{foreign_match.id}/").status_code
        == 404
    )
    assert (
        admin_client.get(
            f"/api/matching/matches/{foreign_match.id}/revisions/"
        ).status_code
        == 404
    )
    assert (
        admin_client.get(
            f"/api/matching/matches/{foreign_match.id}/decisions/"
        ).status_code
        == 404
    )
    assert (
        admin_client.post(
            f"/api/matching/opportunities/{foreign_opportunity.id}/matches/",
            {},
            format="json",
        ).status_code
        == 404
    )
    foreign_person_response = admin_client.post(
        f"/api/matching/opportunities/{own_opportunity.id}/matches/",
        {"person_ids": [str(foreign_person.id)]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert foreign_person_response.status_code == 400
    assert foreign_person_response.json()["person_ids"]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_compat_recompute_rejects_more_than_500_people(admin_client, org_a):
    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="project",
        title="Bounded opportunity",
    )
    people = Person.objects.bulk_create(
        [
            Person(org=org_a, display_name=f"Person {index:03d}")
            for index in range(501)
        ]
    )

    implicit = admin_client.post(
        f"/api/matching/opportunities/{opportunity.id}/matches/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    explicit = admin_client.post(
        f"/api/matching/opportunities/{opportunity.id}/matches/",
        {"person_ids": [str(person.id) for person in people]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )

    assert implicit.status_code == 400
    assert "500" in str(implicit.json()["person_ids"])
    assert explicit.status_code == 400
    assert "500" in str(explicit.json()["person_ids"])
