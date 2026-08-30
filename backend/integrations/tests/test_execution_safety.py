import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from integrations.execution_safety import (
    ExecutionSafetyError,
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_delivered,
    mark_execution_sending,
    mark_provider_accepted,
    reconcile_stale_reserved,
    reconcile_stale_sending,
    release_execution,
    reserve_execution,
)
from integrations.models import (
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    OrganizationExecutionControl,
)
from integrations.providers.apollo.client import ApolloClient
from integrations.providers.feishu_base.client import FeishuBaseClient
from integrations.providers.linkedin.client import LinkedInInvitationsClient
from integrations.providers.whatsapp.client import WhatsAppCloudClient


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _setup(org, actor, *, channel="email", daily=10, per_execution=5, org_daily=20):
    configure_organization_execution(
        org=org, actor=actor, enabled=True, daily_limit=org_daily
    )
    configure_channel(
        org=org,
        actor=actor,
        channel=channel,
        enabled=True,
        test_mode=True,
        daily_limit=daily,
        per_execution_limit=per_execution,
    )
    return add_test_target(
        org=org,
        actor=actor,
        channel=channel,
        identifier="Test@Example.com" if channel == "email" else "+86 138-0000-1234",
        safe_label="Dedicated test target",
    )


def _approve(org, actor, target, *, units=1, channel="email", payload_hash=None):
    payload_hash = payload_hash or _hash("safe-payload-v1")
    approval = issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=channel,
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=units,
        idempotency_key=uuid4(),
    ).approval
    return approval, payload_hash


@pytest.mark.django_db
def test_environment_and_database_controls_default_fail_closed(org_a, admin_profile):
    target = _setup(org_a, admin_profile)
    approval, payload_hash = _approve(org_a, admin_profile, target)
    with pytest.raises(ExecutionSafetyError) as exc:
        reserve_execution(
            org=org_a,
            channel="email",
            action="send_message",
            target_hash=target.identifier_hash,
            payload_hash=payload_hash,
            units=1,
            approval_id=approval.id,
            idempotency_key=uuid4(),
        )
    assert exc.value.code == "environment_execution_disabled"


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_reserve_accept_deliver_updates_both_atomic_quota_projections(
    org_a, admin_profile
):
    target = _setup(org_a, admin_profile)
    approval, payload_hash = _approve(org_a, admin_profile, target, units=2)
    key = uuid4()
    reservation = reserve_execution(
        org=org_a,
        channel="email",
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=2,
        approval_id=approval.id,
        idempotency_key=key,
    )
    replay = reserve_execution(
        org=org_a,
        channel="email",
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=2,
        approval_id=approval.id,
        idempotency_key=key,
    )
    assert replay.replayed is True
    mark_execution_sending(org=org_a, request_id=reservation.request.id)
    accepted = mark_provider_accepted(
        org=org_a, request_id=reservation.request.id, provider_reference="provider-secret-id"
    )
    delivered = mark_execution_delivered(org=org_a, request_id=accepted.id)
    assert delivered.status == ExternalRequestStatus.DELIVERED
    assert delivered.provider_reference_hash == _hash("provider-secret-id")
    channel_control = ChannelExecutionControl.objects.get(org=org_a, channel="email")
    org_control = OrganizationExecutionControl.objects.get(org=org_a)
    assert (channel_control.reserved_units, channel_control.consumed_units) == (0, 2)
    assert (org_control.reserved_units, org_control.consumed_units) == (0, 2)


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_kill_switch_after_reservation_persists_failure_releases_and_raises(
    org_a, admin_profile
):
    target = _setup(org_a, admin_profile)
    approval, payload_hash = _approve(org_a, admin_profile, target)
    reservation = reserve_execution(
        org=org_a,
        channel="email",
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=1,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    )
    control = ChannelExecutionControl.objects.get(org=org_a, channel="email")
    control.enabled = False
    control.save(update_fields=["enabled", "updated_at"])
    with pytest.raises(ExecutionSafetyError) as exc:
        mark_execution_sending(org=org_a, request_id=reservation.request.id)
    assert exc.value.code == "channel_disabled"
    request = ExternalExecutionRequest.objects.get(id=reservation.request.id)
    assert request.status == ExternalRequestStatus.FAILED
    control.refresh_from_db()
    org_control = OrganizationExecutionControl.objects.get(org=org_a)
    assert control.reserved_units == org_control.reserved_units == 0


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_failed_pre_send_releases_but_unknown_consumes_and_can_reconcile(
    org_a, admin_profile
):
    target = _setup(org_a, admin_profile)
    approval, payload_hash = _approve(org_a, admin_profile, target)
    failed = reserve_execution(
        org=org_a,
        channel="email",
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=1,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    ).request
    release_execution(org=org_a, request_id=failed.id, error_code="network_unavailable")

    approval2, payload_hash2 = _approve(org_a, admin_profile, target)
    uncertain = reserve_execution(
        org=org_a,
        channel="email",
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash2,
        units=1,
        approval_id=approval2.id,
        idempotency_key=uuid4(),
    ).request
    mark_execution_sending(org=org_a, request_id=uncertain.id)
    ExternalExecutionRequest.objects.filter(id=uncertain.id).update(
        sending_at=timezone.now() - timedelta(hours=1)
    )
    reconciled = reconcile_stale_sending(
        org=org_a, older_than=timezone.now() - timedelta(minutes=30)
    )
    assert uncertain.id in reconciled
    uncertain.refresh_from_db()
    assert uncertain.status == ExternalRequestStatus.UNKNOWN
    assert OrganizationExecutionControl.objects.get(org=org_a).consumed_units == 1


