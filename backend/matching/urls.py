from django.urls import path

from matching.views import (
    EvidenceListCreateView,
    MatchDetailView,
    MatchOpportunityDetailView,
    MatchOpportunityListCreateView,
    OpportunityMatchListRecomputeView,
    PersonDetailView,
    PersonIdentityListCreateView,
    PersonListCreateView,
)

app_name = "api_matching"

urlpatterns = [
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
    path("matches/<uuid:match_id>/", MatchDetailView.as_view(), name="match_detail"),
]
