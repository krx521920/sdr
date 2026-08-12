from django.urls import path

from integrations.api.views import (
    FacebookConversionSettingsView,
    FacebookMessengerConversationView,
    FacebookOAuthCallbackView,
    FacebookOAuthPageSelectionView,
    FacebookOAuthSessionView,
    FacebookOAuthStartView,
    FacebookPageConnectionDetailView,
    FacebookPageConnectionListCreateView,
    FacebookWebhookView,
)

app_name = "api_integrations"

urlpatterns = [
    path(
        "facebook/oauth/start/",
        FacebookOAuthStartView.as_view(),
        name="facebook_oauth_start",
    ),
    path(
        "facebook/oauth/callback/",
        FacebookOAuthCallbackView.as_view(),
        name="facebook_oauth_callback",
    ),
    path(
        "facebook/oauth/sessions/<uuid:session_id>/",
        FacebookOAuthSessionView.as_view(),
        name="facebook_oauth_session",
    ),
    path(
        "facebook/oauth/sessions/<uuid:session_id>/select/",
        FacebookOAuthPageSelectionView.as_view(),
        name="facebook_oauth_select_pages",
    ),
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
        "facebook/conversions/",
        FacebookConversionSettingsView.as_view(),
        name="facebook_conversion_settings",
    ),
    path(
        "facebook/conversations/leads/<uuid:lead_id>/",
        FacebookMessengerConversationView.as_view(),
        name="facebook_messenger_conversation",
    ),
    path(
        "facebook/webhook/",
        FacebookWebhookView.as_view(),
        name="facebook_webhook",
    ),
]
