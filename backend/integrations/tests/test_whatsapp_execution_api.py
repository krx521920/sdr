import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from automation.models import AutomationJob
from django.core.exceptions import ValidationError
from django.utils import timezone
from sdr.models import OutboundCampaignStatus, SDROutboundCampaign, SDROutboundProspect

from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_sending,
    mark_provider_accepted,
    reconcile_stale_reserved,
    reconcile_stale_sending,
)
from integrations.models import (
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalRequestStatus,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)
from integrations.providers.whatsapp.outbound import WHATSAPP_SEND_ACTION

LIST_URL = "/api/integrations/whatsapp/messages/"
EXECUTION_URL = "/api/integrations/whatsapp/messages/{message_id}/execution/"


@pytest.fixture(autouse=True)
def secure_whatsapp_api_settings(settings):
    settings.ALLOW_UNGUARDED_PROVIDER_IO = False
    settings.REAL_CHANNEL_EXECUTION_ENABLED = True
    settings.ROOT_URLCONF = "integrations.tests.urls"


def _message(org, *, suffix: str, recipient: str = "15551234567"):
    connection = WhatsAppBusinessConnection.objects.filter(org=org).first()
    if connection is None:
        route = WhatsAppPhoneRoute.objects.create(
            org=org,
            phone_number_id=f"9000{suffix}",
        )
        connection = WhatsAppBusinessConnection(org=org, route=route, is_active=True)
        connection.set_access_token(f"private-token-{suffix}")
        connection.save()
    campaign = SDROutboundCampaign.objects.create(
        org=org,
        name=f"Private campaign {suffix}",
        channels=["whatsapp"],
        whatsapp_template_name=f"private_template_{suffix}",
        whatsapp_template_language="en_US",
        status=OutboundCampaignStatus.ACTIVE,
        run_count=1,
    )
    prospect = SDROutboundProspect.objects.create(
        org=org,
        campaign=campaign,
        company_name=f"Private company {suffix}",
        phone=recipient,
        dedupe_key=f"whatsapp-api-{suffix}",
    )
    message = WhatsAppMessage.objects.create(
        org=org,
        connection=connection,
        campaign=campaign,
        prospect=prospect,
        campaign_run=1,
        recipient=recipient,
        template_name=f"private_template_{suffix}",
        template_language="en_US",
        error_message=f"private-error-{suffix}",
        provider_message_id=f"private-provider-{suffix}",
    )
    return message


def _enable_and_approve(*, org, actor, message, intent):
    configure_organization_execution(
        org=org,
        actor=actor,
        enabled=True,
        daily_limit=20,
    )
    configure_channel(
        org=org,
        actor=actor,
        channel=ExecutionChannel.WHATSAPP,
        enabled=True,
        test_mode=True,
        daily_limit=10,
        per_execution_limit=1,
    )
    add_test_target(
        org=org,
        actor=actor,
        channel=ExecutionChannel.WHATSAPP,
        identifier=message.recipient,
        safe_label="Dedicated WhatsApp recipient",
    )
    return issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=ExecutionChannel.WHATSAPP,
        action=WHATSAPP_SEND_ACTION,
        target_hash=intent["target_sha256"],
        payload_hash=intent["payload_sha256"],
        units=intent["units"],
        idempotency_key=uuid4(),
    ).approval


