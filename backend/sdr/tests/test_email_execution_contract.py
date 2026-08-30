from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core import mail, signing
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.tenant_context import database_org_context
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
)
from leads.models import Lead
from sdr.email_execution import (
    EMAIL_SEND_ACTION,
    reserve_email_send,
)
from sdr.models import (
    LeadDelivery,
    LeadDeliveryKind,
    LeadDeliveryStatus,
    LeadIntake,
    LeadIntakeSource,
    LeadIntakeStatus,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDRResponseSettings,
)
from sdr.nurture import (
    enqueue_approved_nurture_delivery,
    nurture_email_execution_intent,
    process_nurture_email_job,
)
from sdr.response import (
    acknowledgement_email_execution_intent,
    enqueue_approved_acknowledgement_delivery,
    process_acknowledgement_email_job,
)


@pytest.fixture(autouse=True)
def secure_email_execution_settings(settings, org_a):
    settings.ALLOW_UNGUARDED_PROVIDER_IO = False
    settings.REAL_CHANNEL_EXECUTION_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    with database_org_context(org_a.id):
        yield


def _make_intake(org, *, suffix: str) -> LeadIntake:
    return LeadIntake.objects.create(
        org=org,
        source=LeadIntakeSource.WEBSITE_FORM,
        source_record_id=f"email-execution-{suffix}",
        raw_payload={
            "first_name": "Ada",
            "email": "ada@example.com",
            "company_name": "Analytical Engines Ltd",
        },
        normalized_payload={
            "identity": {
                "first_name": "Ada",
                "email": "ada@example.com",
            },
            "company": {"name": "Analytical Engines Ltd"},
        },
        status=LeadIntakeStatus.COMPLETED,
        processed_at=timezone.now(),
    )


def _make_acknowledgement(org, *, suffix: str = "ack"):
    configuration = SDRResponseSettings.objects.create(
        org=org,
        acknowledgement_email_enabled=True,
        acknowledgement_subject="Thanks {{ first_name }}",
        acknowledgement_body="Hi {{ first_name }}, we received your request.",
        acknowledgement_from_email="hello@example.test",
    )
    intake = _make_intake(org, suffix=suffix)
    delivery = LeadDelivery.objects.create(
        org=org,
        intake=intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
        recipient="ada@example.com",
    )
    return configuration, intake, delivery


