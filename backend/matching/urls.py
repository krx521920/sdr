from django.urls import path

from matching.views import (
    EvidenceListCreateView,
    MatchDecisionEventListView,
    MatchDetailView,
    MatchingCapabilitiesView,
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

app_name = "api_matching"

urlpatterns = [
    path(
        "capabilities/",
        MatchingCapabilitiesView.as_view(),
        name="capabilities",
    ),
    path("people/", PersonListCreateView.as_view(), name="person_list_create"),
    path("people/<uuid:person_id>/", PersonDetailView.as_view(), name="person_detail"),
    path(
        "identities/",
        PersonIdentityListCreateView.as_view(),
        name="identity_list_create",
    ),
    path("evidence/", EvidenceListCreateView.as_view(), name="evidence_list_create"),
    path(
        "opportunities/",
        MatchOpportunityListCreateView.as_view(),
        name="opportunity_list_create",
    ),
    path(
        "opportunities/<uuid:opportunity_id>/",
        MatchOpportunityDetailView.as_view(),
        name="opportunity_detail",
    ),
    path(
        "opportunities/<uuid:opportunity_id>/matches/",
        OpportunityMatchListRecomputeView.as_view(),
        name="opportunity_matches",
    ),
    path(
        "opportunities/<uuid:opportunity_id>/recompute/",
        OpportunityRecomputeView.as_view(),
        name="opportunity_recompute",
    ),
    path(
        "opportunities/<uuid:opportunity_id>/match-runs/",
        OpportunityMatchRunListView.as_view(),
        name="opportunity_match_runs",
    ),
    path(
        "match-runs/<uuid:run_id>/",
        MatchRunDetailView.as_view(),
        name="match_run_detail",
    ),
    path("matches/<uuid:match_id>/", MatchDetailView.as_view(), name="match_detail"),
    path(
        "matches/<uuid:match_id>/revisions/",
        MatchRevisionListView.as_view(),
        name="match_revisions",
    ),
    path(
        "matches/<uuid:match_id>/decisions/",
        MatchDecisionEventListView.as_view(),
        name="match_decisions",
    ),
]