def _signed_webhook(
    client,
    *,
    phone_number_id,
    provider_message_id,
    status,
    error_title="Rejected",
):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "statuses": [
                                {
                                    "id": provider_message_id,
                                    "status": status,
                                    "timestamp": "1786500000",
                                    "errors": [
                                        {"code": "131000", "title": error_title}
                                    ],
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(
        b"webhook-secret",
        body,
        hashlib.sha256,
    ).hexdigest()
    return client.post(
        "/api/integrations/whatsapp/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
    )


@pytest.mark.django_db
def test_whatsapp_message_list_is_admin_only_filtered_and_non_pii(
    admin_client,
    user_client,
    org_a,
):
    message = _message(org_a, suffix="101")

    assert user_client.get(LIST_URL).status_code == 403
    response = admin_client.get(
        LIST_URL,
        {"campaign_id": str(message.campaign_id), "status": "pending", "limit": 1},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "count": 1,
        "results": [
            {
                "id": str(message.id),
                "campaign_id": str(message.campaign_id),
                "prospect_id": str(message.prospect_id),
                "status": WhatsAppMessageStatus.PENDING,
                "execution_request_id": None,
                "execution_status": None,
                "created_at": response.json()["results"][0]["created_at"],
            }
        ],
    }
    serialized = json.dumps(response.json()).lower()
    for private_value in (
        message.recipient,
        message.template_name,
        message.provider_message_id,
        message.error_message,
        "private-token-101",
    ):
        assert private_value.lower() not in serialized
    assert admin_client.get(LIST_URL, {"limit": 101}).status_code == 400
    assert admin_client.get(LIST_URL, {"recipient": message.recipient}).status_code == 400


@pytest.mark.django_db
def test_whatsapp_execution_intent_is_admin_tenant_scoped_strict_and_non_pii(
    admin_client,
    user_client,
    org_b_client,
    org_a,
):
    message = _message(org_a, suffix="102")
    url = EXECUTION_URL.format(message_id=message.id)

    assert user_client.post(url, {}, format="json").status_code == 403
    assert org_b_client.post(url, {}, format="json").status_code == 404
    rejected = admin_client.post(
        url,
        {"recipient": message.recipient},
        format="json",
    )
    assert rejected.status_code == 400
    assert rejected.json()["recipient"] == "Unsupported field."

    response = admin_client.post(url, {}, format="json")

    assert response.status_code == 200, response.json()
    intent = response.json()["intent"]
    assert response.json()["approval_required"] is True
    assert intent == {
        "channel": ExecutionChannel.WHATSAPP,
        "action": WHATSAPP_SEND_ACTION,
        "message_id": str(message.id),
        "target_sha256": intent["target_sha256"],
        "payload_sha256": intent["payload_sha256"],
        "units": 1,
    }
    assert len(intent["target_sha256"]) == 64
    assert len(intent["payload_sha256"]) == 64
    serialized = json.dumps(response.json()).lower()
    assert message.recipient not in serialized
    assert message.template_name.lower() not in serialized
    assert message.provider_message_id.lower() not in serialized
    assert "test_target_identifier" not in serialized


@pytest.mark.django_db
def test_whatsapp_approval_queues_once_and_unknown_cannot_replay(
    admin_client,
    org_a,
    admin_profile,
):
    message = _message(org_a, suffix="103")
    url = EXECUTION_URL.format(message_id=message.id)
    intent = admin_client.post(url, {}, format="json").json()["intent"]
    approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=message,
        intent=intent,
    )

    first = admin_client.post(
        url,
        {"approval_id": str(approval.id)},
        format="json",
    )
    replay = admin_client.post(
        url,
        {"approval_id": str(approval.id)},
        format="json",
    )

    assert first.status_code == 202, first.json()
    assert replay.status_code == 202, replay.json()
    assert replay.json()["replayed"] is True
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert first.json()["execution_status"] == ExternalRequestStatus.RESERVED
    job = AutomationJob.objects.get(id=first.json()["job_id"])
    assert job.max_attempts == 1
    message.refresh_from_db()
    assert str(message.execution_request_id) == first.json()["execution_request_id"]

    mark_execution_sending(org=org_a, request_id=message.execution_request_id)
    mark_provider_accepted(
        org=org_a,
        request_id=message.execution_request_id,
        local_state_uncertain=True,
    )
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.UNKNOWN
    )
    jobs_before = AutomationJob.objects.filter(org=org_a).count()

    blocked = admin_client.post(
        url,
        {"approval_id": str(approval.id)},
        format="json",
    )

    assert blocked.status_code == 409, blocked.json()
    assert blocked.json()["code"] == "whatsapp_execution_not_replayable"
    assert blocked.json()["execution_status"] == ExternalRequestStatus.UNKNOWN
    assert AutomationJob.objects.filter(org=org_a).count() == jobs_before


