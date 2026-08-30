import json
from dataclasses import asdict
from unittest.mock import Mock
from uuid import uuid4

import pytest
from automation.errors import PermanentJobError, RetryableJobError
from automation.models import AutomationJob
from automation.services import claim_job, fail_job
from sdr.models import (
    OutboundCampaignStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
)

from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_delivered,
    mark_execution_sending,
    mark_provider_accepted,
)
from integrations.models import (
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)
from integrations.providers.whatsapp.client import (
    WhatsAppCloudAPIError,
    WhatsAppCloudClient,
)
from integrations.providers.whatsapp.outbound import (
    WHATSAPP_MESSAGE_JOB,
    WHATSAPP_SEND_ACTION,
    enqueue_whatsapp_campaign_message,
    process_whatsapp_message_job,
    reserve_and_enqueue_whatsapp_message,
    retry_failed_whatsapp_messages,
    whatsapp_message_execution_intent,
)


@pytest.fixture(autouse=True)
def secure_whatsapp_settings(settings):
    settings.ALLOW_UNGUARDED_PROVIDER_IO = False
    settings.REAL_CHANNEL_EXECUTION_ENABLED = True


def _make_graph(org, *, suffix="one", create_message=True):
    route = WhatsAppPhoneRoute.objects.create(
        org=org,
        phone_number_id=f"sender-{suffix}",
    )
    connection = WhatsAppBusinessConnection(
        org=org,
        route=route,
        is_active=True,
    )
    connection.set_access_token(f"secret-token-{suffix}")
    connection.save()
    campaign = SDROutboundCampaign.objects.create(
        org=org,
        name=f"WhatsApp execution {suffix}",
        channels=["whatsapp"],
        whatsapp_template_name="industrial_intro",
        whatsapp_template_language="en_US",
        status=OutboundCampaignStatus.ACTIVE,
        run_count=1,
    )
    prospect = SDROutboundProspect.objects.create(
        org=org,
        campaign=campaign,
        phone="+1 555 123 4567",
        company_name=f"Factory {suffix}",
        country="US",
        dedupe_key=f"whatsapp-contract-{suffix}",
    )
    if not create_message:
        return connection, campaign, prospect, None
    message = WhatsAppMessage.objects.create(
        org=org,
        connection=connection,
        campaign=campaign,
        prospect=prospect,
        campaign_run=1,
        recipient="15551234567",
        template_name="industrial_intro",
        template_language="en_US",
    )
    return connection, campaign, prospect, message


def _execution_approval(*, org, actor, message):
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
        safe_label="Approved WhatsApp recipient",
    )
    intent = whatsapp_message_execution_intent(message)
    return issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=ExecutionChannel.WHATSAPP,
        action=WHATSAPP_SEND_ACTION,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=intent.units,
        idempotency_key=uuid4(),
    ).approval


def _approve_message(*, org, actor, message):
    approval = _execution_approval(org=org, actor=actor, message=message)
    return reserve_and_enqueue_whatsapp_message(
        message,
        approval_id=approval.id,
    )


@pytest.mark.django_db
def test_secure_campaign_creates_pending_message_without_unapproved_job(org_a):
    _, campaign, prospect, _ = _make_graph(
        org_a,
        suffix="pending",
        create_message=False,
    )

    message = enqueue_whatsapp_campaign_message(
        prospect=prospect,
        campaign=campaign,
        campaign_run=campaign.run_count,
    )

    assert message.status == WhatsAppMessageStatus.PENDING
    assert not AutomationJob.objects.filter(
        org=org_a,
        name=WHATSAPP_MESSAGE_JOB,
        payload__message_id=str(message.id),
    ).exists()


