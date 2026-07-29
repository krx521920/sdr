from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import override_settings

from integrations.models import (
    FacebookOAuthSession,
    FacebookOAuthSessionStatus,
    FacebookPageConnection,
)
from integrations.providers.facebook.client import FacebookOAuthToken
from integrations.providers.facebook.oauth import (
    FacebookOAuthStateError,
    finish_facebook_oauth,
    select_facebook_pages,
    start_facebook_oauth,
)

OAUTH_SETTINGS = {
    "META_APP_ID": "app-123",
    "META_APP_SECRET": "app-secret",
    "META_OAUTH_DIALOG_URL": "https://www.facebook.com/v25.0/dialog/oauth",
    "META_OAUTH_REDIRECT_URI": "https://api.example.com/api/integrations/facebook/oauth/callback/",
    "META_OAUTH_FRONTEND_REDIRECT_URL": "https://app.example.com/settings/facebook",
    "META_OAUTH_STATE_TTL": 900,
    "META_OAUTH_SCOPES": (
        "pages_show_list",
        "pages_manage_metadata",
        "leads_retrieval",
    ),
}


class FakeOAuthGraphClient:
    def __init__(self):
        self.subscriptions = []

    def exchange_oauth_code(self, *, code, redirect_uri):
        assert code == "oauth-code"
        assert redirect_uri == OAUTH_SETTINGS["META_OAUTH_REDIRECT_URI"]
        return FacebookOAuthToken(access_token="user-token", expires_in=5_184_000)

    def fetch_managed_pages(self, *, user_access_token):
        assert user_access_token == "user-token"
        return [
            {
                "id": "page-42",
                "name": "Acme Page",
                "access_token": "secret-page-token",
                "tasks": ["ADVERTISE", "MODERATE"],
            },
            {
                "id": "page-99",
                "name": "Second Page",
                "access_token": "second-page-token",
                "tasks": ["ADVERTISE"],
            },
        ]

    def fetch_page_identity(self, *, access_token):
        identities = {
            "secret-page-token": {"id": "page-42", "name": "Acme Page"},
            "second-page-token": {"id": "page-99", "name": "Second Page"},
        }
        return identities[access_token]

    def subscribe_page(self, *, page_id, access_token):
        self.subscriptions.append((page_id, access_token))


@pytest.mark.django_db
@override_settings(**OAUTH_SETTINGS)
def test_oauth_discovers_pages_without_exposing_tokens(org_a, admin_profile):
    started = start_facebook_oauth(org_id=org_a.id, profile_id=admin_profile.id)
    query = parse_qs(urlsplit(started.authorization_url).query)

    assert query["client_id"] == ["app-123"]
    assert query["scope"] == ["pages_show_list,pages_manage_metadata,leads_retrieval"]

    oauth_session = finish_facebook_oauth(
        code="oauth-code",
        state=query["state"][0],
        client=FakeOAuthGraphClient(),
    )

    oauth_session.refresh_from_db()
    assert oauth_session.status == FacebookOAuthSessionStatus.READY
    assert oauth_session.pages_snapshot == [
        {
            "id": "page-42",
            "name": "Acme Page",
            "tasks": ["ADVERTISE", "MODERATE"],
        },
        {
            "id": "page-99",
            "name": "Second Page",
            "tasks": ["ADVERTISE"],
        },
    ]
    assert "secret-page-token" not in str(oauth_session.pages_snapshot)
    assert "secret-page-token" not in oauth_session.page_tokens_ciphertext
    assert (
        oauth_session.get_page_credentials()[0]["access_token"] == "secret-page-token"
    )


@pytest.mark.django_db
@override_settings(**OAUTH_SETTINGS)
def test_selected_page_is_connected_and_temporary_tokens_are_destroyed(
    org_a, admin_profile
):
    client = FakeOAuthGraphClient()
    started = start_facebook_oauth(org_id=org_a.id, profile_id=admin_profile.id)
    state_value = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    oauth_session = finish_facebook_oauth(
        code="oauth-code",
        state=state_value,
        client=client,
    )

    connections = select_facebook_pages(
        org_id=org_a.id,
        profile_id=admin_profile.id,
        session_id=oauth_session.id,
        page_ids=["page-42"],
        client=client,
    )

    oauth_session.refresh_from_db()
    assert [connection.page_id for connection in connections] == ["page-42"]
    assert (
        FacebookPageConnection.objects.get().get_access_token() == "secret-page-token"
    )
    assert client.subscriptions == [("page-42", "secret-page-token")]
    assert oauth_session.status == FacebookOAuthSessionStatus.COMPLETED
    assert oauth_session.page_tokens_ciphertext == ""
    assert oauth_session.completed_at is not None


@pytest.mark.django_db
@override_settings(**OAUTH_SETTINGS)
def test_oauth_state_cannot_be_replayed(org_a, admin_profile):
    started = start_facebook_oauth(org_id=org_a.id, profile_id=admin_profile.id)
    state_value = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    finish_facebook_oauth(
        code="oauth-code",
        state=state_value,
        client=FakeOAuthGraphClient(),
    )

    with pytest.raises(FacebookOAuthStateError, match="already been used"):
        finish_facebook_oauth(
            code="oauth-code",
            state=state_value,
            client=FakeOAuthGraphClient(),
        )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls", **OAUTH_SETTINGS)
def test_oauth_session_is_not_visible_to_another_tenant(
    org_a, admin_profile, org_b_client
):
    started = start_facebook_oauth(org_id=org_a.id, profile_id=admin_profile.id)

    response = org_b_client.get(
        f"/api/integrations/facebook/oauth/sessions/{started.session_id}/"
    )

    assert response.status_code == 404
    assert FacebookOAuthSession.objects.filter(id=started.session_id).exists()
