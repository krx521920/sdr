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


def test_graph_client_can_subscribe_page_to_messenger_messages():
    session = FakeSession(FakeResponse(200, {"success": True}))
    client = FacebookGraphClient(
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    client.subscribe_page(
        page_id="page-42",
        access_token="page-token",
        subscribed_fields=("leadgen", "messages", "messages"),
    )

    assert session.calls[0][2]["subscribed_fields"] == "leadgen,messages"


def test_graph_client_sends_in_window_messenger_text_response():
    class MessageSession:
        def __init__(self):
            self.call = None

        def request(self, method, url, *, params, json, timeout):
            self.call = (method, url, params, json, timeout)
            return FakeResponse(
                200,
                {"recipient_id": "psid-7", "message_id": "mid.reply.1"},
            )

    session = MessageSession()
    client = FacebookGraphClient(
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )

    result = client.send_text_message(
        page_id="page-42",
        recipient_psid="psid-7",
        access_token="page-token",
        text="Thanks, we received your message.",
    )

    method, url, params, payload, timeout = session.call
    assert result["message_id"] == "mid.reply.1"
    assert method == "POST"
    assert url == "https://graph.facebook.com/v25.0/page-42/messages"
    assert params["access_token"] == "page-token"
    assert payload == {
        "recipient": {"id": "psid-7"},
        "messaging_type": "RESPONSE",
        "message": {"text": "Thanks, we received your message."},
    }
    assert timeout == 10.0


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


def test_graph_client_sends_conversion_lead_payload_without_pii():
    class ConversionSession:
        def __init__(self):
            self.call = None

        def request(self, method, url, *, params, json, timeout):
            self.call = (method, url, params, json, timeout)
            return FakeResponse(200, {"events_received": 1, "fbtrace_id": "trace-1"})

    session = ConversionSession()
    client = FacebookGraphClient(
        app_secret="secret",
        api_version="v25.0",
        session=session,
    )
    event = {
        "event_name": "MarketingQualifiedLead",
        "event_time": 1_700_000_100,
        "action_source": "system_generated",
        "user_data": {"lead_id": 1234567890123456},
        "custom_data": {
            "event_source": "crm",
            "lead_event_source": "BottleCRM",
        },
    }

    result = client.send_conversion_event(
        pixel_id="987654321",
        access_token="conversion-token",
        event=event,
        test_event_code="TEST42",
    )

    method, url, params, payload, timeout = session.call
    assert result["events_received"] == 1
    assert method == "POST"
    assert url == "https://graph.facebook.com/v25.0/987654321/events"
    assert params["access_token"] == "conversion-token"
    assert payload == {"data": [event], "test_event_code": "TEST42"}
    assert "email" not in str(payload).lower()
    assert "phone" not in str(payload).lower()
    assert timeout == 10.0
