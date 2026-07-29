import hashlib
import hmac

import pytest

from integrations.providers.facebook.client import (
    FacebookGraphAPIError,
    FacebookGraphClient,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.responses = response if isinstance(response, list) else [response]
        self.calls = []

    def request(self, method, url, *, params, timeout):
        self.calls.append((method, url, params, timeout))
        return self.responses.pop(0)


def test_graph_client_pins_version_and_sends_appsecret_proof():
    session = FakeSession(FakeResponse(200, {"id": "lead-1", "field_data": []}))
    client = FacebookGraphClient(
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    lead = client.fetch_lead(leadgen_id="lead-1", access_token="page-token")

    method, url, params, timeout = session.calls[0]
    expected_proof = hmac.new(b"secret", b"page-token", hashlib.sha256).hexdigest()
    assert lead["id"] == "lead-1"
    assert method == "GET"
    assert url == "https://graph.facebook.com/v25.0/lead-1"
    assert params["access_token"] == "page-token"
    assert params["appsecret_proof"] == expected_proof
    assert timeout == 10.0


def test_graph_client_subscribes_page_to_leadgen_webhooks():
    session = FakeSession(FakeResponse(200, {"success": True}))
    client = FacebookGraphClient(
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    client.subscribe_page(page_id="page-42", access_token="page-token")

    method, url, params, _ = session.calls[0]
    assert method == "POST"
    assert url == "https://graph.facebook.com/v25.0/page-42/subscribed_apps"
    assert params["subscribed_fields"] == "leadgen"


def test_graph_client_marks_rate_limits_for_retry():
    session = FakeSession(
        FakeResponse(429, {"error": {"message": "Application request limit reached"}})
    )
    client = FacebookGraphClient(
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    with pytest.raises(FacebookGraphAPIError) as error:
        client.fetch_lead(leadgen_id="lead-1", access_token="page-token")

    assert error.value.retryable is True
    assert error.value.status_code == 429


def test_graph_client_exchanges_code_for_long_lived_user_token():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "short-token", "expires_in": 3600}),
            FakeResponse(200, {"access_token": "long-token", "expires_in": 5_184_000}),
        ]
    )
    client = FacebookGraphClient(
        app_id="app-123",
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    token = client.exchange_oauth_code(
        code="authorization-code",
        redirect_uri="https://crm.example.com/callback",
    )

    assert token.access_token == "long-token"
    assert token.expires_in == 5_184_000
    assert len(session.calls) == 2
    assert session.calls[0][2]["code"] == "authorization-code"
    assert session.calls[1][2]["fb_exchange_token"] == "short-token"


def test_graph_client_fetches_all_managed_page_pages():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": [{"id": "page-1", "access_token": "token-1"}],
                    "paging": {
                        "cursors": {"after": "cursor-2"},
                        "next": "https://graph.facebook.com/next",
                    },
                },
            ),
            FakeResponse(
                200,
                {"data": [{"id": "page-2", "access_token": "token-2"}]},
            ),
        ]
    )
    client = FacebookGraphClient(
        app_id="app-123",
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    pages = client.fetch_managed_pages(user_access_token="user-token")

    assert [page["id"] for page in pages] == ["page-1", "page-2"]
    assert session.calls[1][2]["after"] == "cursor-2"
