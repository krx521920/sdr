from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from leads.models import Lead
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
    NurtureReplySentiment,
    SDRNurtureSequence,
    SDRResponseSettings,
)


def create_intake(
    *,
    org,
    record_id,
    source,
    status=LeadIntakeStatus.COMPLETED,
    band="high",
    assigned_profile=None,
    crm_lead=None,
    age_days=1,
):
    intake = LeadIntake.objects.create(
        org=org,
        source=source,
        source_record_id=record_id,
        status=status,
        qualification_score=85 if band in {"high", "medium"} else 25,
        qualification_band=band,
        assigned_profile=assigned_profile,
        crm_lead=crm_lead,
        crm_created=crm_lead is not None,
    )
    created_at = timezone.now() - timedelta(days=age_days)
    LeadIntake.objects.filter(id=intake.id).update(created_at=created_at)
    intake.refresh_from_db()
    return intake


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_funnel_analytics_are_progressive_and_tenant_scoped(
    admin_client,
    user_client,
    org_b_client,
    org_a,
    org_b,
    admin_profile,
):
    converted = Lead.objects.create(
        org=org_a,
        first_name="Ada",
        last_name="Lovelace",
        email="converted-analytics@example.com",
        status="converted",
    )
    working = Lead.objects.create(
        org=org_a,
        first_name="Grace",
        last_name="Hopper",
        email="working-analytics@example.com",
        status="in process",
    )
    sql_intake = create_intake(
        org=org_a,
        record_id="analytics-sql",
        source=LeadIntakeSource.WEBSITE_FORM,
        assigned_profile=admin_profile,
        crm_lead=converted,
    )
    create_intake(
        org=org_a,
        record_id="analytics-handoff",
        source=LeadIntakeSource.FACEBOOK_AD,
        band="medium",
        assigned_profile=admin_profile,
        crm_lead=working,
    )
    create_intake(
        org=org_a,
        record_id="analytics-mql",
        source=LeadIntakeSource.EMAIL,
        band="high",
    )
    create_intake(
        org=org_a,
        record_id="analytics-low",
        source=LeadIntakeSource.WEBSITE_FORM,
        band="low",
    )
    create_intake(
        org=org_a,
        record_id="analytics-failed",
        source=LeadIntakeSource.LINKEDIN,
        status=LeadIntakeStatus.FAILED,
        band="",
    )
    create_intake(
        org=org_a,
        record_id="analytics-previous",
        source=LeadIntakeSource.API,
        age_days=35,
    )
    create_intake(
        org=org_b,
        record_id="other-tenant",
        source=LeadIntakeSource.MANUAL,
    )

    SDRResponseSettings.objects.create(org=org_a, response_sla_seconds=60)
    LeadDelivery.objects.create(
        org=org_a,
        intake=sql_intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
        recipient="converted-analytics@example.com",
        status=LeadDeliveryStatus.SENT,
        sent_at=sql_intake.created_at + timedelta(seconds=30),
    )

    response = admin_client.get("/api/sdr/analytics/funnel/?days=30")
    assert response.status_code == 200
    payload = response.json()
    assert [stage["count"] for stage in payload["funnel"]] == [5, 4, 3, 2, 1]
    assert payload["kpis"]["mql"]["value"] == 3
    assert payload["kpis"]["mql"]["previous"] == 1
    assert payload["kpis"]["mql_to_sql_rate"]["value"] == 33.3
    assert sum(day["received"] for day in payload["trend"]) == 5
    assert len(payload["trend"]) == 30

    website = next(
        row for row in payload["sources"] if row["source"] == "website_form"
    )
    assert website == {
        "source": "website_form",
        "label": "Website Form",
        "received": 2,
        "mql": 1,
        "sales_handoff": 1,
        "sql": 1,
        "mql_rate": 50.0,
        "sql_rate": 50.0,
    }
    assert payload["response_sla"] == {
        "sla_seconds": 60,
        "sample_size": 1,
        "average_seconds": 30,
        "median_seconds": 30,
        "within_sla": 1,
        "breached": 0,
        "within_sla_rate": 100.0,
    }

    assert user_client.get("/api/sdr/analytics/funnel/").status_code == 403
    assert org_b_client.get("/api/sdr/analytics/funnel/").json()["kpis"][
        "received"
    ]["value"] == 1
    assert admin_client.get("/api/sdr/analytics/funnel/?days=14").status_code == 400


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_funnel_analytics_report_nurture_ab_outcomes(
    admin_client,
    org_a,
):
    intake = create_intake(
        org=org_a,
        record_id="analytics-engagement",
        source=LeadIntakeSource.WEBSITE_FORM,
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org_a,
        name="Analytics sequence",
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org_a,
        sequence=sequence,
        intake=intake,
    )
    now = timezone.now()
    LeadNurtureDelivery.objects.create(
        org=org_a,
        enrollment=enrollment,
        step_position=1,
        variant="A",
        recipient="a@example.com",
        subject_template="A",
        body_template="A",
        status=NurtureDeliveryStatus.SENT,
        scheduled_for=now,
        sent_at=now,
        delivered_at=now,
        opened_at=now,
        replied_at=now,
        reply_sentiment=NurtureReplySentiment.POSITIVE,
    )
    LeadNurtureDelivery.objects.create(
        org=org_a,
        enrollment=enrollment,
        step_position=2,
        variant="B",
        recipient="b@example.com",
        subject_template="B",
        body_template="B",
        status=NurtureDeliveryStatus.SENT,
        scheduled_for=now,
        sent_at=now,
        delivered_at=now,
    )

    response = admin_client.get("/api/sdr/analytics/funnel/?days=7")
    assert response.status_code == 200
    engagement = response.json()["engagement"]
    assert engagement["sent"] == 2
    assert engagement["delivery_rate"] == 100.0
    assert engagement["open_rate"] == 50.0
    assert engagement["reply_rate"] == 50.0
    assert engagement["positive_reply_rate"] == 50.0
    assert engagement["variants"] == [
        {
            "variant": "A",
            "sent": 1,
            "opened": 1,
            "replied": 1,
            "positive_replies": 1,
            "open_rate": 100.0,
            "reply_rate": 100.0,
            "positive_reply_rate": 100.0,
        },
        {
            "variant": "B",
            "sent": 1,
            "opened": 0,
            "replied": 0,
            "positive_replies": 0,
            "open_rate": 0.0,
            "reply_rate": 0.0,
            "positive_reply_rate": 0.0,
        },
    ]
    assert any(
        insight["title"] == "A/B sample is still small"
        for insight in response.json()["insights"]
    )