@pytest.mark.django_db
def test_intent_is_exact_deterministic_and_contains_no_recipient(org_a):
    connection, campaign, _, message = _make_graph(org_a, suffix="intent")
    second_prospect = SDROutboundProspect.objects.create(
        org=org_a,
        campaign=campaign,
        phone=message.recipient,
        company_name="Second factory",
        country="US",
        dedupe_key="whatsapp-contract-intent-second",
    )
    second_message = WhatsAppMessage.objects.create(
        org=org_a,
        connection=connection,
        campaign=campaign,
        prospect=second_prospect,
        campaign_run=message.campaign_run,
        recipient=message.recipient,
        template_name=message.template_name,
        template_language=message.template_language,
    )

    first = whatsapp_message_execution_intent(message)
    second = whatsapp_message_execution_intent(message)
    other = whatsapp_message_execution_intent(second_message)

    assert first == second
    assert len(first.target_hash) == 64
    assert len(first.payload_hash) == 64
    assert "15551234567" not in repr(asdict(first))
    assert other.target_hash == first.target_hash
    assert other.payload_hash != first.payload_hash
    message.template_language = "zh_CN"
    assert whatsapp_message_execution_intent(message).payload_hash != first.payload_hash


@pytest.mark.django_db
def test_approval_for_identical_provider_payload_cannot_authorize_other_message(
    org_a,
    admin_profile,
):
    connection, campaign, _, first = _make_graph(org_a, suffix="scope")
    second_prospect = SDROutboundProspect.objects.create(
        org=org_a,
        campaign=campaign,
        phone=first.recipient,
        company_name="Other scope factory",
        country="US",
        dedupe_key="whatsapp-contract-scope-second",
    )
    second = WhatsAppMessage.objects.create(
        org=org_a,
        connection=connection,
        campaign=campaign,
        prospect=second_prospect,
        campaign_run=first.campaign_run,
        recipient=first.recipient,
        template_name=first.template_name,
        template_language=first.template_language,
    )
    approval = _execution_approval(
        org=org_a,
        actor=admin_profile,
        message=first,
    )

    with pytest.raises(Exception) as exc_info:
        reserve_and_enqueue_whatsapp_message(second, approval_id=approval.id)

    assert getattr(exc_info.value, "code", "") == "approval_scope_mismatch"
    assert not ExternalExecutionRequest.objects.filter(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
        idempotency_key=second.id,
    ).exists()


@pytest.mark.django_db
def test_approved_enqueue_is_single_attempt_and_carries_request_id(
    org_a,
    admin_profile,
):
    _, _, _, message = _make_graph(org_a, suffix="enqueue")

    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )

    assert submission.replayed is False
    assert submission.request.status == ExternalRequestStatus.RESERVED
    assert submission.request.idempotency_key == message.id
    assert submission.job.max_attempts == 1
    assert submission.job.payload == {
        "org_id": str(org_a.id),
        "message_id": str(message.id),
        "execution_request_id": str(submission.request.id),
    }
    replay = reserve_and_enqueue_whatsapp_message(
        message,
        approval_id=submission.request.approval_id,
    )
    assert replay.request.id == submission.request.id
    assert replay.job.id == submission.job.id
    assert replay.replayed is True
    assert AutomationJob.objects.filter(
        org=org_a,
        name=WHATSAPP_MESSAGE_JOB,
        payload__message_id=str(message.id),
    ).count() == 1


@pytest.mark.django_db
def test_enqueue_failure_refunds_reserved_request_without_active_job(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="enqueue-failure")
    approval = _execution_approval(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound.enqueue_job",
        Mock(side_effect=RuntimeError("local queue ledger unavailable")),
    )

    with pytest.raises(RuntimeError, match="queue ledger unavailable"):
        reserve_and_enqueue_whatsapp_message(
            message,
            approval_id=approval.id,
        )

    request = ExternalExecutionRequest.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
        idempotency_key=message.id,
    )
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert request.status == ExternalRequestStatus.FAILED
    assert request.error_code == "whatsapp_enqueue_failed"
    assert control.reserved_units == 0
    assert control.consumed_units == 0
    assert not AutomationJob.objects.filter(
        org=org_a,
        name=WHATSAPP_MESSAGE_JOB,
        idempotency_key=f"whatsapp-approved-message:{message.id}",
    ).exists()


@pytest.mark.django_db
def test_replay_enqueue_failure_does_not_refund_an_active_winner(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="enqueue-winner")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound.enqueue_approved_whatsapp_message",
        Mock(side_effect=RuntimeError("second caller failed locally")),
    )

    with pytest.raises(RuntimeError, match="second caller failed"):
        reserve_and_enqueue_whatsapp_message(
            message,
            approval_id=submission.request.approval_id,
        )

    submission.request.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert submission.request.status == ExternalRequestStatus.RESERVED
    assert control.reserved_units == 1
    assert AutomationJob.objects.filter(id=submission.job.id).exists()


