import pytest
from django.test import override_settings

from sdr.models import LeadIntake, SDRRoutingRule, SDRRoutingRuleMember


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_website_endpoint_keeps_contract_after_module_move(admin_client):
    payload = {
        "source_record_id": "website-42",
        "first_name": "Ada",
        "email": "ada@example.com",
        "company_name": "Acme",
    }

    created = admin_client.post("/api/sdr/intake/website/", payload, format="json")
    replayed = admin_client.post("/api/sdr/intake/website/", payload, format="json")

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["lead_id"] == created.json()["lead_id"]


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

    response = admin_client.post(
        "/api/sdr/intake/website/",
        {
            "source_record_id": "website-routed-1",
            "email": "routed@example.com",
            "country": "US",
        },
        format="json",
    )

    intake = LeadIntake.objects.get(source_record_id="website-routed-1")
    assert response.status_code == 201
    assert response.json()["assigned_profile_id"] == str(admin_profile.id)
    assert response.json()["routing_rule_id"] == str(rule.id)
    assert 'rule="US website leads"' in response.json()["routing_reason"]
    assert intake.routing_rule_id == rule.id
    assert intake.assigned_profile_id == admin_profile.id
