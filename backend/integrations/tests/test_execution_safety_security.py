import hashlib
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection
from django.test import override_settings
from django.utils import timezone

from automation.tenant_context import database_org_context
from integrations.execution_safety import (
    ExecutionSafetyError,
    add_test_target,
    configure_channel,
    configure_organization_execution,
    hash_target_identifier,
    issue_execution_approval,
    mark_execution_sending,
    reserve_execution,
)
from integrations.models import (
    ChannelExecutionApproval,
    ChannelExecutionControl,
    ChannelTestTarget,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    OrganizationExecutionControl,
)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(org, actor, *, channel=ExecutionChannel.EMAIL, org_limit=10):
    identifier = (
        "execution-security@example.com"
        if channel == ExecutionChannel.EMAIL
        else "+15550000001"
    )
    configure_organization_execution(
        org=org,
        actor=actor,
        enabled=True,
        daily_limit=org_limit,
    )
    configure_channel(
        org=org,
        actor=actor,
        channel=channel,
        enabled=True,
        test_mode=True,
        daily_limit=10,
        per_execution_limit=10,
    )
    target = add_test_target(
        org=org,
        actor=actor,
        channel=channel,
        identifier=identifier,
        safe_label="Dedicated security test target",
    )
    payload_hash = _hash(f"{channel}-payload")
    approval = issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=channel,
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=1,
    )
    return target, payload_hash, approval


