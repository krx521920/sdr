from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from automation.errors import RetryableJobError
from automation.models import AutomationJob
from integrations.models import (
    FacebookMessengerMessage,
    FacebookMessengerMessageStatus,
    FacebookMessengerReply,
    FacebookMessengerReplyKind,
    FacebookMessengerReplyStatus,
    FacebookPageConnection,
)
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.messenger import (
    FACEBOOK_MESSENGER_JOB,
    FACEBOOK_MESSENGER_REPLY_JOB,
    FacebookMessengerUnavailable,
    enqueue_facebook_message_event,
    process_facebook_messenger_job,
    process_facebook_messenger_reply_job,
)
from integrations.providers.facebook.service import connect_facebook_page
from leads.models import Lead
from sdr.models import LeadIntake, LeadLifecycleEvent

from .test_facebook_service import FakeGraphClient


def enable_messenger(org, *, auto_reply=False):
    connection = connect_facebook_page(
        org_id=org.id,
        page_access_token="page-token",
        client=FakeGraphClient(),
    )
    connection.messenger_enabled = True
    connection.messenger_auto_reply_enabled = auto_reply
    connection.save(
        update_fields=[
            "messenger_enabled",
            "messenger_auto_reply_enabled",
            "updated_at",
        ]
    )
    return connection


def event(message_id, body, *, sender="psid-7", occurred_at=None):
    return {
        "page_id": "page-42",
        "sender_psid": sender,
        "message_id": message_id,
        "body": body,
        "attachment_types": [],
        "occurred_at": occurred_at or timezone.now().isoformat(),
    }


@pytest.mark.django_db
def test_message_is_persisted_before_idempotent_dispatch(org_a, monkeypatch):
    enable_messenger(org_a)
    dispatched = []
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: dispatched.append(job.id),
    )

    first = enqueue_facebook_message_event(event("mid.1", "Need a quote"))
    replay = enqueue_facebook_message_event(event("mid.1", "Need a quote"))

    assert first.message_id == replay.message_id
    assert replay.replayed is True
    assert FacebookMessengerMessage.objects.count() == 1
    assert AutomationJob.objects.filter(name=FACEBOOK_MESSENGER_JOB).count() == 1
    assert dispatched == [first.job_id, first.job_id]


@pytest.mark.django_db
def test_first_message_creates_lead_and_follow_up_updates_same_conversation(
    org_a,
    admin_profile,
    monkeypatch,
):
    enable_messenger(org_a)
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: None,
    )
    first = enqueue_facebook_message_event(
        event("mid.1", "We need help automating our enterprise sales workflow.")
    )
    first_result = process_facebook_messenger_job(
        {"org_id": str(org_a.id), "message_id": str(first.message_id)}
    )
    second = enqueue_facebook_message_event(
        event("mid.2", "Our team has 80 sales representatives.")
    )
    second_result = process_facebook_messenger_job(
        {"org_id": str(org_a.id), "message_id": str(second.message_id)}
    )

    assert LeadIntake.objects.filter(source="facebook_messenger").count() == 1
    intake = LeadIntake.objects.get(source="facebook_messenger")
    assert first_result["lead_id"] == second_result["lead_id"]
    assert intake.source_record_id == "page-42:psid-7"
    assert "enterprise sales workflow" in intake.crm_lead.description
    assert "80 sales representatives" in intake.crm_lead.description
    assert FacebookMessengerMessage.objects.filter(
        status=FacebookMessengerMessageStatus.PROCESSED,
        intake=intake,
    ).count() == 2
    assert LeadLifecycleEvent.objects.filter(
        intake=intake,
        event_type="channel_message_received",
    ).count() == 1
    assert FacebookPageConnection.objects.get().last_message_at is not None


@pytest.mark.django_db
def test_first_conversation_message_queues_and_sends_one_auto_reply(
    org_a,
    monkeypatch,
):
    connection = enable_messenger(org_a, auto_reply=True)
    connection.messenger_auto_reply_template = (
        "Thanks for contacting {{ page_name }}. Our team will follow up."
    )
    connection.save(update_fields=["messenger_auto_reply_template", "updated_at"])
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: None,
    )

    first = enqueue_facebook_message_event(event("mid.reply-trigger", "Need help"))
    reply = FacebookMessengerReply.objects.get()

    assert reply.trigger_message_id == first.message_id
    assert reply.status == FacebookMessengerReplyStatus.QUEUED
    assert reply.body == "Thanks for contacting Acme Page. Our team will follow up."
    assert AutomationJob.objects.filter(name=FACEBOOK_MESSENGER_REPLY_JOB).count() == 1

    sent = []
    fake_client = type(
        "ReplyClient",
        (),
        {
            "send_text_message": lambda self, **kwargs: (
                sent.append(kwargs)
                or {"recipient_id": "psid-7", "message_id": "mid.auto-reply"}
            )
        },
    )()
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.graph_client",
        lambda: fake_client,
    )

    payload = {"org_id": str(org_a.id), "reply_id": str(reply.id)}
    result = process_facebook_messenger_reply_job(payload)
    replay = process_facebook_messenger_reply_job(payload)
    enqueue_facebook_message_event(event("mid.follow-up", "One more question"))

    reply.refresh_from_db()
    connection.refresh_from_db()
    assert result["status"] == FacebookMessengerReplyStatus.SENT
    assert replay["status"] == FacebookMessengerReplyStatus.SENT
    assert len(sent) == 1
    assert sent[0]["recipient_psid"] == "psid-7"
    assert sent[0]["access_token"] == "page-token"
    assert reply.provider_message_id == "mid.auto-reply"
    assert reply.attempt_count == 1
    assert connection.last_message_reply_at is not None
    assert FacebookMessengerReply.objects.count() == 1
    assert AutomationJob.objects.filter(name=FACEBOOK_MESSENGER_REPLY_JOB).count() == 1


