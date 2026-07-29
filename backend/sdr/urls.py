from django.urls import path

from sdr.api.views import (
    SDRRoutingPreviewView,
    SDRRoutingRuleDetailView,
    SDRRoutingRuleListCreateView,
)

app_name = "api_sdr"

urlpatterns = [
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
