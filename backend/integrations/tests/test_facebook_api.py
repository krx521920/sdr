import hashlib
import hmac
import json

import pytest
from django.test import override_settings


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
        "integrations.api.views.process_facebook_lead.delay", queued.append
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