def _reserve(org, target, payload_hash, approval, *, key=None):
    return reserve_execution(
        org=org,
        channel=approval.channel,
        action=approval.action,
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=approval.units,
        approval_id=approval.id,
        idempotency_key=key or uuid4(),
    )


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
@pytest.mark.parametrize("revocation", ["organization", "channel", "target"])
def test_every_kill_switch_is_rechecked_after_reservation_and_releases_quota(
    org_a,
    admin_profile,
    revocation,
):
    target, payload_hash, approval = _scope(org_a, admin_profile)
    reservation = _reserve(org_a, target, payload_hash, approval)
    expected = {
        "organization": "organization_execution_disabled",
        "channel": "channel_disabled",
        "target": "target_not_allowlisted",
    }[revocation]
    if revocation == "organization":
        OrganizationExecutionControl.objects.filter(org=org_a).update(enabled=False)
    elif revocation == "channel":
        ChannelExecutionControl.objects.filter(
            org=org_a,
            channel=ExecutionChannel.EMAIL,
        ).update(enabled=False)
    else:
        ChannelTestTarget.objects.filter(id=target.id, org=org_a).update(is_active=False)

    with pytest.raises(ExecutionSafetyError) as exc_info:
        mark_execution_sending(org=org_a, request_id=reservation.request.id)
    assert exc_info.value.code == expected
    request = ExternalExecutionRequest.objects.get(id=reservation.request.id, org=org_a)
    assert (request.status, request.error_code) == (ExternalRequestStatus.FAILED, expected)
    assert OrganizationExecutionControl.objects.get(org=org_a).reserved_units == 0
    assert ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.EMAIL,
    ).reserved_units == 0


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_approval_expiry_single_use_payload_binding_and_exact_replay(
    org_a,
    admin_profile,
):
    target, payload_hash, approval = _scope(org_a, admin_profile)
    key = uuid4()
    first = _reserve(org_a, target, payload_hash, approval, key=key)
    replay = _reserve(org_a, target, payload_hash, approval, key=key)
    assert replay.replayed is True
    assert replay.request.id == first.request.id

    with pytest.raises(ExecutionSafetyError) as exc_info:
        reserve_execution(
            org=org_a,
            channel=approval.channel,
            action=approval.action,
            target_hash=target.identifier_hash,
            payload_hash=_hash("changed-payload"),
            units=1,
            approval_id=approval.id,
            idempotency_key=key,
        )
    assert exc_info.value.code == "execution_idempotency_conflict"
    with pytest.raises(ExecutionSafetyError) as exc_info:
        _reserve(org_a, target, payload_hash, approval)
    assert exc_info.value.code == "approval_consumed"

    expired = issue_execution_approval(
        org=org_a,
        approved_by=admin_profile,
        channel=approval.channel,
        action=approval.action,
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=1,
    )
    ChannelExecutionApproval.objects.filter(id=expired.id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(ExecutionSafetyError) as exc_info:
        _reserve(org_a, target, payload_hash, expired)
    assert exc_info.value.code == "approval_expired"


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_organization_quota_cannot_be_bypassed_across_channels(
    org_a,
    admin_profile,
):
    email_target, email_payload, email_approval = _scope(
        org_a,
        admin_profile,
        org_limit=1,
    )
    _reserve(org_a, email_target, email_payload, email_approval)
    configure_channel(
        org=org_a,
        actor=admin_profile,
        channel=ExecutionChannel.WHATSAPP,
        enabled=True,
        test_mode=True,
        daily_limit=10,
        per_execution_limit=10,
    )
    whatsapp_identifier = "+15550000001"
    whatsapp_target = add_test_target(
        org=org_a,
        actor=admin_profile,
        channel=ExecutionChannel.WHATSAPP,
        identifier=whatsapp_identifier,
        safe_label="Dedicated WhatsApp target",
    )
    whatsapp_payload = _hash("whatsapp-payload")
    whatsapp_approval = issue_execution_approval(
        org=org_a,
        approved_by=admin_profile,
        channel=ExecutionChannel.WHATSAPP,
        action="send_message",
        target_hash=whatsapp_target.identifier_hash,
        payload_hash=whatsapp_payload,
        units=1,
    )
    with pytest.raises(ExecutionSafetyError) as exc_info:
        _reserve(org_a, whatsapp_target, whatsapp_payload, whatsapp_approval)
    assert exc_info.value.code == "organization_daily_limit_exceeded"
    org_control = OrganizationExecutionControl.objects.get(org=org_a)
    assert org_control.reserved_units + org_control.consumed_units == 1


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_cross_org_approval_and_request_ids_are_not_usable(
    org_a,
    org_b,
    admin_profile,
    profile_b,
):
    with database_org_context(org_a.id):
        target, payload_hash, approval = _scope(org_a, admin_profile)
        request = _reserve(org_a, target, payload_hash, approval).request
    with database_org_context(org_b.id):
        configure_organization_execution(
            org=org_b,
            actor=profile_b,
            enabled=True,
            daily_limit=10,
        )
        configure_channel(
            org=org_b,
            actor=profile_b,
            channel=ExecutionChannel.EMAIL,
            enabled=True,
            test_mode=False,
            daily_limit=10,
            per_execution_limit=10,
        )

        with pytest.raises(ExecutionSafetyError) as exc_info:
            reserve_execution(
                org=org_b,
                channel=approval.channel,
                action=approval.action,
                target_hash=target.identifier_hash,
                payload_hash=payload_hash,
                units=1,
                approval_id=approval.id,
                idempotency_key=uuid4(),
            )
        assert exc_info.value.code == "approval_not_found"
        with pytest.raises(ExternalExecutionRequest.DoesNotExist):
            mark_execution_sending(org=org_b, request_id=request.id)


@pytest.mark.django_db
@pytest.mark.parametrize("channel", [ExecutionChannel.WECHAT, ExecutionChannel.WECOM])
def test_unimplemented_wechat_channels_fail_closed_at_all_authorization_boundaries(
    org_a,
    admin_profile,
    channel,
):
    target_hash = hash_target_identifier(
        org=org_a,
        channel=channel,
        identifier="wxid_security_test",
    )
    calls = (
        lambda: configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=channel,
            enabled=True,
            test_mode=True,
            daily_limit=1,
            per_execution_limit=1,
        ),
        lambda: add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=channel,
            identifier="wxid_security_test",
            safe_label="Must remain disabled",
        ),
        lambda: issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=channel,
            action="send_message",
            target_hash=target_hash,
            payload_hash=_hash("disabled-payload"),
            units=1,
        ),
        lambda: reserve_execution(
            org=org_a,
            channel=channel,
            action="send_message",
            target_hash=target_hash,
            payload_hash=_hash("disabled-payload"),
            units=1,
            approval_id=uuid4(),
            idempotency_key=uuid4(),
        ),
    )
    for call in calls:
        with pytest.raises(ExecutionSafetyError) as exc_info:
            call()
        assert exc_info.value.code == "channel_not_implemented"


@pytest.mark.django_db(transaction=True)
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_postgres_parallel_reservations_cannot_overspend_organization_quota(
    org_a,
    admin_profile,
):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking is required.")
    target, payload_hash, first = _scope(org_a, admin_profile, org_limit=1)
    second = issue_execution_approval(
        org=org_a,
        approved_by=admin_profile,
        channel=first.channel,
        action=first.action,
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=1,
    )
    barrier = threading.Barrier(2)
    outcomes = []
    outcome_lock = threading.Lock()

    def reserve(approval_id):
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                barrier.wait(timeout=10)
                try:
                    reserve_execution(
                        org=org_a,
                        channel=ExecutionChannel.EMAIL,
                        action="send_message",
                        target_hash=target.identifier_hash,
                        payload_hash=payload_hash,
                        units=1,
                        approval_id=approval_id,
                        idempotency_key=uuid4(),
                    )
                    result = "ok"
                except ExecutionSafetyError as exc:
                    result = exc.code
                with outcome_lock:
                    outcomes.append(result)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=reserve, args=(item.id,), daemon=True) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["ok", "organization_daily_limit_exceeded"]
    org_control = OrganizationExecutionControl.objects.get(org=org_a)
    assert org_control.reserved_units + org_control.consumed_units == 1