@pytest.mark.django_db
def test_auto_reply_is_skipped_outside_standard_messaging_window(
    org_a,
    monkeypatch,
):
    enable_messenger(org_a, auto_reply=True)
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: None,
    )
    accepted = enqueue_facebook_message_event(
        event(
            "mid.old",
            "This event was delayed",
            occurred_at=(timezone.now() - timedelta(hours=25)).isoformat(),
        )
    )
    reply = FacebookMessengerReply.objects.get(trigger_message_id=accepted.message_id)

    result = process_facebook_messenger_reply_job(
        {"org_id": str(org_a.id), "reply_id": str(reply.id)}
    )

    reply.refresh_from_db()
    assert result["status"] == FacebookMessengerReplyStatus.SKIPPED
    assert reply.error_code == "outside_messaging_window"


@pytest.mark.django_db
def test_auto_reply_retries_transient_meta_failure(org_a, monkeypatch):
    enable_messenger(org_a, auto_reply=True)
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: None,
    )
    accepted = enqueue_facebook_message_event(event("mid.rate-limit", "Hello"))
    reply = FacebookMessengerReply.objects.get(trigger_message_id=accepted.message_id)
    fake_client = type(
        "RateLimitedClient",
        (),
        {
            "send_text_message": lambda self, **kwargs: (_ for _ in ()).throw(
                FacebookGraphAPIError(
                    "Application request limit reached",
                    retryable=True,
                    status_code=429,
                )
            )
        },
    )()
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.graph_client",
        lambda: fake_client,
    )

    with pytest.raises(RetryableJobError):
        process_facebook_messenger_reply_job(
            {"org_id": str(org_a.id), "reply_id": str(reply.id)}
        )

    reply.refresh_from_db()
    assert reply.status == FacebookMessengerReplyStatus.FAILED
    assert reply.attempt_count == 1
    assert reply.error_code == "facebook_reply_rejected"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_assigned_sales_reads_conversation_and_sends_idempotent_manual_reply(
    org_a,
    admin_client,
    user_client,
    user_profile,
    admin_profile,
    monkeypatch,
):
    enable_messenger(org_a)
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: None,
    )
    accepted = enqueue_facebook_message_event(
        event("mid.sales", "Can someone help with enterprise pricing?")
    )
    process_facebook_messenger_job(
        {"org_id": str(org_a.id), "message_id": str(accepted.message_id)}
    )
    intake = LeadIntake.objects.get(source="facebook_messenger")
    url = f"/api/integrations/facebook/conversations/leads/{intake.crm_lead_id}/"

    denied = user_client.get(url)
    assert denied.status_code == 403
    intake.crm_lead.assigned_to.add(user_profile)

    conversation = user_client.get(url)
    assert conversation.status_code == 200
    assert conversation.json()["available"] is True
    assert conversation.json()["can_reply"] is True
    assert conversation.json()["messages"][0]["direction"] == "inbound"

    request_id = uuid4()
    first = user_client.post(
        url,
        {"client_request_id": str(request_id), "body": "Absolutely. What scale do you need?"},
        format="json",
    )
    replay = user_client.post(
        url,
        {"client_request_id": str(request_id), "body": "Absolutely. What scale do you need?"},
        format="json",
    )
    conflict = user_client.post(
        url,
        {"client_request_id": str(request_id), "body": "A different reply"},
        format="json",
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 400
    assert conflict.json()["code"] == "idempotency_conflict"
    reply = FacebookMessengerReply.objects.get(
        kind=FacebookMessengerReplyKind.MANUAL
    )
    assert reply.created_by_id == user_profile.user_id
    assert FacebookMessengerReply.objects.filter(
        kind=FacebookMessengerReplyKind.MANUAL
    ).count() == 1
    assert AutomationJob.objects.filter(name=FACEBOOK_MESSENGER_REPLY_JOB).count() == 1

    sent = []
    fake_client = type(
        "SalesReplyClient",
        (),
        {
            "send_text_message": lambda self, **kwargs: (
                sent.append(kwargs)
                or {"recipient_id": "psid-7", "message_id": "mid.sales-reply"}
            )
        },
    )()
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.graph_client",
        lambda: fake_client,
    )
    result = process_facebook_messenger_reply_job(
        {"org_id": str(org_a.id), "reply_id": str(reply.id)}
    )

    assert result["status"] == FacebookMessengerReplyStatus.SENT
    assert len(sent) == 1
    refreshed = admin_client.get(url).json()
    outbound = [
        message for message in refreshed["messages"] if message["direction"] == "outbound"
    ]
    assert outbound[0]["status"] == FacebookMessengerReplyStatus.SENT
    assert outbound[0]["sent_by"] == user_profile.user.email


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_manual_reply_rejects_expired_window(org_a, admin_client, monkeypatch):
    enable_messenger(org_a)
    monkeypatch.setattr(
        "integrations.providers.facebook.messenger.dispatch_job",
        lambda job: None,
    )
    accepted = enqueue_facebook_message_event(
        event(
            "mid.sales-old",
            "Following up on an old request",
            occurred_at=(timezone.now() - timedelta(hours=25)).isoformat(),
        )
    )
    process_facebook_messenger_job(
        {"org_id": str(org_a.id), "message_id": str(accepted.message_id)}
    )
    intake = LeadIntake.objects.get(source="facebook_messenger")
    url = f"/api/integrations/facebook/conversations/leads/{intake.crm_lead_id}/"

    conversation = admin_client.get(url)
    response = admin_client.post(
        url,
        {"client_request_id": str(uuid4()), "body": "Are you still interested?"},
        format="json",
    )

    assert conversation.status_code == 200
    assert conversation.json()["can_reply"] is False
    assert conversation.json()["reply_unavailable_reason"] == "outside_messaging_window"
    assert response.status_code == 400
    assert response.json()["code"] == "outside_messaging_window"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_non_messenger_lead_returns_unavailable_conversation(
    org_a,
    admin_user,
    admin_client,
):
    lead = Lead.objects.create(
        org=org_a,
        created_by=admin_user,
        title="Manual lead",
        source="other",
    )

    response = admin_client.get(
        f"/api/integrations/facebook/conversations/leads/{lead.id}/"
    )

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reply_unavailable_reason"] == "not_messenger_lead"


