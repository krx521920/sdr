from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from automation.models import AutomationJob
from automation.tenant_context import database_org_context
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import override_settings
from django.utils import timezone

from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_sending,
    mark_provider_accepted,
)
from integrations.models import (
    ChannelExecutionApproval,
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    WhatsAppMessage,
    WhatsAppMessageStatus,
)
from integrations.providers.whatsapp.outbound import (
    WHATSAPP_MESSAGE_JOB,
    WHATSAPP_SEND_ACTION,
    reserve_and_enqueue_whatsapp_message,
    whatsapp_message_execution_intent,
)
from integrations.providers.whatsapp.webhooks import _apply_status
from integrations.tests.test_whatsapp_execution_api import _message

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL WhatsApp scope trigger is required.",
    ),
    pytest.mark.django_db(transaction=True),
]


def _digest(value):
    return sha256(value.encode()).hexdigest()


def _request(*, org, actor, channel, action, idempotency_key, suffix):
    approval = ChannelExecutionApproval.objects.create(
        org=org,
        channel=channel,
        idempotency_key=uuid4(),
        request_hash=_digest(f"approval:{suffix}"),
        action=action,
        target_hash=_digest(f"target:{suffix}"),
        payload_hash=_digest(f"payload:{suffix}"),
        approved_by=actor,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return ExternalExecutionRequest.objects.create(
        org=org,
        channel=channel,
        action=action,
        idempotency_key=idempotency_key,
        request_hash=_digest(f"request:{suffix}"),
        target_hash=approval.target_hash,
        payload_hash=approval.payload_hash,
        approval=approval,
        reserved_at=timezone.now(),
    )


def _raw_bind(message, request):
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {quote(message._meta.db_table)} "
            f"SET {quote('execution_request_id')} = %s "
            f"WHERE {quote('id')} = %s",
            [request.id, message.id],
        )


def _sqlstate(error):
    current = error
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        diagnostic = getattr(current, "diag", None)
        state = getattr(diagnostic, "sqlstate", None)
        if state:
            return state
        current = getattr(current, "__cause__", None) or getattr(
            current,
            "__context__",
            None,
        )
    return None


def _assert_scope_rejected(message, request):
    with pytest.raises(DatabaseError) as exc_info:
        with transaction.atomic():
            _raw_bind(message, request)
    assert "WhatsApp execution request scope mismatch" in str(exc_info.value)
    assert _sqlstate(exc_info.value) == "23514"


def test_whatsapp_execution_trigger_enforces_org_channel_action_and_idempotency(
    org_a,
    org_b,
    admin_profile,
    profile_b,
):
    with database_org_context(org_a.id):
        valid_message = _message(org_a, suffix="301")
        valid_request = _request(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            action="send_message",
            idempotency_key=valid_message.id,
            suffix="valid",
        )
        _raw_bind(valid_message, valid_request)
        valid_message.refresh_from_db()
        assert valid_message.execution_request_id == valid_request.id

        wrong_channel_message = _message(org_a, suffix="302")
        wrong_channel = _request(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.EMAIL,
            action="send_message",
            idempotency_key=wrong_channel_message.id,
            suffix="wrong-channel",
        )
        _assert_scope_rejected(wrong_channel_message, wrong_channel)

        wrong_action_message = _message(org_a, suffix="303")
        wrong_action = _request(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            action="send_invitation",
            idempotency_key=wrong_action_message.id,
            suffix="wrong-action",
        )
        _assert_scope_rejected(wrong_action_message, wrong_action)

        wrong_key_message = _message(org_a, suffix="304")
        wrong_key = _request(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            action="send_message",
            idempotency_key=uuid4(),
            suffix="wrong-key",
        )
        _assert_scope_rejected(wrong_key_message, wrong_key)

    with database_org_context(org_b.id):
        cross_org_request = _request(
            org=org_b,
            actor=profile_b,
            channel=ExecutionChannel.WHATSAPP,
            action="send_message",
            idempotency_key=uuid4(),
            suffix="cross-org",
        )
    with database_org_context(org_a.id):
        cross_org_message = _message(org_a, suffix="305")
        _assert_scope_rejected(cross_org_message, cross_org_request)


