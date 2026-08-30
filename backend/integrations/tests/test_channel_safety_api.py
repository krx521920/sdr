import hashlib
import json
from uuid import uuid4

import pytest
from django.test import override_settings

from integrations.execution_safety import add_test_target

BASE = "/api/integrations/channel-safety"


@pytest.fixture(autouse=True)
def _http_tests():
    with override_settings(SECURE_SSL_REDIRECT=False):
        yield


@pytest.mark.django_db
def test_admin_control_plane_is_safe_revisioned_and_idempotent(
    admin_client, org_a, admin_profile
):
    organization = admin_client.put(
        f"{BASE}/organization/",
        {"enabled": True, "daily_limit": 20, "expected_revision": 0},
        format="json",
    )
    channel = admin_client.put(
        f"{BASE}/channels/email/",
        {
            "enabled": True,
            "test_mode": True,
            "daily_limit": 10,
            "per_execution_limit": 2,
            "expected_revision": 0,
        },
        format="json",
    )
    stale = admin_client.put(
        f"{BASE}/channels/email/",
        {
            "enabled": False,
            "test_mode": True,
            "daily_limit": 10,
            "per_execution_limit": 2,
            "expected_revision": 0,
        },
        format="json",
    )
    assert organization.status_code == channel.status_code == 200
    assert stale.status_code == 409

    raw_identifier = "qa-mailbox@example.com"
    target = admin_client.post(
        f"{BASE}/test-targets/",
        {"channel": "email", "identifier": raw_identifier, "safe_label": "QA mailbox"},
        format="json",
    )
    assert target.status_code == 201
    payload_hash = hashlib.sha256(b"safe payload").hexdigest()
    key = uuid4()
    approval_payload = {
        "target_id": target.json()["id"],
        "action": "send_email",
        "payload_sha256": payload_hash,
        "units": 1,
        "expires_in_seconds": 600,
    }
    created = admin_client.post(
        f"{BASE}/approvals/",
        approval_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    replay = admin_client.post(
        f"{BASE}/approvals/",
        approval_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )
    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == created.json()["id"]

    summary = admin_client.get(f"{BASE}/")
    body = json.dumps(summary.json()).lower()
    assert summary.status_code == 200
    assert raw_identifier not in body
    assert payload_hash not in body
    assert "identifier_hash" not in body
    channels = {item["channel"]: item for item in summary.json()["channels"]}
    assert channels["wechat"]["implemented"] is False
    assert channels["wechat"]["enabled"] is False
    assert channels["wecom"]["implemented"] is False


@pytest.mark.django_db
def test_control_plane_is_admin_only_and_cross_org_targets_are_hidden(
    user_client, admin_client, org_b, profile_b
):
    assert user_client.get(f"{BASE}/").status_code == 403
    foreign = add_test_target(
        org=org_b,
        actor=profile_b,
        channel="email",
        identifier="foreign@example.com",
        safe_label="Foreign QA mailbox",
    )
    hidden = admin_client.delete(f"{BASE}/test-targets/{foreign.id}/")
    assert hidden.status_code == 404
