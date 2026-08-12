from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from leads.models import Lead
from sdr.models import (
    LeadDelivery,
    LeadDeliveryKind,
    LeadIntake,
    LeadNurtureEnrollment,
    NurtureEnrollmentStatus,
    OutboundProspectStatus,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRResponseSettings,
)
from sdr.outbound import reconcile_outbound_campaigns


def create_campaign(client, name="Industrial automation EU"):
    response = client.post(
        "/api/sdr/outbound/campaigns/",
        {
            "name": name,
            "description": "Factory automation decision makers.",
            "icp_description": "European manufacturers with 100+ employees.",
            "channels": ["email", "linkedin"],
            "status": "draft",
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    return SDROutboundCampaign.objects.get(id=response.json()["id"])


def create_outbound_sequence(org, name="Campaign email sequence"):
    sequence = SDRNurtureSequence.objects.create(
        org=org,
        name=name,
        is_active=True,
        auto_enroll=False,
        sources=["outbound"],
        from_email="sales@example.com",
    )
    SDRNurtureStep.objects.create(
        org=org,
        sequence=sequence,
        position=1,
        delay_minutes=1440,
        subject_a="A focused introduction",
        body_a="Hello {{ first_name }}, would this be relevant?",
    )
    return sequence


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_outbound_campaign_api_is_admin_only_and_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
):
    campaign = create_campaign(admin_client)
    listed = admin_client.get("/api/sdr/outbound/campaigns/")
    assert listed.status_code == 200
    assert listed.json()["summary"] == {
        "campaigns": 1,
        "active_campaigns": 0,
        "prospects": 0,
        "ready": 0,
        "promoted": 0,
        "failed": 0,
    }
    duplicate = admin_client.post(
        "/api/sdr/outbound/campaigns/",
        {"name": "industrial AUTOMATION eu", "channels": []},
        format="json",
    )
    assert duplicate.status_code == 400

    activated = admin_client.patch(
        f"/api/sdr/outbound/campaigns/{campaign.id}/",
        {"status": "active"},
        format="json",
    )
    assert activated.status_code == 400
    launch = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launch.status_code == 409
    assert user_client.get("/api/sdr/outbound/campaigns/").status_code == 403
    assert (
        org_b_client.get(f"/api/sdr/outbound/campaigns/{campaign.id}/").status_code
        == 404
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_csv_import_cleans_and_deduplicates_against_list_and_crm(
    admin_client,
    org_b_client,
    org_a,
    org_b,
):
    campaign = create_campaign(admin_client)
    Lead.objects.create(
        org=org_a,
        first_name="Existing",
        last_name="Lead",
        email="existing@example.com",
        status="assigned",
    )
    csv_text = """company_name,email,first_name,last_name,website,country,source_url
Analytical Engines, ADA@EXAMPLE.COM, Ada , Lovelace,analytical.example,United States,linkedin.com/in/ada
Analytical Engines,ada@example.com,Ada,Lovelace,analytical.example,US,linkedin.com/in/ada
Existing Corp,existing@example.com,Existing,Lead,existing.example,US,
Broken Corp,not-an-email,Broken,Email,broken.example,GB,
"""
    imported = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {"csv_text": csv_text, "promote_ready": False},
        format="json",
    )
    assert imported.status_code == 201, imported.json()
    assert imported.json()["created"] == 1
    assert imported.json()["duplicate_count"] == 2
    assert imported.json()["error_count"] == 1
    prospect = SDROutboundProspect.objects.get(campaign=campaign)
    assert prospect.email == "ada@example.com"
    assert prospect.first_name == "Ada"
    assert prospect.website == "https://analytical.example"
    assert prospect.country == "US"

    replay = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {"csv_text": csv_text.splitlines()[0] + "\n" + csv_text.splitlines()[1]},
        format="json",
    )
    assert replay.json()["created"] == 0
    assert replay.json()["duplicate_count"] == 1

    other_campaign = SDROutboundCampaign.objects.create(
        org=org_b,
        name="Other tenant campaign",
    )
    other = org_b_client.post(
        f"/api/sdr/outbound/campaigns/{other_campaign.id}/prospects/import/",
        {"csv_text": csv_text.splitlines()[0] + "\n" + csv_text.splitlines()[1]},
        format="json",
    )
    assert other.status_code == 201
    assert other.json()["created"] == 1

    bad_header = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {"csv_text": "company_name,mystery\nAcme,value"},
        format="json",
    )
    assert bad_header.status_code == 400


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_promoted_prospect_uses_durable_pipeline_without_inbound_ack(
    admin_client,
    org_a,
):
    campaign = create_campaign(admin_client, "Durable outbound")
    SDRResponseSettings.objects.create(
        org=org_a,
        acknowledgement_email_enabled=True,
        acknowledgement_from_email="sales@example.com",
        sales_in_app_enabled=False,
    )
    generic = SDRNurtureSequence.objects.create(
        org=org_a,
        name="Generic inbound nurture",
        priority=1,
        is_active=True,
        auto_enroll=True,
        sources=[],
        from_email="sales@example.com",
    )
    SDRNurtureStep.objects.create(
        org=org_a,
        sequence=generic,
        position=1,
        delay_minutes=1440,
        subject_a="Generic",
        body_a="Generic",
    )
    explicit = SDRNurtureSequence.objects.create(
        org=org_a,
        name="Explicit outbound",
        priority=2,
        is_active=True,
        auto_enroll=True,
        sources=["outbound"],
        from_email="sales@example.com",
    )
    SDRNurtureStep.objects.create(
        org=org_a,
        sequence=explicit,
        position=1,
        delay_minutes=1440,
        subject_a="Outbound",
        body_a="Outbound",
    )
    campaign.sequence = explicit
    campaign.save(update_fields=["sequence", "updated_at"])

    imported = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {
            "csv_text": (
                "company_name,email,first_name,last_name,job_title,website,country\n"
                "Babbage Works,charles@babbage.example,Charles,Babbage,CTO,"
                "babbage.example,GB"
            ),
            "promote_ready": False,
        },
        format="json",
    )
    assert imported.status_code == 201, imported.json()
    assert imported.json()["queued"] == 0
    launched = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launched.status_code == 200, launched.json()
    assert launched.json()["execution"]["queued"] == 1
    prospect = SDROutboundProspect.objects.get(campaign=campaign)
    assert prospect.status == OutboundProspectStatus.QUEUED
    job = AutomationJob.objects.get(
        name="sdr.process_outbound_prospect",
        payload__prospect_id=str(prospect.id),
    )

    result = run_automation_job.run(str(job.id), str(org_a.id))
    assert result["status"] == "succeeded"
    prospect.refresh_from_db()
    assert prospect.status == OutboundProspectStatus.PROMOTED
    intake = LeadIntake.objects.get(id=prospect.intake_id)
    assert intake.source == "outbound"
    assert intake.crm_lead.custom_fields["sdr"]["source"] == "outbound"
    assert not LeadDelivery.objects.filter(
        intake=intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
    ).exists()
    enrollment = LeadNurtureEnrollment.objects.get(intake=intake)
    assert enrollment.sequence == explicit

    replay = run_automation_job.run(str(job.id), str(org_a.id))
    assert replay["status"] == "skipped"
    assert LeadIntake.objects.filter(source="outbound").count() == 1


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_outbound_prospect_can_be_disqualified_and_restored(
    admin_client,
    user_client,
):
    campaign = create_campaign(admin_client, "Prospect state")
    imported = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {
            "csv_text": (
                "company_name,email\nState Machines,state@example.com"
            )
        },
        format="json",
    )
    prospect_id = imported.json()["prospect_ids"][0]
    url = f"/api/sdr/outbound/prospects/{prospect_id}/action/"
    disqualified = admin_client.post(
        url,
        {"action": "disqualify"},
        format="json",
    )
    assert disqualified.status_code == 200
    assert disqualified.json()["status"] == OutboundProspectStatus.DISQUALIFIED
    assert admin_client.post(url, {"action": "promote"}, format="json").status_code == 409
    restored = admin_client.post(url, {"action": "restore"}, format="json")
    assert restored.status_code == 200
    assert restored.json()["status"] == OutboundProspectStatus.READY
    assert user_client.post(url, {"action": "promote"}, format="json").status_code == 403


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_campaign_launch_enforces_sequence_tenant_and_execution_safety(
    admin_client,
    org_a,
    org_b,
):
    other_sequence = create_outbound_sequence(org_b, "Other tenant sequence")
    rejected = admin_client.post(
        "/api/sdr/outbound/campaigns/",
        {
            "name": "Cross tenant sequence",
            "channels": ["email"],
            "sequence_id": str(other_sequence.id),
        },
        format="json",
    )
    assert rejected.status_code == 400

    sequence = create_outbound_sequence(org_a, "Disabled outbound sequence")
    sequence.is_active = False
    sequence.save(update_fields=["is_active", "updated_at"])
    campaign = create_campaign(admin_client, "Guarded campaign")
    configured = admin_client.patch(
        f"/api/sdr/outbound/campaigns/{campaign.id}/",
        {"sequence_id": str(sequence.id), "daily_send_limit": 25},
        format="json",
    )
    assert configured.status_code == 200
    launch = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launch.status_code == 409
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert campaign.run_count == 0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_campaign_daily_cap_and_periodic_refill(
    admin_client,
    org_a,
):
    sequence = create_outbound_sequence(org_a, "Daily capped sequence")
    campaign = create_campaign(admin_client, "Daily capped campaign")
    configured = admin_client.patch(
        f"/api/sdr/outbound/campaigns/{campaign.id}/",
        {"sequence_id": str(sequence.id), "daily_send_limit": 2},
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    imported = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {
            "csv_text": (
                "company_name,email\n"
                "Cap One,one@cap.example\n"
                "Cap Two,two@cap.example\n"
                "Cap Three,three@cap.example"
            )
        },
        format="json",
    )
    assert imported.status_code == 201
    launched = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launched.status_code == 200, launched.json()
    assert launched.json()["execution"]["queued"] == 2
    assert launched.json()["execution"]["remaining_today"] == 0
    blocked_edit = admin_client.patch(
        f"/api/sdr/outbound/campaigns/{campaign.id}/",
        {"daily_send_limit": 3},
        format="json",
    )
    assert blocked_edit.status_code == 400
    assert reconcile_outbound_campaigns(org_id=org_a.id) == 0

    failed = SDROutboundProspect.objects.filter(
        campaign=campaign,
        status=OutboundProspectStatus.QUEUED,
    ).first()
    failed.status = OutboundProspectStatus.FAILED
    failed.save(update_fields=["status", "updated_at"])
    retried = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "retry_failed"},
        format="json",
    )
    assert retried.status_code == 200
    assert retried.json()["execution"]["queued"] == 1

    queued = SDROutboundProspect.objects.filter(
        campaign=campaign,
        status=OutboundProspectStatus.QUEUED,
    )
    queued.update(queued_at=timezone.now() - timedelta(days=1))
    assert reconcile_outbound_campaigns(org_id=org_a.id) == 1
    assert SDROutboundProspect.objects.filter(
        campaign=campaign,
        status=OutboundProspectStatus.QUEUED,
    ).count() == 3


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_pause_invalidates_stale_jobs_and_resume_restores_enrollment(
    admin_client,
    org_a,
):
    sequence = create_outbound_sequence(org_a, "Pause-safe sequence")
    campaign = create_campaign(admin_client, "Pause-safe campaign")
    admin_client.patch(
        f"/api/sdr/outbound/campaigns/{campaign.id}/",
        {"sequence_id": str(sequence.id), "daily_send_limit": 1},
        format="json",
    )
    admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/prospects/import/",
        {"csv_text": "company_name,email\nSafe Corp,safe@example.com"},
        format="json",
    )
    launched = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert launched.status_code == 200
    prospect = SDROutboundProspect.objects.get(campaign=campaign)
    stale_job = AutomationJob.objects.get(
        name="sdr.process_outbound_prospect",
        payload__prospect_id=str(prospect.id),
        payload__campaign_run=1,
    )

    paused = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "pause"},
        format="json",
    )
    assert paused.status_code == 200
    run_automation_job.run(str(stale_job.id), str(org_a.id))
    prospect.refresh_from_db()
    assert prospect.status == OutboundProspectStatus.READY
    assert prospect.intake_id is None

    resumed = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert resumed.status_code == 200, resumed.json()
    assert resumed.json()["campaign"]["run_count"] == 2
    assert resumed.json()["execution"]["queued"] == 1
    current_job = AutomationJob.objects.get(
        name="sdr.process_outbound_prospect",
        payload__prospect_id=str(prospect.id),
        payload__campaign_run=2,
    )
    run_automation_job.run(str(current_job.id), str(org_a.id))
    prospect.refresh_from_db()
    assert prospect.status == OutboundProspectStatus.PROMOTED
    enrollment = LeadNurtureEnrollment.objects.get(intake=prospect.intake)
    assert enrollment.sequence == sequence
    assert enrollment.status == NurtureEnrollmentStatus.ACTIVE

    admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "pause"},
        format="json",
    )
    enrollment.refresh_from_db()
    assert enrollment.status == NurtureEnrollmentStatus.PAUSED
    resumed = admin_client.post(
        f"/api/sdr/outbound/campaigns/{campaign.id}/action/",
        {"action": "launch"},
        format="json",
    )
    assert resumed.status_code == 200
    assert resumed.json()["execution"]["resumed"] == 1
    enrollment.refresh_from_db()
    assert enrollment.status == NurtureEnrollmentStatus.ACTIVE
