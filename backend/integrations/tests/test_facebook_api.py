import hashlib
import hmac
import json

import pytest
from django.test import override_settings

from integrations.models import FacebookOAuthSessionStatus


@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    META_APP_SECRET="app-secret",
    META_WEBHOOK_VERIFY_TOKEN="verify-me",
)
def test_webhook_challenge_is_returned_as_plain_text(unauthenticated_client):
    response = unauthenticated_client.get(
        "/api/integrations/facebook/webhook/",
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 200
    assert response.content == b"challenge-123"
    assert response["Content-Type"].startswith("text/plain")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls", META_APP_SECRET="app-secret")
def test_signed_webhook_is_enqueued(unauthenticated_client, monkeypatch):
    body = json.dumps(
        {
            "object": "page",
            "entry": [
                {
                    "id": "page-42",
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "page_id": "page-42",
                                "leadgen_id": "lead-7",
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    queued = []
    monkeypatch.setattr(
        "integrations.api.views.enqueue_facebook_lead_event", queued.append
    )

    response = unauthenticated_client.generic(
        "POST",
        "/api/integrations/facebook/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "events": 1}
    assert queued == [{"page_id": "page-42", "leadgen_id": "lead-7"}]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls", META_APP_SECRET="app-secret")
def test_unsigned_webhook_is_rejected(unauthenticated_client):
    response = unauthenticated_client.post(
        "/api/integrations/facebook/webhook/",
        {"object": "page", "entry": []},
        format="json",
    )

    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_only_org_admin_can_connect_page(
    admin_client, user_client, org_b_client, monkeypatch
):
    fake_client = type(
        "Client",
        (),
        {
            "fetch_page_identity": lambda self, access_token: {
                "id": "page-42",
                "name": "Acme Page",
            },
            "subscribe_page": lambda self, page_id, access_token: None,
        },
    )()
    monkeypatch.setattr(
        "integrations.providers.facebook.service.graph_client", lambda: fake_client
    )

    denied = user_client.post(
        "/api/integrations/facebook/pages/",
        {"page_access_token": "page-token"},
        format="json",
    )
    created = admin_client.post(
        "/api/integrations/facebook/pages/",
        {"page_access_token": "page-token"},
        format="json",
    )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["page_id"] == "page-42"
    assert "page_access_token" not in created.json()
    assert "access_token_ciphertext" not in created.json()
    assert org_b_client.get("/api/integrations/facebook/pages/").json() == []


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    META_APP_ID="app-123",
    META_APP_SECRET="app-secret",
    META_OAUTH_DIALOG_URL="https://www.facebook.com/v25.0/dialog/oauth",
    META_OAUTH_REDIRECT_URI="https://api.example.com/api/integrations/facebook/oauth/callback/",
    META_OAUTH_FRONTEND_REDIRECT_URL="https://app.example.com/settings/facebook",
    META_OAUTH_STATE_TTL=900,
    META_OAUTH_SCOPES=("pages_show_list", "pages_manage_metadata", "leads_retrieval"),
)
def test_admin_can_start_oauth_but_regular_user_cannot(admin_client, user_client):
    denied = user_client.post("/api/integrations/facebook/oauth/start/")
    created = admin_client.post("/api/integrations/facebook/oauth/start/")

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["authorization_url"].startswith("https://www.facebook.com/")


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    META_OAUTH_FRONTEND_REDIRECT_URL="https://app.example.com/settings/facebook",
)
def test_oauth_callback_redirects_to_page_selection(
    unauthenticated_client, monkeypatch
):
    oauth_session = type(
        "OAuthSession",
        (),
        {
            "id": "7ea3d47b-6079-4bdb-9498-fce5ec13352a",
            "status": FacebookOAuthSessionStatus.READY,
        },
    )()
    monkeypatch.setattr(
        "integrations.api.views.finish_facebook_oauth",
        lambda code, state: oauth_session,
    )

    response = unauthenticated_client.get(
        "/api/integrations/facebook/oauth/callback/",
        {"code": "oauth-code", "state": "signed-state"},
    )

    assert response.status_code == 302
    assert response.url == (
        "https://app.example.com/settings/facebook?"
        "facebook_oauth_session=7ea3d47b-6079-4bdb-9498-fce5ec13352a"
    )