@pytest.mark.django_db
@pytest.mark.parametrize("provider_status", ["delivered", "read"])
def test_signed_whatsapp_webhook_converges_consumed_execution_to_delivered(
    admin_client,
    unauthenticated_client,
    org_a,
    admin_profile,
    settings,
    provider_status,
):
    settings.META_APP_SECRET = "webhook-secret"
    message = _message(org_a, suffix=f"20{1 if provider_status == 'delivered' else 2}")
    url = EXECUTION_URL.format(message_id=message.id)
    intent = admin_client.post(url, {}, format="json").json()["intent"]
    approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=message,
        intent=intent,
    )
    queued = admin_client.post(
        url,
        {"approval_id": str(approval.id)},
        format="json",
    )
    message.refresh_from_db()
    mark_execution_sending(org=org_a, request_id=message.execution_request_id)
    provider_id = f"wamid.{provider_status}"
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id=provider_id,
    )

    response = _signed_webhook(
        unauthenticated_client,
        phone_number_id=message.connection.phone_number_id,
        provider_message_id=provider_id,
        status=provider_status,
    )

    assert queued.status_code == 202
    assert response.status_code == 200, response.content
    assert response.json() == {"processed": 1, "ignored": 0}
    message.refresh_from_db()
    message.execution_request.refresh_from_db()
    assert message.execution_request.status == ExternalRequestStatus.DELIVERED
    assert message.status == WhatsAppMessageStatus(provider_status)
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert control.reserved_units == 0
    assert control.consumed_units == 1


@pytest.mark.django_db
def test_failed_webhook_consumes_without_refund_and_ledger_error_still_acks(
    admin_client,
    unauthenticated_client,
    org_a,
    admin_profile,
    settings,
    monkeypatch,
):
    settings.META_APP_SECRET = "webhook-secret"
    message = _message(org_a, suffix="203")
    url = EXECUTION_URL.format(message_id=message.id)
    intent = admin_client.post(url, {}, format="json").json()["intent"]
    approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=message,
        intent=intent,
    )
    admin_client.post(url, {"approval_id": str(approval.id)}, format="json")
    message.refresh_from_db()
    mark_execution_sending(org=org_a, request_id=message.execution_request_id)
    provider_id = "wamid.failed-consumed"
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id=provider_id,
    )

    failed = _signed_webhook(
        unauthenticated_client,
        phone_number_id=message.connection.phone_number_id,
        provider_message_id=provider_id,
        status="failed",
    )

    assert failed.status_code == 200
    message.refresh_from_db()
    message.execution_request.refresh_from_db()
    assert message.status == WhatsAppMessageStatus.FAILED
    assert message.execution_request.status == ExternalRequestStatus.ACCEPTED
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert control.reserved_units == 0
    assert control.consumed_units == 1

    message.status = WhatsAppMessageStatus.SENT
    message.provider_message_id = "wamid.ack-safe"
    message.save(update_fields=["status", "provider_message_id", "updated_at"])
    monkeypatch.setattr(
        "integrations.providers.whatsapp.webhooks.mark_execution_delivered",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    ack_safe = _signed_webhook(
        unauthenticated_client,
        phone_number_id=message.connection.phone_number_id,
        provider_message_id="wamid.ack-safe",
        status="delivered",
    )
    assert ack_safe.status_code == 200
    assert ack_safe.json() == {"processed": 1, "ignored": 0}
    message.refresh_from_db()
    assert message.status == WhatsAppMessageStatus.DELIVERED


@pytest.mark.django_db
def test_webhook_statuses_are_monotonic_and_provider_error_text_is_not_stored(
    unauthenticated_client,
    org_a,
    settings,
):
    settings.META_APP_SECRET = "webhook-secret"
    message = _message(org_a, suffix="status-order")
    provider_id = "wamid.status-order"
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id=provider_id,
    )
    private_provider_error = "Rejected private recipient 15551234567"

    failed = _signed_webhook(
        unauthenticated_client,
        phone_number_id=message.connection.phone_number_id,
        provider_message_id=provider_id,
        status="failed",
        error_title=private_provider_error,
    )
    assert failed.status_code == 200
    message.refresh_from_db()
    assert message.status == WhatsAppMessageStatus.FAILED
    assert message.error_message == "WhatsApp reported a delivery failure."
    assert private_provider_error not in message.error_message

    for status, expected in (
        ("sent", WhatsAppMessageStatus.FAILED),
        ("delivered", WhatsAppMessageStatus.DELIVERED),
        ("failed", WhatsAppMessageStatus.DELIVERED),
        ("read", WhatsAppMessageStatus.READ),
        ("delivered", WhatsAppMessageStatus.READ),
        ("failed", WhatsAppMessageStatus.READ),
    ):
        response = _signed_webhook(
            unauthenticated_client,
            phone_number_id=message.connection.phone_number_id,
            provider_message_id=provider_id,
            status=status,
            error_title=private_provider_error,
        )
        assert response.status_code == 200
        message.refresh_from_db()
        assert message.status == expected

    assert message.provider_status_snapshot["status"] == "read"
    assert message.error_code == ""
    assert message.error_message == ""


