from uuid import uuid4

import pytest
from django.test import override_settings

from leads.models import Lead
from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.intelligence.contracts import build_lead_context
from sdr.models import (
    LeadInspection,
    LeadIntake,
    LeadIntakeSource,
    SalesFeedbackDecision,
    SalesFeedbackReason,
    SDRSalesFeedback,
)
from sdr.sales_feedback import (
    build_ai_calibration_context,
    build_sales_feedback_calibration,
)


def create_handoff(*, org, profile, record_id, band="high", score=84):
    lead = Lead.objects.create(
        org=org,
        first_name="Sales",
        last_name=record_id,
        email=f"{record_id}@example.com",
        status="in process",
    )
    intake = LeadIntake.objects.create(
        org=org,
        source=LeadIntakeSource.WEBSITE_FORM,
        source_record_id=record_id,
        status="completed",
        qualification_score=score,
        qualification_band=band,
        assigned_profile=profile,
        crm_lead=lead,
        crm_created=True,
    )
    return lead, intake


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls")
def test_assigned_sales_can_submit_feedback_with_immutable_ai_snapshot(
    user_client,
    org_b_client,
    user_profile,
    org_a,
):
    user_profile.has_sales_access = True
    user_profile.save(update_fields=["has_sales_access"])
    lead, intake = create_handoff(
        org=org_a,
        profile=user_profile,
        record_id="sales-feedback-api",
    )
    inspection = LeadInspection.objects.create(
        org=org_a,
        intake=intake,
        status="completed",
        qualification_score=88,
        qualification_band="high",
        provider="openai",
        model="gpt-5.6-luna",
        prompt_version="lead-qualification-v2-feedback-calibration",
    )

    initial = user_client.get(f"/api/sdr/sales-feedback/leads/{lead.id}/")
    submitted = user_client.put(
        f"/api/sdr/sales-feedback/leads/{lead.id}/",
        {
            "decision": "rejected",
            "reason": "wrong_role",
            "quality_score": 2,
            "satisfaction_score": 3,
            "notes": "Contact cannot approve this purchase.",
        },
        format="json",
    )

    assert initial.status_code == 200
    assert initial.json()["feedback"] is None
    assert initial.json()["intake"]["qualification_score"] == 88
    assert submitted.status_code == 200
    feedback = submitted.json()["feedback"]
    assert feedback["decision"] == "rejected"
    assert feedback["reason"] == "wrong_role"
    assert feedback["feedback_by_name"] == (
        user_profile.user.name or user_profile.user.email
    )
    assert feedback["qualification_score_snapshot"] == 88
    assert feedback["provider_snapshot"] == "openai"
    assert feedback["model_snapshot"] == "gpt-5.6-luna"

    inspection.qualification_score = 41
    inspection.qualification_band = "medium"
    inspection.model = "changed-model"
    inspection.save()
    revised = user_client.put(
        f"/api/sdr/sales-feedback/leads/{lead.id}/",
        {
            "decision": "accepted",
            "reason": "",
            "quality_score": 5,
            "satisfaction_score": 5,
            "notes": "",
        },
        format="json",
    )
    assert revised.status_code == 200
    revised_feedback = revised.json()["feedback"]
    assert revised_feedback["reason"] == "good_fit"
    assert revised_feedback["qualification_score_snapshot"] == 88
    assert revised_feedback["qualification_band_snapshot"] == "high"
    assert revised_feedback["model_snapshot"] == "gpt-5.6-luna"
    isolated = org_b_client.get(f"/api/sdr/sales-feedback/leads/{lead.id}/")
    assert isolated.status_code == 200
    assert isolated.json() == {"available": False}


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="sdr.tests.urls")
def test_sales_feedback_enforces_sales_access_assignment_and_reasons(
    user_client,
    user_profile,
    admin_profile,
    org_a,
):
    lead, intake = create_handoff(
        org=org_a,
        profile=admin_profile,
        record_id="sales-feedback-permission",
    )
    endpoint = f"/api/sdr/sales-feedback/leads/{lead.id}/"

    hidden = user_client.get(endpoint)
    assert hidden.status_code == 200
    assert hidden.json() == {"available": False}
    user_profile.has_sales_access = True
    user_profile.save(update_fields=["has_sales_access"])
    assert user_client.get(endpoint).status_code == 403

    intake.assigned_profile = user_profile
    intake.save(update_fields=["assigned_profile"])
    missing_reason = user_client.put(
        endpoint,
        {
            "decision": "rejected",
            "reason": "",
            "quality_score": 2,
            "satisfaction_score": 2,
            "notes": "",
        },
        format="json",
    )
    other_without_notes = user_client.put(
        endpoint,
        {
            "decision": "recycle",
            "reason": "other",
            "quality_score": 3,
            "satisfaction_score": 3,
            "notes": "",
        },
        format="json",
    )
    assert missing_reason.status_code == 400
    assert "reason" in missing_reason.json()
    assert other_without_notes.status_code == 400
    assert "notes" in other_without_notes.json()


