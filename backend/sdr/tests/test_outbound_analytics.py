from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from integrations.models import (
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)
from leads.models import Lead
from sdr.models import (
    LeadIntake,
    LeadIntakeSource,
    LeadIntakeStatus,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureReplySentiment,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCampaign,
    SDROutboundProspect,
)


def create_prospect_journey(
    *,
    org,
    campaign,
    sequence,
    record_id,
    dedupe_key,
    email,
    industry,
    country,
    band,
    lead=None,
    assigned_profile=None,
):
    intake = LeadIntake.objects.create(
        org=org,
        source=LeadIntakeSource.OUTBOUND,
        source_record_id=record_id,
        status=LeadIntakeStatus.COMPLETED,
        qualification_score=90 if band == "high" else 25,
        qualification_band=band,
        crm_lead=lead,
        assigned_profile=assigned_profile,
        crm_created=lead is not None,
    )
    prospect = SDROutboundProspect.objects.create(
        org=org,
        campaign=campaign,
        company_name=f"{industry} account",
        email=email,
        industry=industry,
        country=country,
        dedupe_key=dedupe_key,
        status="promoted",
        intake=intake,
        promoted_at=timezone.now(),
    )
    enrollment = LeadNurtureEnrollment.objects.create(
        org=org,
        sequence=sequence,
        intake=intake,
        lead=lead,
    )
    return prospect, enrollment


def create_delivery(
    *,
    org,
    enrollment,
    position,
    variant,
    opened=False,
    clicked=False,
    replied=False,
    bounced=False,
):
    now = timezone.now()
    return LeadNurtureDelivery.objects.create(
        org=org,
        enrollment=enrollment,
        step_position=position,
        variant=variant,
        recipient=f"step-{position}-{variant.lower()}@example.com",
        subject_template=f"Subject {variant}",
        body_template=f"Body {variant}",
        status=NurtureDeliveryStatus.SENT,
        scheduled_for=now - timedelta(minutes=1),
        sent_at=now,
        delivered_at=None if bounced else now,
        opened_at=now if opened else None,
        clicked_at=now if clicked else None,
        replied_at=now if replied else None,
        reply_sentiment=(NurtureReplySentiment.POSITIVE if replied else ""),
        bounced_at=now if bounced else None,
    )


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_campaign_analytics_report_cohort_icp_steps_and_variants(
    admin_client,
    user_client,
    org_b_client,
    org_a,
    admin_profile,
):
    converted = Lead.objects.create(
        org=org_a,
        first_name="Ada",
        email="ada-outbound-analytics@example.com",
        status="converted",
    )
    working = Lead.objects.create(
        org=org_a,
        first_name="Grace",
        email="grace-outbound-analytics@example.com",
        status="in process",
    )
    sequence = SDRNurtureSequence.objects.create(
        org=org_a,
        name="Campaign analytics sequence",
        sources=[LeadIntakeSource.OUTBOUND],
    )
    SDRNurtureStep.objects.create(
        org=org_a,
        sequence=sequence,
        position=1,
        subject_a="Workflow idea",
        body_a="Body A",
        subject_b="Manual handoffs",
        body_b="Body B",
        variant_b_percent=50,
    )
    SDRNurtureStep.objects.create(
        org=org_a,
        sequence=sequence,
        position=2,
        delay_minutes=1440,
        subject_a="Following up",
        body_a="Follow-up A",
    )
    campaign = SDROutboundCampaign.objects.create(
        org=org_a,
        name="Manufacturing ICP",
        icp_description="Operations leaders in manufacturing and SaaS",
        channels=["email", "whatsapp"],
        sequence=sequence,
    )
    prospect_a, enrollment_a = create_prospect_journey(
        org=org_a,
        campaign=campaign,
        sequence=sequence,
        record_id="outbound-analytics-a",
        dedupe_key="outbound-analytics-a",
        email=converted.email,
        industry="Manufacturing",
        country="US",
        band="high",
        lead=converted,
        assigned_profile=admin_profile,
    )
    _prospect_b, enrollment_b = create_prospect_journey(
        org=org_a,
        campaign=campaign,
        sequence=sequence,
        record_id="outbound-analytics-b",
        dedupe_key="outbound-analytics-b",
        email=working.email,
        industry="SaaS",
        country="GB",
        band="low",
        lead=working,
    )
    create_delivery(
        org=org_a,
        enrollment=enrollment_a,
        position=1,
        variant="A",
        opened=True,
        clicked=True,
        replied=True,
    )
    create_delivery(
        org=org_a,
        enrollment=enrollment_a,
        position=2,
        variant="B",
    )
    create_delivery(
        org=org_a,
        enrollment=enrollment_b,
        position=1,
        variant="B",
        bounced=True,
    )
    route = WhatsAppPhoneRoute.objects.create(
        org=org_a,
        phone_number_id="outbound-analytics-phone",
    )
    connection = WhatsAppBusinessConnection.objects.create(
        org=org_a,
        route=route,
        display_phone_number="+15550001111",
        access_token_ciphertext="encrypted-placeholder",
        is_active=True,
    )
    now = timezone.now()
    WhatsAppMessage.objects.create(
        org=org_a,
        connection=connection,
        campaign=campaign,
        prospect=prospect_a,
        campaign_run=1,
        recipient="15550001111",
        template_name="analytics_template",
        status=WhatsAppMessageStatus.READ,
        sent_at=now,
        delivered_at=now,
        read_at=now,
    )

    response = admin_client.get(
        f"/api/sdr/outbound/campaigns/{campaign.id}/analytics/"
    )
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["cohort"] == {
        "prospects": 2,
        "promoted": 2,
        "enrolled": 2,
        "mql": 1,
        "sales_handoff": 1,
        "sql": 1,
        "promotion_rate": 100.0,
        "mql_rate": 50.0,
        "sql_rate": 50.0,
        "mql_to_sql_rate": 100.0,
    }
    assert payload["email"]["sent"] == 3
    assert payload["email"]["delivered"] == 2
    assert payload["email"]["opened"] == 1
    assert payload["email"]["clicked"] == 1
    assert payload["email"]["replied"] == 1
    assert payload["email"]["positive_replies"] == 1
    assert payload["email"]["bounced"] == 1
    assert payload["email"]["open_rate"] == 33.3
    assert payload["email"]["bounce_rate"] == 33.3
    assert [(row["position"], row["sent"]) for row in payload["steps"]] == [
        (1, 2),
        (2, 1),
    ]
    assert payload["steps"][0]["subject_a"] == "Workflow idea"
    assert payload["steps"][0]["variants"][0]["replied"] == 1
    assert payload["steps"][0]["variants"][1]["bounced"] == 1
    industries = {row["value"]: row for row in payload["icp"]["industries"]}
    assert industries["Manufacturing"]["sent"] == 2
    assert industries["Manufacturing"]["mql"] == 1
    assert industries["Manufacturing"]["sql"] == 1
    assert industries["SaaS"]["sent"] == 1
    assert payload["whatsapp"] == {
        "queued": 0,
        "sent": 1,
        "delivered": 1,
        "read": 1,
        "failed": 0,
        "delivery_rate": 100.0,
        "read_rate": 100.0,
    }

    endpoint = f"/api/sdr/outbound/campaigns/{campaign.id}/analytics/"
    assert user_client.get(endpoint).status_code == 403
    assert org_b_client.get(endpoint).status_code == 404
