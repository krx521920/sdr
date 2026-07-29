import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from integrations.providers.facebook.adapter import (
    FacebookLeadAdsAdapter,
    FacebookWebhookError,
)


def _webhook_body():
    return json.dumps(
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
                                "form_id": "form-3",
                                "ad_id": "ad-9",
                                "created_time": 1_700_000_000,
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def test_signature_and_leadgen_event_are_verified_and_parsed():
    body = _webhook_body()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    adapter = FacebookLeadAdsAdapter(app_secret="app-secret")

    assert adapter.verify_signature(
        headers={"X-Hub-Signature-256": signature}, body=body
    )
    assert (
        adapter.verify_signature(
            headers={"X-Hub-Signature-256": "sha256=bad"}, body=body
        )
        is False
    )

    event = adapter.parse_events(body=body)[0]
    assert event.page_id == "page-42"
    assert event.leadgen_id == "lead-7"
    assert event.form_id == "form-3"
    assert event.ad_id == "ad-9"


def test_mismatched_entry_and_value_page_ids_are_rejected():
    body = _webhook_body().replace(b'"page_id":"page-42"', b'"page_id":"page-99"')

    with pytest.raises(FacebookWebhookError, match="does not match"):
        FacebookLeadAdsAdapter(app_secret="secret").parse_events(body=body)


def test_fetched_lead_is_normalized_without_losing_custom_answers():
    adapter = FacebookLeadAdsAdapter(app_secret="secret")
    event = adapter.parse_events(body=_webhook_body())[0]

    candidate = adapter.normalize(
        org_id=uuid4(),
        event=event,
        lead={
            "id": "lead-7",
            "created_time": "2026-07-29T08:00:00+0000",
            "platform": "fb",
            "field_data": [
                {"name": "full_name", "values": ["Ada Lovelace"]},
                {"name": "email", "values": ["ADA@EXAMPLE.COM"]},
                {"name": "phone_number", "values": ["+44 20 0000 0000"]},
                {"name": "company_name", "values": ["Analytical Engines"]},
                {"name": "job_title", "values": ["VP Sales"]},
                {"name": "team_size", "values": ["51-200"]},
            ],
        },
    )

    assert candidate.source.value == "facebook_ad"
    assert candidate.source_record_id == "lead-7"
    assert candidate.identity.first_name == "Ada"
    assert candidate.identity.last_name == "Lovelace"
    assert candidate.identity.email == "ada@example.com"
    assert candidate.company.name == "Analytical Engines"
    assert candidate.attributes["facebook_form_id"] == "form-3"
    assert candidate.attributes["facebook_answers"] == {"team_size": ["51-200"]}
