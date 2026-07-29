from django.urls import path

from integrations.api.views import (
    FacebookPageConnectionDetailView,
    FacebookPageConnectionListCreateView,
    FacebookWebhookView,
)

app_name = "api_integrations"

urlpatterns = [
    path(
        "facebook/pages/",
        FacebookPageConnectionListCreateView.as_view(),
        name="facebook_pages",
    ),
    path(
        "facebook/pages/<uuid:connection_id>/",
        FacebookPageConnectionDetailView.as_view(),
        name="facebook_page_detail",
    ),
    path(
        "facebook/webhook/",
        FacebookWebhookView.as_view(),
        name="facebook_webhook",
    ),
]