@pytest.mark.django_db
@override_settings(REAL_CHANNEL_EXECUTION_ENABLED=True)
def test_stale_reserved_is_failed_and_refunded_without_entering_provider_state(
    org_a, admin_profile
):
    target = _setup(org_a, admin_profile)
    approval, payload_hash = _approve(org_a, admin_profile, target, units=2)
    request = reserve_execution(
        org=org_a,
        channel="email",
        action="send_message",
        target_hash=target.identifier_hash,
        payload_hash=payload_hash,
        units=2,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    ).request
    ExternalExecutionRequest.objects.filter(id=request.id).update(
        reserved_at=timezone.now() - timedelta(hours=1)
    )

    reconciled = reconcile_stale_reserved(
        org=org_a,
        older_than=timezone.now() - timedelta(minutes=30),
    )

    request.refresh_from_db()
    assert reconciled == [request.id]
    assert request.status == ExternalRequestStatus.FAILED
    assert request.error_code == "stale_reservation_released"
    channel_control = ChannelExecutionControl.objects.get(org=org_a, channel="email")
    org_control = OrganizationExecutionControl.objects.get(org=org_a)
    assert channel_control.reserved_units == org_control.reserved_units == 0
    assert channel_control.consumed_units == org_control.consumed_units == 0


@pytest.mark.django_db
def test_unimplemented_channels_and_non_admin_approval_fail_closed(
    org_a, admin_profile, user_profile
):
    for channel in (ExecutionChannel.WECHAT, ExecutionChannel.WECOM):
        with pytest.raises(ExecutionSafetyError) as exc:
            configure_channel(
                org=org_a,
                actor=admin_profile,
                channel=channel,
                enabled=True,
                test_mode=True,
                daily_limit=1,
                per_execution_limit=1,
            )
        assert exc.value.code == "channel_not_implemented"
    with pytest.raises(ExecutionSafetyError) as exc:
        issue_execution_approval(
            org=org_a,
            approved_by=user_profile,
            channel="email",
            action="send_message",
            target_hash=_hash("target"),
            payload_hash=_hash("payload"),
            units=1,
            idempotency_key=uuid4(),
        )
    assert exc.value.code == "approval_permission_denied"


class _NetworkMustNotRun:
    def request(self, *args, **kwargs):
        raise AssertionError("provider network was called")

    def post(self, *args, **kwargs):
        raise AssertionError("provider network was called")


@pytest.mark.parametrize(
    "invoke",
    [
        lambda session: ApolloClient(api_key="test", session=session).search_people(
            filters={}, page=1, per_page=1
        ),
        lambda session: FeishuBaseClient(session=session).tenant_access_token(
            app_id="test", app_secret="test"
        ),
        lambda session: WhatsAppCloudClient(
            api_version="v20.0", session=session
        ).send_template(
            phone_number_id="1",
            access_token="test",
            recipient="8613800001234",
            template_name="test",
            language_code="en",
        ),
        lambda session: LinkedInInvitationsClient(session=session).send_email_invitation(
            access_token="test", recipient_email="test@example.com"
        ),
    ],
)
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=False,
)
def test_legacy_provider_clients_fail_before_network_without_execution_request(invoke):
    with pytest.raises(RuntimeError) as exc:
        invoke(_NetworkMustNotRun())
    assert getattr(exc.value, "retryable", True) is False
    assert getattr(exc.value, "error_code", "") == "execution_approval_required"