@pytest.mark.django_db
def test_approved_worker_sends_once_and_settles_execution(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="success")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    provider = Mock(return_value={"messages": [{"id": "wamid.success"}]})
    factory = Mock()
    factory.return_value.send_template = provider
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        factory,
    )

    result = process_whatsapp_message_job(submission.job.payload)

    provider.assert_called_once()
    factory.assert_called_once_with(
        org=org_a,
        execution_request_id=submission.request.id,
    )
    submission.request.refresh_from_db()
    message.refresh_from_db()
    assert result["status"] == WhatsAppMessageStatus.SENT
    assert message.status == WhatsAppMessageStatus.SENT
    assert message.provider_message_id == "wamid.success"
    assert submission.request.status == ExternalRequestStatus.ACCEPTED
    serialized_ledger_data = json.dumps(
        {"payload": submission.job.payload, "result": result},
        sort_keys=True,
    )
    assert "wamid.success" not in serialized_ledger_data
    assert "provider_message_id" not in result


@pytest.mark.django_db
def test_snapshot_change_refunds_without_provider_call(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="snapshot")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    message.template_name = "changed_after_approval"
    message.save(update_fields=["template_name", "updated_at"])
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        provider,
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_whatsapp_message_job(submission.job.payload)

    assert exc_info.value.code == "whatsapp_execution_snapshot_changed"
    provider.assert_not_called()
    submission.request.refresh_from_db()
    message.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert submission.request.status == ExternalRequestStatus.FAILED
    assert message.status == WhatsAppMessageStatus.FAILED
    assert control.reserved_units == 0
    assert control.consumed_units == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "request_status",
    [ExternalRequestStatus.SENDING, ExternalRequestStatus.UNKNOWN],
)
def test_sending_or_unknown_request_is_never_replayed(
    org_a,
    admin_profile,
    monkeypatch,
    request_status,
):
    _, _, _, message = _make_graph(org_a, suffix=f"no-replay-{request_status}")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    ExternalExecutionRequest.objects.filter(id=submission.request.id).update(
        status=request_status,
    )
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        provider,
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_whatsapp_message_job(submission.job.payload)

    assert exc_info.value.code == "whatsapp_execution_not_replayable"
    provider.assert_not_called()


@pytest.mark.django_db
def test_accepted_sent_request_converges_without_provider_replay(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="converge")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    mark_execution_sending(
        org=org_a,
        request_id=submission.request.id,
        expected_status=ExternalRequestStatus.RESERVED,
    )
    mark_provider_accepted(org=org_a, request_id=submission.request.id)
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id="wamid.converge",
    )
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        provider,
    )

    result = process_whatsapp_message_job(submission.job.payload)

    provider.assert_not_called()
    submission.request.refresh_from_db()
    assert result["status"] == WhatsAppMessageStatus.SENT
    assert submission.request.status == ExternalRequestStatus.ACCEPTED


@pytest.mark.django_db
def test_reserved_request_never_refunds_or_downgrades_local_sent_evidence(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="reserved-sent")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    WhatsAppMessage.objects.filter(id=message.id).update(
        status=WhatsAppMessageStatus.SENT,
        provider_message_id="wamid.existing-evidence",
    )
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        provider,
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_whatsapp_message_job(submission.job.payload)

    assert exc_info.value.code == "whatsapp_execution_state_conflict"
    provider.assert_not_called()
    submission.request.refresh_from_db()
    message.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert submission.request.status == ExternalRequestStatus.RESERVED
    assert message.status == WhatsAppMessageStatus.SENT
    assert message.provider_message_id == "wamid.existing-evidence"
    assert control.reserved_units == 1
    assert control.consumed_units == 0


