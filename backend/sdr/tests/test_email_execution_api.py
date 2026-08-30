from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from automation.models import AutomationJob
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_sending,
    mark_provider_accepted,
)
from integrations.models import (
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
)
from leads.models import Lead
from sdr.email_execution import EMAIL_SEND_ACTION, reserve_email_send
from sdr.models import (
    LeadDelivery,
    LeadDeliveryKind,
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
from sdr.nurture import ensure_enrollment_schedule, nurture_email_execution_intent
from sdr.response import (
    acknowledgement_email_execution_intent,
    schedule_acknowledgement_job,
)

ACK_URL = "/api/sdr/email/acknowledgements/{delivery_id}/execution/"
NURTURE_URL = "/api/sdr/email/nurture/{delivery_id}/execution/"


@pytest.fixture(autouse=True)
def secure_email_api_settings(settings):
    settings.ALLOW_UNGUARDED_PROVIDER_IO = False
    settings.REAL_CHANNEL_EXECUTION_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.ROOT_URLCONF = "sdr.tests.urls"


def _make_intake(org, *, suffix: str) -> LeadIntake:
    return LeadIntake.objects.create(
        org=org,
        source=LeadIntakeSource.WEBSITE_FORM,
        source_record_id=f"email-api-{suffix}",
        raw_payload={
            "first_name": "SecretAda",
            "email": "private-email-address@example.com",
            "company_name": "Private Analytical Engines",
        },
        normalized_payload={
            "identity": {
                "first_name": "SecretAda",
                "email": "private-email-address@example.com",
            },
            "company": {"name": "Private Analytical Engines"},
        },
        status=LeadIntakeStatus.COMPLETED,
        processed_at=timezone.now(),
    )


def _make_acknowledgement(org, *, suffix: str = "ack") -> LeadDelivery:
    SDRResponseSettings.objects.create(
        org=org,
        acknowledgement_email_enabled=True,
        acknowledgement_subject="Highly Confidential Subject {{ first_name }}",
        acknowledgement_body="Private body for {{ first_name }}.",
        acknowledgement_from_email="sender@example.test",
    )
    intake = _make_intake(org, suffix=suffix)
    return LeadDelivery.objects.create(
        org=org,
        intake=intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
        recipient="private-email-address@example.com",
    )


def _make_nurture(org, *, suffix: str = "nurture") -> LeadNurtureDelivery:
    intake = _make_intake(org, suffix=suffix)
    lead = Lead.objects.create(
        org=org,
        first_name="SecretAda",
        last_name="PrivateLovelace",
        email="private-email-address@example.com",
        company_name="Private Analytical Engines",
        status="new",
    )
    intake.crm_lead = lead
    intake.save(update_fields=["crm_lead", "updated_at"])
    sequence = SDRNurtureSequence.objects.create(
        org=org,
        name=f"Private nurture {suffix}",
        is_active=True,
        from_email="sender@example.test",
    )
    step = SDRNurtureStep.objects.create(
        org=org,
        sequence=sequence,
        position=1,
        delay_minutes=60,
        subject_a="Private nurture subject {{ first_name }}",
        body_a="Private nurture body for {{ company_name }}.",
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    return LeadNurtureDelivery.objects.create(
        org=org,
        enrollment=enrollment,
        step=step,
        step_position=1,
        recipient="private-email-address@example.com",
        subject_template=step.subject_a,
        body_template=step.body_a,
        scheduled_for=timezone.now() + timedelta(hours=1),
    )


def _enable_email(org, admin_profile, recipient: str) -> None:
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
    add_test_target(
        org=org,
        actor=admin_profile,
        channel=ExecutionChannel.EMAIL,
        identifier=recipient,
        safe_label="Dedicated email API target",
    )


def _approval(org, admin_profile, intent):
    return issue_execution_approval(
        org=org,
        approved_by=admin_profile,
        channel=ExecutionChannel.EMAIL,
        action=EMAIL_SEND_ACTION,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=intent.units,
    ).approval


def _assert_safe_intent(response, delivery):
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["approval_required"] is True
    assert body["intent"] == {
        "channel": "email",
        "action": "send_email",
        "delivery_id": str(delivery.id),
        "target_sha256": body["intent"]["target_sha256"],
        "payload_sha256": body["intent"]["payload_sha256"],
        "units": 1,
    }
    assert len(body["intent"]["target_sha256"]) == 64
    assert len(body["intent"]["payload_sha256"]) == 64
    serialized = str(body).casefold()
    for private_value in (
        "private-email-address@example.com",
        "secretada",
        "private analytical engines",
        "highly confidential subject",
        "private nurture subject",
        "private body",
    ):
        assert private_value not in serialized


@pytest.mark.django_db
def test_email_execution_intents_are_admin_only_and_do_not_leak_pii(
    admin_client,
    user_client,
    org_a,
):
    acknowledgement = _make_acknowledgement(org_a)
    nurture = _make_nurture(org_a)
    endpoints = (
        (ACK_URL.format(delivery_id=acknowledgement.id), acknowledgement),
        (NURTURE_URL.format(delivery_id=nurture.id), nurture),
    )

    for url, delivery in endpoints:
        assert user_client.post(url, {}, format="json").status_code == 403
        _assert_safe_intent(admin_client.post(url, {}, format="json"), delivery)


@pytest.mark.django_db
def test_email_execution_endpoints_are_tenant_scoped_and_reject_unknown_fields(
    admin_client,
    org_b_client,
    org_a,
):
    acknowledgement = _make_acknowledgement(org_a)
    nurture = _make_nurture(org_a)
    endpoints = (
        ACK_URL.format(delivery_id=acknowledgement.id),
        NURTURE_URL.format(delivery_id=nurture.id),
    )

    for url in endpoints:
        assert org_b_client.post(url, {}, format="json").status_code == 404
        rejected = admin_client.post(
            url,
            {"recipient": "attacker@example.com"},
            format="json",
        )
        assert rejected.status_code == 400
        assert "recipient" in rejected.json()

    assert not ExternalExecutionRequest.objects.filter(org=org_a).exists()
    assert not AutomationJob.objects.filter(org=org_a).exists()


@pytest.mark.django_db
def test_email_execution_rejects_wrong_delivery_type_and_terminal_state(
    admin_client,
    org_a,
):
    acknowledgement = _make_acknowledgement(org_a)
    wrong_kind = LeadDelivery.objects.create(
        org=org_a,
        intake=acknowledgement.intake,
        kind=LeadDeliveryKind.SALES_IN_APP,
        recipient="internal-profile",
    )
    nurture = _make_nurture(org_a)
    nurture.status = NurtureDeliveryStatus.SENT
    nurture.sent_at = timezone.now()
    nurture.save(update_fields=["status", "sent_at", "updated_at"])

    wrong = admin_client.post(
        ACK_URL.format(delivery_id=wrong_kind.id),
        {},
        format="json",
    )
    terminal = admin_client.post(
        NURTURE_URL.format(delivery_id=nurture.id),
        {},
        format="json",
    )

    assert wrong.status_code == 409
    assert wrong.json()["code"] == "email_delivery_kind_mismatch"
    assert terminal.status_code == 409
    assert terminal.json()["code"] == "email_execution_unavailable"


@pytest.mark.django_db
def test_email_execution_rejects_mismatched_approval_without_reserving_or_queueing(
    admin_client,
    org_a,
    admin_profile,
):
    acknowledgement = _make_acknowledgement(org_a)
    nurture = _make_nurture(org_a)
    _enable_email(org_a, admin_profile, acknowledgement.recipient)
    approval = _approval(
        org_a,
        admin_profile,
        acknowledgement_email_execution_intent(acknowledgement),
    )

    response = admin_client.post(
        NURTURE_URL.format(delivery_id=nurture.id),
        {"approval_id": str(approval.id)},
        format="json",
    )

    assert response.status_code == 409, response.json()
    assert response.json()["code"] == "approval_scope_mismatch"
    assert not ExternalExecutionRequest.objects.filter(org=org_a).exists()
    assert not AutomationJob.objects.filter(org=org_a).exists()


@pytest.mark.django_db
def test_email_execution_second_stage_queues_each_delivery_once(
    admin_client,
    org_a,
    admin_profile,
):
    acknowledgement = _make_acknowledgement(org_a)
    nurture = _make_nurture(org_a)
    _enable_email(org_a, admin_profile, acknowledgement.recipient)
    cases = (
        (
            ACK_URL.format(delivery_id=acknowledgement.id),
            acknowledgement,
            acknowledgement_email_execution_intent(acknowledgement),
        ),
        (
            NURTURE_URL.format(delivery_id=nurture.id),
            nurture,
            nurture_email_execution_intent(nurture),
        ),
    )

    for url, delivery, intent in cases:
        approval = _approval(org_a, admin_profile, intent)
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
        assert first.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        assert first.json()["execution_status"] == ExternalRequestStatus.RESERVED
        assert replay.json()["execution_status"] == ExternalRequestStatus.RESERVED
        assert replay.json()["job_id"] == first.json()["job_id"]
        assert (
            replay.json()["execution_request_id"]
            == (first.json()["execution_request_id"])
        )
        request = ExternalExecutionRequest.objects.get(
            id=first.json()["execution_request_id"],
            org=org_a,
        )
        job = AutomationJob.objects.get(id=first.json()["job_id"], org=org_a)
        assert request.channel == ExecutionChannel.EMAIL
        assert request.action == EMAIL_SEND_ACTION
        assert request.idempotency_key == delivery.id
        assert request.status == ExternalRequestStatus.RESERVED
        assert job.max_attempts == 1
        assert job.payload["execution_request_id"] == str(request.id)

    assert ExternalExecutionRequest.objects.filter(org=org_a).count() == 2
    assert AutomationJob.objects.filter(org=org_a).count() == 2


@pytest.mark.django_db
def test_email_execution_sending_and_unknown_replays_do_not_enqueue(
    admin_client,
    org_a,
    admin_profile,
    monkeypatch,
):
    acknowledgement = _make_acknowledgement(org_a)
    nurture = _make_nurture(org_a)
    _enable_email(org_a, admin_profile, acknowledgement.recipient)

    acknowledgement_intent = acknowledgement_email_execution_intent(acknowledgement)
    acknowledgement_approval = _approval(
        org_a,
        admin_profile,
        acknowledgement_intent,
    )
    sending_request = reserve_email_send(
        org=org_a,
        delivery_id=acknowledgement.id,
        approval_id=acknowledgement_approval.id,
        intent=acknowledgement_intent,
    ).request
    mark_execution_sending(org=org_a, request_id=sending_request.id)

    nurture_intent = nurture_email_execution_intent(nurture)
    nurture_approval = _approval(org_a, admin_profile, nurture_intent)
    unknown_request = reserve_email_send(
        org=org_a,
        delivery_id=nurture.id,
        approval_id=nurture_approval.id,
        intent=nurture_intent,
    ).request
    mark_execution_sending(org=org_a, request_id=unknown_request.id)
    mark_provider_accepted(
        org=org_a,
        request_id=unknown_request.id,
        local_state_uncertain=True,
    )

    acknowledgement_enqueue = Mock(
        side_effect=AssertionError("SENDING execution must not be enqueued")
    )
    nurture_enqueue = Mock(
        side_effect=AssertionError("UNKNOWN execution must not be enqueued")
    )
    monkeypatch.setattr(
        "sdr.api.views.enqueue_approved_acknowledgement_delivery",
        acknowledgement_enqueue,
    )
    monkeypatch.setattr(
        "sdr.api.views.enqueue_approved_nurture_delivery",
        nurture_enqueue,
    )

    sending = admin_client.post(
        ACK_URL.format(delivery_id=acknowledgement.id),
        {"approval_id": str(acknowledgement_approval.id)},
        format="json",
    )
    unknown = admin_client.post(
        NURTURE_URL.format(delivery_id=nurture.id),
        {"approval_id": str(nurture_approval.id)},
        format="json",
    )

    assert sending.status_code == 409, sending.json()
    assert sending.json()["execution_status"] == ExternalRequestStatus.SENDING
    assert sending.json()["replayed"] is True
    assert unknown.status_code == 409, unknown.json()
    assert unknown.json()["execution_status"] == ExternalRequestStatus.UNKNOWN
    assert unknown.json()["replayed"] is True
    acknowledgement_enqueue.assert_not_called()
    nurture_enqueue.assert_not_called()
    assert not AutomationJob.objects.filter(org=org_a).exists()


@pytest.mark.django_db
def test_secure_scheduling_stages_email_deliveries_without_unapproved_jobs(org_a):
    acknowledgement = _make_acknowledgement(org_a)
    nurture = _make_nurture(org_a)

    acknowledgement_result = schedule_acknowledgement_job(acknowledgement.intake)
    nurture_result = ensure_enrollment_schedule(nurture.enrollment)

    assert acknowledgement_result is None
    assert nurture_result.id == nurture.id
    assert LeadDelivery.objects.filter(
        org=org_a,
        id=acknowledgement.id,
        status="pending",
    ).exists()
    assert LeadNurtureDelivery.objects.filter(
        org=org_a,
        id=nurture.id,
        status=NurtureDeliveryStatus.PENDING,
    ).exists()
    assert not AutomationJob.objects.filter(org=org_a).exists()
