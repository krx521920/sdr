import pytest
from django.test import override_settings

from automation.models import AutomationJob
from automation.tasks import run_automation_job
from sdr.models import LeadIntake, SDRRoutingRule, SDRRoutingRuleMember


def run_intake_job(response, org):
    job_id = response.json()["job_id"]
    result = run_automation_job.apply(args=[job_id, str(org.id)]).get()
    assert result["status"] == "succeeded"
    return job_id


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_website_endpoint_keeps_contract_after_module_move(admin_client, org_a):
    payload = {
        "source_record_id": "website-42",
        "first_name": "Ada",
        "email": "ada@example.com",
        "company_name": "Acme",
    }

    created = admin_client.post("/api/sdr/intake/website/", payload, format="json")
    replayed = admin_client.post("/api/sdr/intake/website/", payload, format="json")

    assert created.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json()["replayed"] is True
    assert replayed.json()["intake_id"] == created.json()["intake_id"]
    assert replayed.json()["job_id"] == created.json()["job_id"]
    assert AutomationJob.objects.filter(name="sdr.process_intake").count() == 1

    run_intake_job(created, org_a)
    completed = admin_client.post("/api/sdr/intake/website/", payload, format="json")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["lead_id"] is not None


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_website_endpoint_persists_explainable_routing(
    admin_client, org_a, admin_profile
):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    rule = SDRRoutingRule.objects.create(
        org=org_a,
        name="US website leads",
        countries=["US"],
        sources=["website_form"],
    )
    SDRRoutingRuleMember.objects.create(
        org=org_a,
        rule=rule,
        profile=admin_profile,
    )

    accepted = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "website-routed-1",
            "email": "routed@example.com",
            "country": "US",
        },
        format="json",
    )

    assert accepted.status_code == 202
    run_intake_job(accepted, org_a)

    intake = LeadIntake.objects.get(source_record_id="website-routed-1")
    response = admin_client.get(f"/api/sdr/intakes/{intake.id}/")
    assert response.status_code == 200
    assert response.json()["assigned_profile_id"] == str(admin_profile.id)
    assert response.json()["routing_rule_id"] == str(rule.id)
    assert 'rule="US website leads"' in response.json()["routing_reason"]
    assert intake.routing_rule_id == rule.id
    assert intake.assigned_profile_id == admin_profile.id