def _make_nurture(org, *, suffix: str = "nurture"):
    SDRResponseSettings.objects.create(org=org, email_safety_enabled=False)
    lead = Lead.objects.create(
        org=org,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        company_name="Analytical Engines Ltd",
        status="new",
    )
    intake = _make_intake(org, suffix=suffix)
    intake.crm_lead = lead
    intake.save(update_fields=["crm_lead", "updated_at"])
    sequence = SDRNurtureSequence.objects.create(
        org=org,
        name=f"Execution contract {suffix}",
        is_active=True,
        from_email="sales@example.test",
    )
    step = SDRNurtureStep.objects.create(
        org=org,
        sequence=sequence,
        position=1,
        delay_minutes=0,
        subject_a="A follow-up for {{ first_name }}",
        body_a="Hi {{ first_name }}, can we help {{ company_name }}?",
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    delivery = LeadNurtureDelivery.objects.create(
        org=org,
        enrollment=enrollment,
        step=step,
        step_position=1,
        recipient="ada@example.com",
        subject_template=step.subject_a,
        body_template=step.body_a,
        scheduled_for=timezone.now() + timedelta(hours=1),
    )
    return enrollment, delivery


def _enable_email_execution(*, org, admin_profile, recipient: str):
    configure_organization_execution(
        org=org,
        actor=admin_profile,
        enabled=True,
        daily_limit=100,
    )
    configure_channel(
        org=org,
        actor=admin_profile,
        channel=ExecutionChannel.EMAIL,
        enabled=True,
        test_mode=True,
        daily_limit=100,
        per_execution_limit=1,
    )
    return add_test_target(
        org=org,
        actor=admin_profile,
        channel=ExecutionChannel.EMAIL,
        identifier=recipient,
        safe_label="Dedicated email test recipient",
    )


def _reserve_approved_email(
    *,
    org,
    admin_profile,
    delivery,
    intent,
) -> ExternalExecutionRequest:
    target = _enable_email_execution(
        org=org,
        admin_profile=admin_profile,
        recipient=delivery.recipient,
    )
    assert target.identifier_hash == intent.target_hash
    approval = issue_execution_approval(
        org=org,
        approved_by=admin_profile,
        channel=ExecutionChannel.EMAIL,
        action=EMAIL_SEND_ACTION,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
        idempotency_key=uuid4(),
    ).approval
    reservation = reserve_email_send(
        org=org,
        delivery_id=delivery.id,
        approval_id=approval.id,
        intent=intent,
    )
    return reservation.request


def _ack_payload(*, org, intake, delivery, request=None):
    payload = {
        "org_id": str(org.id),
        "intake_id": str(intake.id),
        "delivery_id": str(delivery.id),
    }
    if request is not None:
        payload["execution_request_id"] = str(request.id)
    return payload


def _nurture_payload(*, org, delivery, request=None):
    payload = {
        "org_id": str(org.id),
        "delivery_id": str(delivery.id),
    }
    if request is not None:
        payload["execution_request_id"] = str(request.id)
    return payload


@pytest.mark.django_db
def test_approved_acknowledgement_job_binds_request_and_disables_retries(
    org_a,
    admin_profile,
):
    _, _, delivery = _make_acknowledgement(org_a)
    intent = acknowledgement_email_execution_intent(delivery)
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=intent,
    )

    job = enqueue_approved_acknowledgement_delivery(
        delivery,
        execution_request_id=request.id,
    )

    assert job.payload["execution_request_id"] == str(request.id)
    assert job.payload["delivery_id"] == str(delivery.id)
    assert job.max_attempts == 1
    assert request.channel == ExecutionChannel.EMAIL
    assert request.action == EMAIL_SEND_ACTION
    assert request.idempotency_key == delivery.id
    assert request.target_hash == intent.target_hash
    assert request.payload_hash == intent.payload_hash
    assert request.units == 1


@pytest.mark.django_db
def test_approved_nurture_job_binds_request_and_disables_retries(
    org_a,
    admin_profile,
):
    _, delivery = _make_nurture(org_a)
    intent = nurture_email_execution_intent(delivery)
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=intent,
    )

    job = enqueue_approved_nurture_delivery(
        delivery,
        execution_request_id=request.id,
    )

    assert job.payload["execution_request_id"] == str(request.id)
    assert job.payload["delivery_id"] == str(delivery.id)
    assert job.max_attempts == 1


@pytest.mark.django_db
def test_nurture_execution_snapshot_is_stable_across_signing_clock_ticks(
    org_a,
    monkeypatch,
):
    _, delivery = _make_nurture(org_a, suffix="stable-snapshot")
    monkeypatch.setattr(
        signing.TimestampSigner,
        "timestamp",
        lambda self: "first-clock-tick",
    )
    first = nurture_email_execution_intent(delivery)
    monkeypatch.setattr(
        signing.TimestampSigner,
        "timestamp",
        lambda self: "second-clock-tick",
    )
    second = nurture_email_execution_intent(delivery)

    assert second == first


@pytest.mark.django_db
def test_legacy_acknowledgement_job_without_request_fails_before_provider_io(
    org_a,
    monkeypatch,
):
    _, intake, delivery = _make_acknowledgement(org_a)
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.response.send_mail", provider)

    with pytest.raises(PermanentJobError) as exc_info:
        process_acknowledgement_email_job(
            _ack_payload(org=org_a, intake=intake, delivery=delivery)
        )

    assert exc_info.value.code == "execution_approval_required"
    provider.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == LeadDeliveryStatus.FAILED