@pytest.mark.django_db
def test_explicit_provider_rejection_refunds_reservation(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, campaign, _, message = _make_graph(org_a, suffix="rejected")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    client = Mock()
    private_provider_error = "Meta rejected private recipient 15551234567"
    client.send_template.side_effect = WhatsAppCloudAPIError(
        private_provider_error,
        retryable=False,
        status_code=400,
        error_code="whatsapp_provider_400",
        outcome_known=True,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        Mock(return_value=client),
    )

    claimed = claim_job(job_id=submission.job.id, org_id=org_a.id)
    assert claimed is not None
    with pytest.raises(PermanentJobError) as exc_info:
        process_whatsapp_message_job(submission.job.payload)
    fail_job(
        claimed=claimed,
        error_code=exc_info.value.code,
        error_message=str(exc_info.value),
        retryable=exc_info.value.retryable,
    )

    assert exc_info.value.code == "whatsapp_provider_400"
    assert str(exc_info.value) == (
        "WhatsApp provider rejected the message before acceptance."
    )
    assert private_provider_error not in str(exc_info.value)
    submission.request.refresh_from_db()
    message.refresh_from_db()
    submission.job.refresh_from_db()
    attempt = submission.job.attempts.get(attempt_number=1)
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert submission.request.status == ExternalRequestStatus.FAILED
    assert message.status == WhatsAppMessageStatus.FAILED
    assert message.error_message == (
        "WhatsApp provider rejected the message before acceptance."
    )
    assert private_provider_error not in message.error_message
    assert private_provider_error not in submission.job.last_error_message
    assert private_provider_error not in attempt.error_message
    assert control.reserved_units == 0
    assert control.consumed_units == 0
    assert retry_failed_whatsapp_messages(campaign) == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("retryable", "outcome_known", "expected_detail"),
    [
        (
            False,
            True,
            "WhatsApp provider rejected the message before acceptance.",
        ),
        (
            True,
            False,
            "The WhatsApp provider request could not be completed safely.",
        ),
    ],
)
def test_legacy_provider_error_is_sanitized_before_automation_ledger(
    org_a,
    settings,
    monkeypatch,
    retryable,
    outcome_known,
    expected_detail,
):
    settings.ALLOW_UNGUARDED_PROVIDER_IO = True
    _, campaign, prospect, _ = _make_graph(
        org_a,
        suffix=f"legacy-error-{retryable}",
        create_message=False,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._safe_dispatch",
        lambda _job: None,
    )
    message = enqueue_whatsapp_campaign_message(
        prospect=prospect,
        campaign=campaign,
        campaign_run=campaign.run_count,
    )
    job = AutomationJob.objects.get(
        org=org_a,
        name=WHATSAPP_MESSAGE_JOB,
        payload__message_id=str(message.id),
    )
    private_provider_error = "Private provider failure for 15551234567"
    client = Mock()
    client.send_template.side_effect = WhatsAppCloudAPIError(
        private_provider_error,
        retryable=retryable,
        error_code="whatsapp_provider_131000",
        outcome_known=outcome_known,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        Mock(return_value=client),
    )
    claimed = claim_job(job_id=job.id, org_id=org_a.id)
    assert claimed is not None

    with pytest.raises((PermanentJobError, RetryableJobError)) as exc_info:
        process_whatsapp_message_job(job.payload)
    fail_job(
        claimed=claimed,
        error_code=exc_info.value.code,
        error_message=str(exc_info.value),
        retryable=exc_info.value.retryable,
    )

    message.refresh_from_db()
    job.refresh_from_db()
    attempt = job.attempts.get(attempt_number=1)
    assert str(exc_info.value) == expected_detail
    assert message.error_message == expected_detail
    assert private_provider_error not in message.error_message
    assert private_provider_error not in job.last_error_message
    assert private_provider_error not in attempt.error_message


