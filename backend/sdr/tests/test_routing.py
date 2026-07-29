from uuid import uuid4

import pytest
from django.test import override_settings

from common.models import Profile
from leads.models import Lead
from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.models import (
    SDRRoutingRule,
    SDRRoutingRuleMember,
    SDRRoutingRuleState,
    SDRRoutingStrategy,
)
from sdr.routing import RuleBasedSalesRouter


def candidate(org, *, country="US", source=LeadSource.WEBSITE_FORM):
    return LeadCandidate(
        org_id=org.id,
        source=source,
        source_record_id=str(uuid4()),
        identity=LeadIdentity(email="buyer@example.com"),
        company=CompanySnapshot(country=country),
    )


def qualification(band=QualificationBand.HIGH):
    return QualificationResult(score=80, band=band)


def add_member(rule, profile, position):
    return SDRRoutingRuleMember.objects.create(
        org=rule.org,
        rule=rule,
        profile=profile,
        position=position,
    )


@pytest.mark.django_db
def test_first_matching_rule_uses_least_loaded_sales_member(
    org_a, admin_profile, user_profile
):
    Profile.objects.filter(id__in=[admin_profile.id, user_profile.id]).update(
        has_sales_access=True
    )
    admin_profile.refresh_from_db()
    user_profile.refresh_from_db()
    rule = SDRRoutingRule.objects.create(
        org=org_a,
        name="United States website leads",
        priority=10,
        countries=["US"],
        sources=[LeadSource.WEBSITE_FORM.value],
        qualification_bands=[QualificationBand.HIGH.value],
    )
    add_member(rule, admin_profile, 0)
    add_member(rule, user_profile, 1)
    busy_lead = Lead.objects.create(org=org_a, title="Already assigned", is_active=True)
    busy_lead.assigned_to.add(admin_profile)

    decision = RuleBasedSalesRouter().route(candidate(org_a), qualification())

    assert decision.rule_id == rule.id
    assert decision.profile_id == user_profile.id
    assert 'rule="United States website leads"' in decision.reason


@pytest.mark.django_db
def test_non_matching_rule_falls_back_without_losing_assignment(org_a, admin_profile):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    rule = SDRRoutingRule.objects.create(
        org=org_a,
        name="United Kingdom only",
        countries=["GB"],
    )
    add_member(rule, admin_profile, 0)

    decision = RuleBasedSalesRouter().route(
        candidate(org_a, country="US"), qualification()
    )

    assert decision.rule_id is None
    assert decision.profile_id == admin_profile.id
    assert decision.reason.startswith("fallback:")


@pytest.mark.django_db(transaction=True)
def test_round_robin_preview_does_not_advance_cursor(
    org_a, admin_profile, user_profile
):
    Profile.objects.filter(id__in=[admin_profile.id, user_profile.id]).update(
        has_sales_access=True
    )
    admin_profile.refresh_from_db()
    user_profile.refresh_from_db()
    rule = SDRRoutingRule.objects.create(
        org=org_a,
        name="Rotate evenly",
        strategy=SDRRoutingStrategy.ROUND_ROBIN,
    )
    add_member(rule, admin_profile, 0)
    add_member(rule, user_profile, 1)
    router = RuleBasedSalesRouter()

    preview = router.preview(candidate(org_a), qualification())
    first = router.route(candidate(org_a), qualification())
    second = router.route(candidate(org_a), qualification())

    assert preview.profile_id == admin_profile.id
    assert first.profile_id == admin_profile.id
    assert second.profile_id == user_profile.id
    assert SDRRoutingRuleState.objects.get(rule=rule).next_index == 2


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls")
def test_admin_api_is_tenant_scoped_and_previews_rules(
    admin_client,
    user_client,
    org_b_client,
    org_a,
    admin_profile,
    profile_b,
):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    profile_b.has_sales_access = True
    profile_b.save(update_fields=["has_sales_access"])
    payload = {
        "name": "North America",
        "priority": 20,
        "strategy": "direct",
        "countries": ["United States"],
        "sources": ["website_form", "facebook_ad"],
        "qualification_bands": ["high", "medium"],
        "profile_ids": [str(admin_profile.id)],
    }

    denied = user_client.get("/api/sdr/routing-rules/")
    created = admin_client.post("/api/sdr/routing-rules/", payload, format="json")
    other_tenant = org_b_client.get("/api/sdr/routing-rules/")
    invalid_member = admin_client.post(
        "/api/sdr/routing-rules/",
        {**payload, "name": "Cross tenant", "profile_ids": [str(profile_b.id)]},
        format="json",
    )
    preview = admin_client.post(
        "/api/sdr/routing-rules/preview/",
        {
            "country": "US",
            "source": "website_form",
            "qualification_band": "high",
        },
        format="json",
    )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["countries"] == ["US"]
    assert created.json()["members"][0]["profile_id"] == str(admin_profile.id)
    assert other_tenant.status_code == 200
    assert other_tenant.json()["rules"] == []
    assert invalid_member.status_code == 400
    assert preview.status_code == 200
    assert preview.json()["matched"] is True
    assert preview.json()["routing_rule_id"] == created.json()["id"]
    assert preview.json()["assigned_profile_id"] == str(admin_profile.id)


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls")
def test_rule_update_and_delete_cannot_cross_tenant(
    admin_client, org_b_client, org_a, admin_profile
):
    admin_profile.has_sales_access = True
    admin_profile.save(update_fields=["has_sales_access"])
    rule = SDRRoutingRule.objects.create(org=org_a, name="Original")
    add_member(rule, admin_profile, 0)

    hidden = org_b_client.patch(
        f"/api/sdr/routing-rules/{rule.id}/",
        {"name": "Hijacked"},
        format="json",
    )
    updated = admin_client.patch(
        f"/api/sdr/routing-rules/{rule.id}/",
        {"name": "Updated"},
        format="json",
    )
    deleted = admin_client.delete(f"/api/sdr/routing-rules/{rule.id}/")

    assert hidden.status_code == 404
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated"
    assert deleted.status_code == 204
    assert not SDRRoutingRule.objects.filter(id=rule.id).exists()