@pytest.mark.django_db
def test_acknowledgement_revalidates_current_rendered_snapshot_before_send(
    org_a,
    admin_profile,
    monkeypatch,
):
    configuration, intake, delivery = _make_acknowledgement(org_a)
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=acknowledgement_email_execution_intent(delivery),
    )
    configuration.acknowledgement_subject = "Changed after approval"
    configuration.save(update_fields=["acknowledgement_subject", "updated_at"])
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.response.send_mail", provider)

    with pytest.raises(PermanentJobError) as exc_info:
        process_acknowledgement_email_job(
            _ack_payload(
                org=org_a,
                intake=intake,
                delivery=delivery,
                request=request,
            )
        )

    assert exc_info.value.code == "email_execution_snapshot_changed"
    provider.assert_not_called()
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    assert request.error_code == "email_execution_snapshot_changed"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "request_status",
    [ExternalRequestStatus.SENDING, ExternalRequestStatus.UNKNOWN],
)
def test_acknowledgement_sending_or_unknown_request_is_never_replayed(
    org_a,
    admin_profile,
    monkeypatch,
    request_status,
):
    _, intake, delivery = _make_acknowledgement(
        org_a,
        suffix=f"non-replay-{request_status}",
    )
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=acknowledgement_email_execution_intent(delivery),
    )
    ExternalExecutionRequest.objects.filter(id=request.id).update(status=request_status)
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.response.send_mail", provider)

    with pytest.raises(PermanentJobError) as exc_info:
        process_acknowledgement_email_job(
            _ack_payload(
                org=org_a,
                intake=intake,
                delivery=delivery,
                request=request,
            )
        )

    assert exc_info.value.code == "email_execution_not_replayable"
    provider.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == LeadDeliveryStatus.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize(
    "accepted_status",
    [ExternalRequestStatus.ACCEPTED, ExternalRequestStatus.DELIVERED],
)
def test_accepted_or_delivered_acknowledgement_only_repairs_local_state(
    org_a,
    admin_profile,
    monkeypatch,
    accepted_status,
):
    _, intake, delivery = _make_acknowledgement(
        org_a,
        suffix=f"repair-{accepted_status}",
    )
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=acknowledgement_email_execution_intent(delivery),
    )
    mark_execution_sending(
        org=org_a,
        request_id=request.id,
        expected_status=ExternalRequestStatus.RESERVED,
    )
    mark_provider_accepted(org=org_a, request_id=request.id)
    if accepted_status == ExternalRequestStatus.DELIVERED:
        mark_execution_delivered(org=org_a, request_id=request.id)
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.response.send_mail", provider)

    result = process_acknowledgement_email_job(
        _ack_payload(
            org=org_a,
            intake=intake,
            delivery=delivery,
            request=request,
        )
    )

    provider.assert_not_called()
    request.refresh_from_db()
    delivery.refresh_from_db()
    assert result["status"] == LeadDeliveryStatus.SENT
    assert delivery.status == LeadDeliveryStatus.SENT
    assert request.status == ExternalRequestStatus.DELIVERED


