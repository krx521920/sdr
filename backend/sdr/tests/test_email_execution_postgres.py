from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Barrier, Event, Lock
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection
from django.test import override_settings
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.tenant_context import database_org_context
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
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
from sdr.email_execution import EMAIL_SEND_ACTION, reserve_email_send
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
from sdr.nurture import nurture_email_execution_intent, process_nurture_email_job
from sdr.response import (
    acknowledgement_email_execution_intent,
    process_acknowledgement_email_job,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL row locks and RLS are required.",
    ),
    pytest.mark.django_db(transaction=True),
]


def _make_acknowledgement(org, *, suffix: str):
    SDRResponseSettings.objects.create(
        org=org,
        acknowledgement_email_enabled=True,
        acknowledgement_subject="Thanks {{ first_name }}",
        acknowledgement_body="Hi {{ first_name }}, we received your request.",
        acknowledgement_from_email="hello@example.test",
    )
    intake = LeadIntake.objects.create(
        org=org,
        source=LeadIntakeSource.WEBSITE_FORM,
        source_record_id=f"email-postgres-{suffix}",
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
    delivery = LeadDelivery.objects.create(
        org=org,
        intake=intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
        recipient="ada@example.com",
    )
    return intake, delivery


def _make_nurture(org, *, suffix: str):
    SDRResponseSettings.objects.create(org=org, email_safety_enabled=False)
    lead = Lead.objects.create(
        org=org,
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        company_name="Analytical Engines Ltd",
        status="new",
    )
    intake = LeadIntake.objects.create(
        org=org,
        source=LeadIntakeSource.WEBSITE_FORM,
        source_record_id=f"email-postgres-nurture-{suffix}",
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
        crm_lead=lead,
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org,
        name=f"PostgreSQL execution contract {suffix}",
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
        scheduled_for=timezone.now(),
    )
    return enrollment, delivery


def _reserve_approved_email(*, org, actor, delivery, intent=None):
    intent = intent or acknowledgement_email_execution_intent(delivery)
    configure_organization_execution(
        org=org,
        actor=actor,
        enabled=True,
        daily_limit=10,
    )
    configure_channel(
        org=org,
        actor=actor,
        channel=ExecutionChannel.EMAIL,
        enabled=True,
        test_mode=True,
        daily_limit=10,
        per_execution_limit=1,
    )
    target = add_test_target(
        org=org,
        actor=actor,
        channel=ExecutionChannel.EMAIL,
        identifier=delivery.recipient,
        safe_label="Dedicated PostgreSQL email recipient",
    )
    assert target.identifier_hash == intent.target_hash
    approval = issue_execution_approval(
        org=org,
        approved_by=actor,
        channel=ExecutionChannel.EMAIL,
        action=EMAIL_SEND_ACTION,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
        idempotency_key=uuid4(),
    ).approval
    return reserve_email_send(
        org=org,
        delivery_id=delivery.id,
        approval_id=approval.id,
        intent=intent,
    ).request


def _payload(*, org, intake, delivery, request):
    return {
        "org_id": str(org.id),
        "intake_id": str(intake.id),
        "delivery_id": str(delivery.id),
        "execution_request_id": str(request.id),
    }


class _BlockingEmailProvider:
    def __init__(self, *, request_id):
        self.request_id = request_id
        self.call_count = 0
        self.lock = Lock()
        self.started = Event()
        self.release = Event()
        self.observed_statuses = []

    def __call__(self, **kwargs):
        del kwargs
        status = ExternalExecutionRequest.objects.get(id=self.request_id).status
        with self.lock:
            self.call_count += 1
            self.observed_statuses.append(status)
        self.started.set()
        assert self.release.wait(timeout=10), "concurrent email test was not released"
        return 1


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_accepted_nurture_email_converges_locally_without_provider_replay(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        enrollment, delivery = _make_nurture(org_a, suffix="accepted")
        intent = nurture_email_execution_intent(delivery)
        request = _reserve_approved_email(
            org=org_a,
            actor=admin_profile,
            delivery=delivery,
            intent=intent,
        )
        mark_execution_sending(
            org=org_a,
            request_id=request.id,
            expected_status=ExternalRequestStatus.RESERVED,
        )
        mark_provider_accepted(org=org_a, request_id=request.id)

    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.nurture.EmailMultiAlternatives.send", provider)

    with database_org_context(org_a.id):
        result = process_nurture_email_job(
            {
                "org_id": str(org_a.id),
                "delivery_id": str(delivery.id),
                "execution_request_id": str(request.id),
            }
        )
        request.refresh_from_db()
        delivery.refresh_from_db()
        enrollment.refresh_from_db()

    provider.assert_not_called()
    assert result["status"] == NurtureDeliveryStatus.SENT
    assert delivery.status == NurtureDeliveryStatus.SENT
    assert enrollment.current_step_position == delivery.step_position
    assert request.status == ExternalRequestStatus.DELIVERED


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_two_postgres_workers_claim_one_email_request_for_one_provider_call(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
):
    with database_org_context(org_a.id):
        intake, delivery = _make_acknowledgement(org_a, suffix="concurrent")
        request = _reserve_approved_email(
            org=org_a,
            actor=admin_profile,
            delivery=delivery,
        )
        payload = _payload(
            org=org_a,
            intake=intake,
            delivery=delivery,
            request=request,
        )

    provider = _BlockingEmailProvider(request_id=request.id)
    monkeypatch.setattr("sdr.response.send_mail", provider)
    start = Barrier(2)

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                start.wait(timeout=10)
                try:
                    result = process_acknowledgement_email_job(payload)
                except PermanentJobError as exc:
                    return exc.code
                return result["status"]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        assert provider.started.wait(timeout=10), "no worker reached the email provider"
        done, _pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
        assert done, "the competing worker did not observe the claimed request"
        provider.release.set()
        outcomes = [future.result(timeout=10) for future in futures]

    assert provider.call_count == 1
    assert provider.observed_statuses == [ExternalRequestStatus.SENDING]
    assert sorted(outcomes) == [
        "email_execution_not_replayable",
        LeadDeliveryStatus.SENT,
    ]
    with database_org_context(org_a.id):
        request.refresh_from_db()
        delivery.refresh_from_db()
        control = ChannelExecutionControl.objects.get(
            org=org_a,
            channel=ExecutionChannel.EMAIL,
        )
        assert request.status == ExternalRequestStatus.DELIVERED
        assert delivery.status == LeadDeliveryStatus.SENT
        assert control.reserved_units == 0
        assert control.consumed_units == 1


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_cross_org_email_request_is_invisible_to_worker(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
    profile_b,
    monkeypatch,
):
    with database_org_context(org_a.id):
        intake_a, delivery_a = _make_acknowledgement(org_a, suffix="tenant-a")
    with database_org_context(org_b.id):
        _intake_b, delivery_b = _make_acknowledgement(org_b, suffix="tenant-b")
        request_b = _reserve_approved_email(
            org=org_b,
            actor=profile_b,
            delivery=delivery_b,
        )

    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.response.send_mail", provider)
    payload = _payload(
        org=org_a,
        intake=intake_a,
        delivery=delivery_a,
        request=request_b,
    )

    with database_org_context(org_a.id):
        assert not ExternalExecutionRequest.objects.filter(id=request_b.id).exists()
        with pytest.raises(PermanentJobError) as exc_info:
            process_acknowledgement_email_job(payload)

    assert exc_info.value.code == "email_execution_request_not_found"
    provider.assert_not_called()
    with database_org_context(org_b.id):
        visible = ExternalExecutionRequest.objects.get(id=request_b.id)
        assert visible.status == ExternalRequestStatus.RESERVED


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
@pytest.mark.parametrize(
    "request_status",
    [ExternalRequestStatus.SENDING, ExternalRequestStatus.UNKNOWN],
)
def test_postgres_worker_never_replays_sending_or_unknown_email_request(
    transactional_db,
    org_a,
    admin_profile,
    monkeypatch,
    request_status,
):
    with database_org_context(org_a.id):
        intake, delivery = _make_acknowledgement(
            org_a,
            suffix=f"non-replay-{request_status}",
        )
        request = _reserve_approved_email(
            org=org_a,
            actor=admin_profile,
            delivery=delivery,
        )
        ExternalExecutionRequest.objects.filter(id=request.id).update(
            status=request_status
        )
        payload = _payload(
            org=org_a,
            intake=intake,
            delivery=delivery,
            request=request,
        )

    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("sdr.response.send_mail", provider)

    with database_org_context(org_a.id):
        with pytest.raises(PermanentJobError) as exc_info:
            process_acknowledgement_email_job(payload)

        request.refresh_from_db()
        delivery.refresh_from_db()
        assert request.status == request_status
        assert delivery.status == LeadDeliveryStatus.PENDING

    assert exc_info.value.code == "email_execution_not_replayable"
    provider.assert_not_called()