def test_external_execution_scope_is_immutable_and_must_match_approval(
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        request = _request(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            action="send_message",
            idempotency_key=uuid4(),
            suffix="immutable-scope",
        )
        original_payload_hash = request.payload_hash
        with pytest.raises(DatabaseError) as immutable_error:
            with transaction.atomic():
                ExternalExecutionRequest.objects.filter(id=request.id).update(
                    payload_hash=_digest("mutated-payload")
                )
        assert "External execution request scope is immutable" in str(
            immutable_error.value
        )
        assert _sqlstate(immutable_error.value) == "23514"
        request.refresh_from_db()
        assert request.payload_hash == original_payload_hash

        approval = ChannelExecutionApproval.objects.create(
            org=org_a,
            channel=ExecutionChannel.WHATSAPP,
            idempotency_key=uuid4(),
            request_hash=_digest("approval:mismatched"),
            action="send_message",
            target_hash=_digest("target:mismatched"),
            payload_hash=_digest("payload:mismatched"),
            approved_by=admin_profile,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        with pytest.raises(DatabaseError) as mismatch_error:
            with transaction.atomic():
                ExternalExecutionRequest.objects.create(
                    org=org_a,
                    channel=ExecutionChannel.WHATSAPP,
                    action="send_message",
                    idempotency_key=uuid4(),
                    request_hash=_digest("request:mismatched"),
                    target_hash=_digest("different-target"),
                    payload_hash=approval.payload_hash,
                    approval=approval,
                    reserved_at=timezone.now(),
                )
        assert "External execution request approval scope mismatch" in str(
            mismatch_error.value
        )
        assert _sqlstate(mismatch_error.value) == "23514"


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_concurrent_delivery_webhooks_converge_monotonically_without_deadlock(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._safe_dispatch",
        lambda _job: None,
    )
    with database_org_context(org_a.id):
        message = _message(org_a, suffix="concurrent-status")
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=20,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            identifier=message.recipient,
            safe_label="Concurrent webhook recipient",
        )
        intent = whatsapp_message_execution_intent(message)
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            action=WHATSAPP_SEND_ACTION,
            target_hash=intent.target_hash,
            payload_hash=intent.payload_hash,
            units=1,
            idempotency_key=uuid4(),
        ).approval
        submission = reserve_and_enqueue_whatsapp_message(
            message,
            approval_id=approval.id,
        )
        mark_execution_sending(org=org_a, request_id=submission.request.id)
        mark_provider_accepted(org=org_a, request_id=submission.request.id)
        provider_id = "wamid.concurrent-status"
        WhatsAppMessage.objects.filter(id=message.id).update(
            status=WhatsAppMessageStatus.SENT,
            provider_message_id=provider_id,
        )
        message_id = message.id
        request_id = submission.request.id

    start = Barrier(2)

    def worker(provider_status):
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                start.wait(timeout=10)
                return _apply_status(
                    org_a.id,
                    {
                        "id": provider_id,
                        "status": provider_status,
                        "timestamp": "1786500000",
                    },
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(worker, ("delivered", "read"), timeout=20)
        )

    assert outcomes == [True, True]
    with database_org_context(org_a.id):
        message = WhatsAppMessage.objects.get(id=message_id)
        request = ExternalExecutionRequest.objects.get(id=request_id)
        assert message.status == WhatsAppMessageStatus.READ
        assert request.status == "delivered"


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_concurrent_whatsapp_reservation_creates_one_request_job_and_quota(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._safe_dispatch",
        lambda _job: None,
    )
    with database_org_context(org_a.id):
        message = _message(org_a, suffix="concurrent")
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=20,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            identifier=message.recipient,
            safe_label="Concurrent WhatsApp recipient",
        )
        intent = whatsapp_message_execution_intent(message)
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=ExecutionChannel.WHATSAPP,
            action=WHATSAPP_SEND_ACTION,
            target_hash=intent.target_hash,
            payload_hash=intent.payload_hash,
            units=intent.units,
            idempotency_key=uuid4(),
        ).approval
        message_id = message.id
        approval_id = approval.id

    start = Barrier(2)

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                current = WhatsAppMessage.objects.select_related(
                    "org",
                    "connection__route",
                ).get(id=message_id)
                start.wait(timeout=10)
                submission = reserve_and_enqueue_whatsapp_message(
                    current,
                    approval_id=approval_id,
                )
                return (
                    submission.request.id,
                    submission.job.id,
                    submission.replayed,
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(worker),
            executor.submit(worker),
        ]
        outcomes = [future.result(timeout=20) for future in futures]

    assert outcomes[0][0] == outcomes[1][0]
    assert outcomes[0][1] == outcomes[1][1]
    assert sorted(item[2] for item in outcomes) == [False, True]
    with database_org_context(org_a.id):
        message.refresh_from_db()
        request = ExternalExecutionRequest.objects.get(
            org=org_a,
            channel=ExecutionChannel.WHATSAPP,
            idempotency_key=message_id,
        )
        control = ChannelExecutionControl.objects.get(
            org=org_a,
            channel=ExecutionChannel.WHATSAPP,
        )
        job = AutomationJob.objects.get(
            org=org_a,
            name=WHATSAPP_MESSAGE_JOB,
            idempotency_key=f"whatsapp-approved-message:{message_id}",
        )
        assert ExternalExecutionRequest.objects.filter(
            org=org_a,
            channel=ExecutionChannel.WHATSAPP,
            idempotency_key=message_id,
        ).count() == 1
        assert AutomationJob.objects.filter(
            org=org_a,
            name=WHATSAPP_MESSAGE_JOB,
            idempotency_key=f"whatsapp-approved-message:{message_id}",
        ).count() == 1
        assert request.id == outcomes[0][0]
        assert job.id == outcomes[0][1]
        assert job.max_attempts == 1
        assert message.execution_request_id == request.id
        assert control.reserved_units == 1
        assert control.consumed_units == 0