@pytest.mark.django_db
def test_sales_feedback_calibrates_by_band_and_never_exposes_notes(
    org_a,
    admin_profile,
):
    for index in range(10):
        if index < 6:
            decision = SalesFeedbackDecision.ACCEPTED
            reason = SalesFeedbackReason.GOOD_FIT
            band = "high"
            quality = 5
        elif index < 8:
            decision = SalesFeedbackDecision.REJECTED
            reason = SalesFeedbackReason.WRONG_ROLE
            band = "high"
            quality = 2
        else:
            decision = SalesFeedbackDecision.RECYCLE
            reason = SalesFeedbackReason.BAD_TIMING
            band = "medium"
            quality = 3
        _, intake = create_handoff(
            org=org_a,
            profile=admin_profile,
            record_id=f"calibration-{index}",
            band=band,
            score=86 if band == "high" else 56,
        )
        SDRSalesFeedback.objects.create(
            org=org_a,
            intake=intake,
            feedback_by=admin_profile,
            decision=decision,
            reason=reason,
            quality_score=quality,
            satisfaction_score=4,
            notes="private free-text sales note",
            qualification_score_snapshot=intake.qualification_score,
            qualification_band_snapshot=band,
            provider_snapshot="openai",
            model_snapshot="gpt-5.6-luna",
            prompt_version_snapshot="lead-qualification-v2-feedback-calibration",
        )

    report = build_sales_feedback_calibration(org=org_a)
    context = build_ai_calibration_context(org_id=org_a.id)

    assert report["summary"] == {
        "total": 10,
        "accepted": 6,
        "rejected": 2,
        "recycled": 2,
        "eligible_handoffs": 10,
        "coverage_rate": 100.0,
        "acceptance_rate": 60.0,
        "average_quality": 4.0,
        "average_satisfaction": 4.0,
        "calibration_ready": True,
        "minimum_calibration_samples": 10,
    }
    high = next(row for row in report["by_qualification_band"] if row["band"] == "high")
    assert high["sample_size"] == 8
    assert high["acceptance_rate"] == 75.0
    assert context["sample_size"] == 10
    assert context["overall_acceptance_rate"] == 60.0
    assert "private free-text" not in str(context)


def test_model_context_only_includes_bounded_aggregate_feedback():
    candidate = LeadCandidate(
        org_id=uuid4(),
        source=LeadSource.WEBSITE_FORM,
        source_record_id="context-feedback",
        identity=LeadIdentity(email="buyer@example.com"),
        company=CompanySnapshot(name="Example"),
    )
    calibration = {
        "sample_size": 10,
        "overall_acceptance_rate": 60.0,
        "top_rejection_reasons": [{"reason": "wrong_role", "count": 2}],
    }
    context = build_lead_context(
        candidate=candidate,
        baseline=QualificationResult(55, QualificationBand.MEDIUM),
        research=None,
        icp_description="B2B operations teams",
        positive_signals="Senior buyer",
        negative_signals="Student",
        sales_feedback_calibration=calibration,
    )
    assert context["historical_sales_feedback"] == calibration