@pytest.mark.django_db
def test_acknowledgement_persists_accepted_before_local_sent_and_can_repair(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, intake, delivery = _make_acknowledgement(org_a, suffix="accepted-repair")
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=acknowledgement_email_execution_intent(delivery),
    )
    provider = Mock(return_value=1)
    monkeypatch.setattr("sdr.response.send_mail", provider)
    from sdr import response as response_module

    finalize = response_module._finalize_acknowledgement_delivery
    monkeypatch.setattr(
        response_module,
        "_finalize_acknowledgement_delivery",
        Mock(side_effect=RuntimeError("local write failed")),
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_acknowledgement_email_job(
            _ack_payload(
                org=org_a,
                intake=intake,
                delivery=delivery,
                request=request,
            )
        )

    assert exc_info.value.code == "email_local_state_incomplete"
    request.refresh_from_db()
    delivery.refresh_from_db()
    assert request.status == ExternalRequestStatus.ACCEPTED
    assert delivery.status == LeadDeliveryStatus.FAILED
    provider.assert_called_once()

    monkeypatch.setattr(
        response_module,
        "_finalize_acknowledgement_delivery",
        finalize,
    )
    result = process_acknowledgement_email_job(
        _ack_payload(
            org=org_a,
            intake=intake,
            delivery=delivery,
            request=request,
        )
    )

    provider.assert_called_once()
    request.refresh_from_db()
    assert result["status"] == LeadDeliveryStatus.SENT
    assert request.status == ExternalRequestStatus.DELIVERED


@pytest.mark.django_db
def test_provider_exception_becomes_unknown_and_is_permanent(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, intake, delivery = _make_acknowledgement(org_a, suffix="provider-unknown")
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=acknowledgement_email_execution_intent(delivery),
    )
    monkeypatch.setattr(
        "sdr.response.send_mail",
        Mock(side_effect=TimeoutError("provider outcome unknown")),
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_acknowledgement_email_job(
            _ack_payload(
                org=org_a,
                intake=intake,
                delivery=delivery,
                request=request,
            )
        )

    assert exc_info.value.retryable is False
    assert exc_info.value.code == "email_execution_outcome_unknown"
    request.refresh_from_db()
    delivery.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert delivery.status == LeadDeliveryStatus.FAILED
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.EMAIL,
    )
    assert control.reserved_units == 0
    assert control.consumed_units == 1


@pytest.mark.django_db
def test_approved_nurture_send_finishes_request_without_replay(
    org_a,
    admin_profile,
):
    _, delivery = _make_nurture(org_a, suffix="success")
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=nurture_email_execution_intent(delivery),
    )

    result = process_nurture_email_job(
        _nurture_payload(org=org_a, delivery=delivery, request=request)
    )

    request.refresh_from_db()
    delivery.refresh_from_db()
    assert result["status"] == NurtureDeliveryStatus.SENT
    assert delivery.status == NurtureDeliveryStatus.SENT
    assert request.status == ExternalRequestStatus.DELIVERED
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_nurture_pre_provider_safety_error_releases_reservation(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, delivery = _make_nurture(org_a, suffix="safety-check-failed")
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=nurture_email_execution_intent(delivery),
    )
    monkeypatch.setattr(
        "sdr.nurture.reserve_delivery_send",
        Mock(side_effect=RuntimeError("local safety store unavailable")),
    )
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.nurture.EmailMultiAlternatives.send", provider)

    with pytest.raises(PermanentJobError) as exc_info:
        process_nurture_email_job(
            _nurture_payload(org=org_a, delivery=delivery, request=request)
        )

    assert exc_info.value.code == "email_safety_check_failed"
    provider.assert_not_called()
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    assert request.error_code == "email_safety_check_failed"


@pytest.mark.django_db
def test_nurture_provider_exception_is_unknown_and_not_retryable(
    org_a,
    admin_profile,
    monkeypatch,
):
    _, delivery = _make_nurture(org_a, suffix="unknown")
    request = _reserve_approved_email(
        org=org_a,
        admin_profile=admin_profile,
        delivery=delivery,
        intent=nurture_email_execution_intent(delivery),
    )
    monkeypatch.setattr(
        "sdr.nurture.EmailMultiAlternatives.send",
        Mock(side_effect=TimeoutError("provider outcome unknown")),
    )

    with pytest.raises(PermanentJobError) as exc_info:
        process_nurture_email_job(
            _nurture_payload(org=org_a, delivery=delivery, request=request)
        )

    assert exc_info.value.retryable is False
    assert exc_info.value.code == "email_execution_outcome_unknown"
    request.refresh_from_db()
    delivery.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert delivery.status == NurtureDeliveryStatus.FAILED