@pytest.mark.django_db
def test_whatsapp_message_full_clean_rejects_execution_scope_changes(
    admin_client,
    org_a,
    admin_profile,
):
    message = _message(org_a, suffix="204")
    url = EXECUTION_URL.format(message_id=message.id)
    intent = admin_client.post(url, {}, format="json").json()["intent"]
    approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=message,
        intent=intent,
    )
    admin_client.post(url, {"approval_id": str(approval.id)}, format="json")
    message.refresh_from_db()
    request = message.execution_request
    request.channel = ExecutionChannel.EMAIL

    with pytest.raises(ValidationError, match="WhatsApp execution request"):
        message.full_clean()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("outcome", "expected_message_status", "expected_error_code"),
    [
        ("delivered", WhatsAppMessageStatus.DELIVERED, ""),
        (
            "failed_consumed",
            WhatsAppMessageStatus.FAILED,
            "confirmed_provider_failure_consumed",
        ),
    ],
)
def test_unknown_resolution_api_projects_whatsapp_without_replay_and_is_tenant_scoped(
    admin_client,
    org_b_client,
    org_a,
    admin_profile,
    outcome,
    expected_message_status,
    expected_error_code,
):
    suffix = "401" if outcome == "delivered" else "402"
    message = _message(org_a, suffix=suffix)
    execution_url = EXECUTION_URL.format(message_id=message.id)
    intent = admin_client.post(execution_url, {}, format="json").json()["intent"]
    approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=message,
        intent=intent,
    )
    admin_client.post(
        execution_url,
        {"approval_id": str(approval.id)},
        format="json",
    )
    message.refresh_from_db()
    mark_execution_sending(org=org_a, request_id=message.execution_request_id)
    mark_provider_accepted(
        org=org_a,
        request_id=message.execution_request_id,
        local_state_uncertain=True,
    )
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.UNKNOWN
    )
    jobs_before = AutomationJob.objects.filter(org=org_a).count()
    resolution_url = (
        "/api/integrations/channel-safety/unknown/"
        f"{message.execution_request_id}/resolve/"
    )

    assert (
        org_b_client.post(
            resolution_url,
            {"outcome": outcome},
            format="json",
        ).status_code
        == 404
    )
    resolved = admin_client.post(
        resolution_url,
        {"outcome": outcome},
        format="json",
    )

    assert resolved.status_code == 200, resolved.json()
    expected_execution_status = (
        ExternalRequestStatus.DELIVERED
        if outcome == "delivered"
        else ExternalRequestStatus.FAILED
    )
    assert resolved.json()["status"] == expected_execution_status
    message.refresh_from_db()
    assert message.status == expected_message_status
    assert message.error_code == expected_error_code
    assert AutomationJob.objects.filter(org=org_a).count() == jobs_before


