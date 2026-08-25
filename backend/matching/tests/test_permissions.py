from types import SimpleNamespace

import pytest
from django.test import override_settings

from common.models import MatchingAccessLevel
from matching.permissions import HasMatchingAccess, has_matching_access
from matching.views import (
    EvidenceListCreateView,
    MatchDecisionEventListView,
    MatchDetailView,
    MatchOpportunityDetailView,
    MatchOpportunityListCreateView,
    MatchRevisionListView,
    MatchRunDetailView,
    OpportunityMatchListRecomputeView,
    OpportunityMatchRunListView,
    OpportunityRecomputeView,
    PersonDetailView,
    PersonIdentityListCreateView,
    PersonListCreateView,
)


@pytest.mark.parametrize(
    ("current", "required", "allowed"),
    [
        (MatchingAccessLevel.NONE, MatchingAccessLevel.READ, False),
        (MatchingAccessLevel.READ, MatchingAccessLevel.READ, True),
        (MatchingAccessLevel.READ, MatchingAccessLevel.MANAGE, False),
        (MatchingAccessLevel.MANAGE, MatchingAccessLevel.MANAGE, True),
        (MatchingAccessLevel.MANAGE, MatchingAccessLevel.RECOMPUTE, False),
        (MatchingAccessLevel.RECOMPUTE, MatchingAccessLevel.RECOMPUTE, True),
        (MatchingAccessLevel.RECOMPUTE, MatchingAccessLevel.DECIDE, False),
        (MatchingAccessLevel.DECIDE, MatchingAccessLevel.DECIDE, True),
    ],
)
def test_matching_access_levels_are_cumulative(current, required, allowed):
    profile = SimpleNamespace(
        role="USER",
        is_organization_admin=False,
        matching_access_level=current,
    )

    assert has_matching_access(profile, required) is allowed


@pytest.mark.parametrize(
    "profile",
    [
        SimpleNamespace(
            role="ADMIN",
            is_organization_admin=False,
            matching_access_level=MatchingAccessLevel.NONE,
        ),
        SimpleNamespace(
            role="USER",
            is_organization_admin=True,
            matching_access_level=MatchingAccessLevel.NONE,
        ),
    ],
)
def test_matching_access_admins_have_effective_decide(profile):
    assert has_matching_access(profile, MatchingAccessLevel.DECIDE) is True


def test_matching_permission_fails_closed_without_an_explicit_method_mapping():
    request = SimpleNamespace(
        method="POST",
        profile=SimpleNamespace(
            role="USER",
            is_organization_admin=False,
            matching_access_level=MatchingAccessLevel.DECIDE,
        ),
    )
    permission = HasMatchingAccess()

    assert permission.has_permission(request, SimpleNamespace()) is False
    assert permission.has_permission(
        request,
        SimpleNamespace(matching_access_by_method={"GET": MatchingAccessLevel.READ}),
    ) is False


def test_all_matching_views_declare_the_expected_method_contract():
    assert PersonListCreateView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }
    assert PersonDetailView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.MANAGE,
    }
    assert PersonIdentityListCreateView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }
    assert EvidenceListCreateView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }
    assert MatchOpportunityListCreateView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }
    assert MatchOpportunityDetailView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.MANAGE,
    }
    assert OpportunityMatchListRecomputeView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.RECOMPUTE,
    }
    assert OpportunityRecomputeView.matching_access_by_method == {
        "POST": MatchingAccessLevel.RECOMPUTE,
    }
    assert MatchRunDetailView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
    }
    assert OpportunityMatchRunListView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
    }
    assert MatchDetailView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.DECIDE,
    }
    assert MatchRevisionListView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
    }
    assert MatchDecisionEventListView.matching_access_by_method == {
        "GET": MatchingAccessLevel.READ,
    }


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_capabilities_are_available_without_matching_access(
    user_client,
    user_profile,
):
    response = user_client.get("/api/matching/capabilities/")

    assert response.status_code == 200
    assert response.json() == {
        "read": False,
        "manage": False,
        "recompute": False,
        "decide": False,
    }


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_read_and_manage_boundaries_are_enforced(
    user_client,
    user_profile,
):
    denied = user_client.get("/api/matching/people/")
    assert denied.status_code == 403

    user_profile.matching_access_level = MatchingAccessLevel.READ
    user_profile.save(update_fields=["matching_access_level"])
    assert user_client.get("/api/matching/people/").status_code == 200
    assert (
        user_client.post(
            "/api/matching/people/",
            {"display_name": "Denied"},
            format="json",
        ).status_code
        == 403
    )

    user_profile.matching_access_level = MatchingAccessLevel.MANAGE
    user_profile.save(update_fields=["matching_access_level"])
    created = user_client.post(
        "/api/matching/people/",
        {"display_name": "Allowed"},
        format="json",
    )
    assert created.status_code == 201


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="matching.tests.urls")
def test_recompute_and_decide_have_separate_boundaries(
    user_client,
    user_profile,
    org_a,
):
    from matching.models import MatchOpportunity

    opportunity = MatchOpportunity.objects.create(
        org=org_a,
        opportunity_type="employment",
        title="Permission boundary",
    )
    recompute_url = f"/api/matching/opportunities/{opportunity.id}/recompute/"

    user_profile.matching_access_level = MatchingAccessLevel.MANAGE
    user_profile.save(update_fields=["matching_access_level"])
    assert user_client.post(recompute_url, {}, format="json").status_code == 403

    user_profile.matching_access_level = MatchingAccessLevel.RECOMPUTE
    user_profile.save(update_fields=["matching_access_level"])
    assert user_client.post(recompute_url, {}, format="json").status_code == 400

    match_url = "/api/matching/matches/00000000-0000-0000-0000-000000000001/"
    assert user_client.patch(match_url, {}, format="json").status_code == 403

    user_profile.matching_access_level = MatchingAccessLevel.DECIDE
    user_profile.save(update_fields=["matching_access_level"])
    assert user_client.patch(match_url, {}, format="json").status_code == 404