@pytest.mark.django_db
def test_transport_uncertainty_is_charged_unknown_and_never_retried(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, campaign, _, message = _make_graph(org_a, suffix="unknown")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    client = Mock()
    client.send_template.side_effect = WhatsAppCloudAPIError(
        "transport outcome unknown",
        retryable=True,
        error_code="whatsapp_transport_error",
        outcome_known=False,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        Mock(return_value=client),
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_whatsapp_message_job(submission.job.payload)

    assert exc_info.value.retryable is False
    assert exc_info.value.code == "whatsapp_execution_outcome_unknown"
    submission.request.refresh_from_db()
    message.refresh_from_db()
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.WHATSAPP,
    )
    assert submission.request.status == ExternalRequestStatus.UNKNOWN
    assert message.status == getattr(
        WhatsAppMessageStatus,
        "UNKNOWN",
        WhatsAppMessageStatus.FAILED,
    )
    assert control.reserved_units == 0
    assert control.consumed_units == 1
    assert retry_failed_whatsapp_messages(campaign) == 0


@pytest.mark.django_db
def test_webhook_race_cannot_downgrade_delivered_message_to_unknown(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, _, _, message = _make_graph(org_a, suffix="webhook-race")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    client = Mock()
    client.send_template.side_effect = WhatsAppCloudAPIError(
        "transport outcome unknown",
        retryable=True,
        error_code="whatsapp_transport_error",
        outcome_known=False,
    )
    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound._client",
        Mock(return_value=client),
    )
    real_mark_accepted = mark_provider_accepted

    def webhook_wins(**kwargs):
        accepted = real_mark_accepted(**kwargs)
        mark_execution_delivered(org=org_a, request_id=submission.request.id)
        WhatsAppMessage.objects.filter(id=message.id).update(
            status=WhatsAppMessageStatus.DELIVERED,
            provider_message_id="wamid.webhook-race",
        )
        return accepted

    monkeypatch.setattr(
        "integrations.providers.whatsapp.outbound.mark_provider_accepted",
        webhook_wins,
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_whatsapp_message_job(submission.job.payload)

    assert exc_info.value.code == "whatsapp_execution_outcome_unknown"
    submission.request.refresh_from_db()
    message.refresh_from_db()
    assert submission.request.status == ExternalRequestStatus.DELIVERED
    assert message.status == WhatsAppMessageStatus.DELIVERED


class _ProviderErrorResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {"error": {"message": "Provider rejected request", "code": 131000}}


class _ProviderErrorSession:
    def __init__(self, status_code):
        self.status_code = status_code

    def post(self, *args, **kwargs):
        return _ProviderErrorResponse(self.status_code)


@pytest.mark.parametrize(
    ("status_code", "outcome_known"),
    [(400, True), (408, False), (429, False), (500, False), (503, False)],
)
def test_client_only_treats_deterministic_4xx_as_known_rejection(
    settings,
    status_code,
    outcome_known,
):
    settings.ALLOW_UNGUARDED_PROVIDER_IO = True
    client = WhatsAppCloudClient(
        api_version="v25.0",
        session=_ProviderErrorSession(status_code),
    )

    with pytest.raises(WhatsAppCloudAPIError) as exc_info:
        client.send_template(
            phone_number_id="sender",
            access_token="secret",
            recipient="15551234567",
            template_name="approved_template",
            language_code="en_US",
        )

    assert exc_info.value.outcome_known is outcome_known
    assert str(exc_info.value) == "WhatsApp Cloud API rejected the message"
    assert "Provider rejected request" not in str(exc_info.value)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("phone_number_id", "different-sender"),
        ("recipient", "16661234567"),
        ("template_name", "different_template"),
        ("language_code", "zh_CN"),
    ],
)
def test_client_rechecks_actual_provider_arguments_before_post(
    org_a,
    admin_profile,
    field,
    replacement,
):
    _, _, _, message = _make_graph(org_a, suffix=f"last-mile-{field}")
    submission = _approve_message(
        org=org_a,
        actor=admin_profile,
        message=message,
    )
    mark_execution_sending(org=org_a, request_id=submission.request.id)
    session = Mock()
    client = WhatsAppCloudClient(
        api_version="v25.0",
        session=session,
        org=org_a,
        execution_request_id=submission.request.id,
    )
    arguments = {
        "phone_number_id": message.connection.phone_number_id,
        "access_token": "private-access-token",
        "recipient": message.recipient,
        "template_name": message.template_name,
        "language_code": message.template_language,
        "message_id": message.id,
        "campaign_id": message.campaign_id,
        "campaign_run": message.campaign_run,
    }
    arguments[field] = replacement

    with pytest.raises(WhatsAppCloudAPIError) as exc_info:
        client.send_template(**arguments)

    assert exc_info.value.error_code == "execution_scope_mismatch"
    assert exc_info.value.outcome_known is True
    session.post.assert_not_called()