@pytest.mark.django_db
def test_stale_reconciliation_projects_whatsapp_and_preserves_terminal_evidence(
    admin_client,
    org_a,
    admin_profile,
):
    reserved = _message(org_a, suffix="403")
    reserved_url = EXECUTION_URL.format(message_id=reserved.id)
    reserved_intent = admin_client.post(reserved_url, {}, format="json").json()[
        "intent"
    ]
    reserved_approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=reserved,
        intent=reserved_intent,
    )
    admin_client.post(
        reserved_url,
        {"approval_id": str(reserved_approval.id)},
        format="json",
    )
    reserved.refresh_from_db()
    reserved.execution_request.reserved_at = timezone.now() - timedelta(hours=1)
    reserved.execution_request.save(update_fields=["reserved_at", "updated_at"])

    released = reconcile_stale_reserved(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=5),
    )

    reserved.refresh_from_db()
    reserved.execution_request.refresh_from_db()
    assert reserved.execution_request_id in released
    assert reserved.execution_request.status == ExternalRequestStatus.FAILED
    assert reserved.status == WhatsAppMessageStatus.FAILED
    assert reserved.error_code == "stale_reservation_released"

    sending = _message(org_a, suffix="404")
    sending_url = EXECUTION_URL.format(message_id=sending.id)
    sending_intent = admin_client.post(sending_url, {}, format="json").json()[
        "intent"
    ]
    sending_approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=sending,
        intent=sending_intent,
    )
    admin_client.post(
        sending_url,
        {"approval_id": str(sending_approval.id)},
        format="json",
    )
    sending.refresh_from_db()
    mark_execution_sending(org=org_a, request_id=sending.execution_request_id)
    sending.execution_request.sending_at = timezone.now() - timedelta(hours=1)
    sending.execution_request.save(update_fields=["sending_at", "updated_at"])
    WhatsAppMessage.objects.filter(id=sending.id).update(
        status=WhatsAppMessageStatus.SENDING
    )

    unknown = reconcile_stale_sending(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=5),
    )

    sending.refresh_from_db()
    sending.execution_request.refresh_from_db()
    assert sending.execution_request_id in unknown
    assert sending.execution_request.status == ExternalRequestStatus.UNKNOWN
    assert sending.status == WhatsAppMessageStatus.UNKNOWN
    assert sending.error_code == "stale_whatsapp_outcome_unknown"

    terminal = _message(org_a, suffix="405")
    terminal_url = EXECUTION_URL.format(message_id=terminal.id)
    terminal_intent = admin_client.post(terminal_url, {}, format="json").json()[
        "intent"
    ]
    terminal_approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=terminal,
        intent=terminal_intent,
    )
    admin_client.post(
        terminal_url,
        {"approval_id": str(terminal_approval.id)},
        format="json",
    )
    terminal.refresh_from_db()
    mark_execution_sending(org=org_a, request_id=terminal.execution_request_id)
    terminal.execution_request.sending_at = timezone.now() - timedelta(hours=1)
    terminal.execution_request.save(update_fields=["sending_at", "updated_at"])
    WhatsAppMessage.objects.filter(id=terminal.id).update(
        status=WhatsAppMessageStatus.SENT
    )

    reconcile_stale_sending(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=5),
    )

    terminal.refresh_from_db()
    terminal.execution_request.refresh_from_db()
    assert terminal.execution_request.status == ExternalRequestStatus.UNKNOWN
    assert terminal.status == WhatsAppMessageStatus.SENT

    terminal_reserved = _message(org_a, suffix="406")
    terminal_reserved_url = EXECUTION_URL.format(message_id=terminal_reserved.id)
    terminal_reserved_intent = admin_client.post(
        terminal_reserved_url,
        {},
        format="json",
    ).json()["intent"]
    terminal_reserved_approval = _enable_and_approve(
        org=org_a,
        actor=admin_profile,
        message=terminal_reserved,
        intent=terminal_reserved_intent,
    )
    admin_client.post(
        terminal_reserved_url,
        {"approval_id": str(terminal_reserved_approval.id)},
        format="json",
    )
    terminal_reserved.refresh_from_db()
    terminal_reserved.execution_request.reserved_at = timezone.now() - timedelta(
        hours=1
    )
    terminal_reserved.execution_request.save(
        update_fields=["reserved_at", "updated_at"]
    )
    WhatsAppMessage.objects.filter(id=terminal_reserved.id).update(
        status=WhatsAppMessageStatus.SENT
    )

    protected = reconcile_stale_reserved(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=5),
    )

    terminal_reserved.refresh_from_db()
    terminal_reserved.execution_request.refresh_from_db()
    assert terminal_reserved.execution_request_id not in protected
    assert (
        terminal_reserved.execution_request.status
        == ExternalRequestStatus.RESERVED
    )
    assert terminal_reserved.status == WhatsAppMessageStatus.SENT