@pytest.mark.django_db
def test_disabled_page_does_not_persist_private_messages(org_a):
    connect_facebook_page(
        org_id=org_a.id,
        page_access_token="page-token",
        client=FakeGraphClient(),
    )

    with pytest.raises(FacebookMessengerUnavailable, match="not enabled"):
        enqueue_facebook_message_event(event("mid.disabled", "Hello"))

    assert FacebookMessengerMessage.objects.count() == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_admin_explicitly_enables_messenger_subscription(
    org_a,
    admin_client,
    monkeypatch,
):
    connection = connect_facebook_page(
        org_id=org_a.id,
        page_access_token="page-token",
        client=FakeGraphClient(),
    )
    subscriptions = []
    fake_client = type(
        "MessengerClient",
        (),
        {
            "subscribe_page": lambda self, **kwargs: subscriptions.append(kwargs),
        },
    )()
    monkeypatch.setattr(
        "integrations.providers.facebook.service.graph_client",
        lambda: fake_client,
    )

    response = admin_client.patch(
        f"/api/integrations/facebook/pages/{connection.id}/",
        {"messenger_enabled": True},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["messenger_enabled"] is True
    assert subscriptions[0]["subscribed_fields"] == ("leadgen", "messages")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_admin_configures_validated_messenger_auto_reply(org_a, admin_client):
    connection = enable_messenger(org_a)

    response = admin_client.patch(
        f"/api/integrations/facebook/pages/{connection.id}/",
        {
            "messenger_auto_reply_enabled": True,
            "messenger_auto_reply_template": (
                "Thanks for contacting {{ organization_name }}."
            ),
        },
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["messenger_auto_reply_enabled"] is True
    connection.refresh_from_db()
    assert connection.messenger_auto_reply_enabled is True
    assert connection.messenger_auto_reply_template.endswith(
        "{{ organization_name }}."
    )

    invalid = admin_client.patch(
        f"/api/integrations/facebook/pages/{connection.id}/",
        {"messenger_auto_reply_template": "Hello {{ secret_value }}"},
        format="json",
    )
    assert invalid.status_code == 400

    disabled = admin_client.patch(
        f"/api/integrations/facebook/pages/{connection.id}/",
        {"messenger_enabled": False},
        format="json",
    )
    assert disabled.status_code == 200
    assert disabled.json()["messenger_auto_reply_enabled"] is False

    without_intake = admin_client.patch(
        f"/api/integrations/facebook/pages/{connection.id}/",
        {"messenger_auto_reply_enabled": True},
        format="json",
    )
    assert without_intake.status_code == 400
