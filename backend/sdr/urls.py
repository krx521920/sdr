from django.urls import path

from sdr.api.views import (
    LeadInspectionDetailView,
    LeadInspectionListView,
    LeadIntakeDetailView,
    LeadIntakeListView,
    SDRIntelligenceSettingsView,
    SDRResponseSettingsView,
    SDRRoutingPreviewView,
    SDRRoutingRuleDetailView,
    SDRRoutingRuleListCreateView,
)

app_name = "api_sdr"

urlpatterns = [
    path(
        "response-settings/",
        SDRResponseSettingsView.as_view(),
        name="response_settings",
    ),
    path("intakes/", LeadIntakeListView.as_view(), name="intake_list"),
    path(
        "intakes/<uuid:intake_id>/",
        LeadIntakeDetailView.as_view(),
        name="intake_detail",
    ),
    path(
        "intelligence/settings/",
        SDRIntelligenceSettingsView.as_view(),
        name="intelligence_settings",
    ),
    path(
        "intelligence/inspections/",
        LeadInspectionListView.as_view(),
        name="inspection_list",
    ),
    path(
        "intelligence/inspections/<uuid:inspection_id>/",
        LeadInspectionDetailView.as_view(),
        name="inspection_detail",
    ),
    path(
        "routing-rules/",
        SDRRoutingRuleListCreateView.as_view(),
        name="routing_rule_list_create",
    ),
    path(
        "routing-rules/preview/",
        SDRRoutingPreviewView.as_view(),
        name="routing_rule_preview",
    ),
    path(
        "routing-rules/<uuid:rule_id>/",
        SDRRoutingRuleDetailView.as_view(),
        name="routing_rule_detail",
    ),
]
