from unittest.mock import Mock

import pytest
from django.core import mail
from django.test import override_settings

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from common.models import Notification
from sdr.compliance import request_intake_deletion
from sdr.models import (
    LeadDelivery,
    LeadDeliveryStatus,
    LeadIntake,
    LeadLifecycleEvent,
    SDRResponseSettings,
)
from sdr.response import schedule_post_handoff_jobs


def run_job(job, org):
    return run_automation_job.apply(args=[str(job.id), str(org.id)]).get()


def accept_and_process(client, org, *, source_record_id="response-loop-1"):
    response = client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": source_record_id,
            "first_name": "Ada",
            "email": "ada@example.com",
            "company_name": "Analytical Engines Ltd",
            "message": "We need help qualifying and routing inbound leads.",
        },
        format="json",
    )
    assert response.status_code == 202
    job = AutomationJob.objects.get(id=response.json()["job_id"])
    assert run_job(job, org)["status"] == "succeeded"
    return LeadIntake.objects.get(id=response.json()["intake_id"])


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_acknowledgement_is_queued_before_slow_intake_processing(
    admin_client,
    org_a,
):
    SDRResponseSettings.objects.create(
        org=org_a,
        acknowledgement_email_enabled=True,
    )
    response = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "response-loop-immediate-ack",
            "first_name": "Grace",
            "email": "grace@example.com",
        },
        format="json",
    )

    assert response.status_code == 202
    intake = LeadIntake.objects.get(id=response.json()["intake_id"])
    assert intake.status == "received"
    assert AutomationJob.objects.filter(
        org=org_a,
        name="sdr.send_acknowledgement",
        status="queued",
    ).exists()


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_completed_intake_sends_idempotent_ack_and_in_app_handoff(
    admin_client,
    org_a,
    admin_profile,
):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    SDRResponseSettings.objects.create(
        org=org_a,
        acknowledgement_email_enabled=True,
        acknowledgement_subject="Thanks {{ first_name }}",
        acknowledgement_body="Hi {{ first_name }}, {{ organization_name }} received it.",
        sales_in_app_enabled=True,
        response_sla_seconds=60,
    )

    intake = accept_and_process(admin_client, org_a)
    ack_job = AutomationJob.objects.get(name="sdr.send_acknowledgement")
    in_app_job = AutomationJob.objects.get(name="sdr.notify_sales_in_app")

    assert run_job(ack_job, org_a)["status"] == "succeeded"
    assert run_job(in_app_job, org_a)["status"] == "succeeded"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ada@example.com"]
    assert mail.outbox[0].subject == "Thanks Ada"
    assert (
        Notification.objects.filter(
            org=org_a,
            recipient=admin_profile,
            verb="sdr_lead_assigned",
        ).count()
        == 1
    )
    assert set(
        LeadDelivery.objects.filter(intake=intake).values_list("status", flat=True)
    ) == {LeadDeliveryStatus.SENT}
    assert LeadLifecycleEvent.objects.filter(
        intake=intake,
        event_key="delivery:acknowledgement_email",
    ).exists()
    assert LeadLifecycleEvent.objects.filter(
        intake=intake,
        event_key="delivery:sales_in_app",
    ).exists()

    assert run_job(ack_job, org_a)["status"] == "skipped"
    assert len(mail.outbox) == 1

    detail = admin_client.get(f"/api/sdr/intakes/{intake.id}/")
    assert detail.status_code == 200
    assert detail.json()["response_seconds"] is not None
    assert detail.json()["sla_breached"] is False


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF="integrations.tests.urls",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_deletion_request_stops_queued_intake_and_response_jobs(
    admin_client,
    org_a,
):
    SDRResponseSettings.objects.create(
        org=org_a,
        acknowledgement_email_enabled=True,
        sales_in_app_enabled=False,
    )
    response = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "response-loop-deletion-request",
            "first_name": "Grace",
            "email": "grace@example.com",
            "company_name": "Deletion Safe Ltd",
        },
        format="json",
    )
    assert response.status_code == 202
    intake = LeadIntake.objects.get(id=response.json()["intake_id"])
    intake_job = AutomationJob.objects.get(id=response.json()["job_id"])
    acknowledgement_job = AutomationJob.objects.get(
        org=org_a,
        name="sdr.send_acknowledgement",
    )

    deletion = admin_client.post(
        f"/api/sdr/compliance/intakes/{intake.id}/deletion/",
        {"action": "request"},
        format="json",
    )

    assert deletion.status_code == 200, deletion.json()
    assert deletion.json()["status"] == "deletion_requested"
    assert run_job(intake_job, org_a)["status"] == "succeeded"
    assert run_job(acknowledgement_job, org_a)["status"] == "succeeded"
    intake.refresh_from_db()
    delivery = LeadDelivery.objects.get(
        intake=intake,
        kind="acknowledgement_email",
    )
    assert intake.status == "received"
    assert intake.crm_lead_id is None
    assert delivery.status == LeadDeliveryStatus.SKIPPED
    assert delivery.last_error_code == "data_deletion_requested"
    assert mail.outbox == []


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_deletion_request_stops_response_reconciliation(admin_client, org_a):
    configuration = SDRResponseSettings.objects.create(
        org=org_a,
        acknowledgement_email_enabled=False,
        sales_in_app_enabled=False,
    )
    intake = accept_and_process(
        admin_client,
        org_a,
        source_record_id="response-loop-deletion-reconcile",
    )
    configuration.acknowledgement_email_enabled = True
    configuration.sales_in_app_enabled = True
    configuration.save(
        update_fields=[
            "acknowledgement_email_enabled",
            "sales_in_app_enabled",
            "updated_at",
        ]
    )

    request_intake_deletion(intake)

    assert schedule_post_handoff_jobs(intake) == []
    assert not LeadDelivery.objects.filter(intake=intake).exists()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_feishu_handoff_uses_encrypted_official_webhook(
    admin_client,
    org_a,
    admin_profile,
    monkeypatch,
):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    configuration = SDRResponseSettings.objects.create(
        org=org_a,
        sales_in_app_enabled=False,
        feishu_enabled=True,
    )
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-12345678"
    configuration.set_feishu_webhook(webhook)
    configuration.save()
    response = Mock(status_code=200)
    response.json.return_value = {"code": 0, "msg": "success"}
    post = Mock(return_value=response)
    monkeypatch.setattr("sdr.response.requests.post", post)

    intake = accept_and_process(
        admin_client,
        org_a,
        source_record_id="response-loop-feishu",
    )
    feishu_job = AutomationJob.objects.get(name="sdr.notify_sales_feishu")
    assert run_job(feishu_job, org_a)["status"] == "succeeded"
    delivery = LeadDelivery.objects.get(intake=intake, kind="sales_feishu")
    assert delivery.status == LeadDeliveryStatus.SENT
    assert configuration.feishu_webhook_ciphertext != webhook
    request = post.call_args
    assert request.args[0] == webhook
    assert request.kwargs["json"]["msg_type"] == "text"
    assert "Analytical Engines Ltd" in request.kwargs["json"]["content"]["text"]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_response_settings_reject_non_feishu_webhook(admin_client):
    invalid = admin_client.put(
        "/api/sdr/response-settings/",
        {
            "acknowledgement_email_enabled": False,
            "acknowledgement_subject": "Thanks",
            "acknowledgement_body": "Received",
            "acknowledgement_from_email": "",
            "sales_in_app_enabled": True,
            "feishu_enabled": True,
            "feishu_webhook_url": "https://attacker.example/webhook",
            "response_sla_seconds": 60,
        },
        format="json",
    )
    assert invalid.status_code == 400

    invalid_template = admin_client.put(
        "/api/sdr/response-settings/",
        {
            "acknowledgement_email_enabled": True,
            "acknowledgement_subject": "{% include 'secret.html' %}",
            "acknowledgement_body": "Received",
            "acknowledgement_from_email": "",
            "sales_in_app_enabled": True,
            "feishu_enabled": False,
            "feishu_webhook_url": "",
            "response_sla_seconds": 60,
        },
        format="json",
    )
    assert invalid_template.status_code == 400

    valid = admin_client.put(
        "/api/sdr/response-settings/",
        {
            "acknowledgement_email_enabled": False,
            "acknowledgement_subject": "Thanks",
            "acknowledgement_body": "Received",
            "acknowledgement_from_email": "",
            "sales_in_app_enabled": True,
            "feishu_enabled": True,
            "feishu_webhook_url": (
                "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-12345678"
            ),
            "response_sla_seconds": 60,
        },
        format="json",
    )
    assert valid.status_code == 200
    assert valid.json()["feishu_configured"] is True
    assert "feishu_webhook_url" not in valid.json()
