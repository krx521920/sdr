"""Sales feedback reporting and privacy-safe AI calibration context."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, Q
from django.utils import timezone

from sdr.models import (
    LeadIntake,
    SalesFeedbackDecision,
    SalesFeedbackReason,
    SDRSalesFeedback,
)

AI_CALIBRATION_MIN_SAMPLES = 10
AI_CALIBRATION_LOOKBACK_DAYS = 180


def build_sales_feedback_calibration(
    *,
    org,
    start=None,
    end=None,
) -> dict[str, Any]:
    """Aggregate sales verdicts for operators without exposing free-text notes."""

    queryset = SDRSalesFeedback.objects.filter(org=org)
    handoffs = LeadIntake.objects.filter(
        org=org,
        assigned_profile__isnull=False,
        crm_lead__isnull=False,
    )
    if start is not None:
        queryset = queryset.filter(submitted_at__gte=start)
        handoffs = handoffs.filter(created_at__gte=start)
    if end is not None:
        queryset = queryset.filter(submitted_at__lte=end)
        handoffs = handoffs.filter(created_at__lte=end)

    summary_values = queryset.aggregate(
        total=Count("id"),
        accepted=Count("id", filter=Q(decision=SalesFeedbackDecision.ACCEPTED)),
        rejected=Count("id", filter=Q(decision=SalesFeedbackDecision.REJECTED)),
        recycled=Count("id", filter=Q(decision=SalesFeedbackDecision.RECYCLE)),
        average_quality=Avg("quality_score"),
        average_satisfaction=Avg("satisfaction_score"),
    )
    summary = {
        key: int(summary_values[key] or 0)
        for key in ("total", "accepted", "rejected", "recycled")
    }
    eligible_handoffs = handoffs.count()
    summary.update(
        {
            "eligible_handoffs": eligible_handoffs,
            "coverage_rate": _rate(summary["total"], eligible_handoffs),
            "acceptance_rate": _rate(summary["accepted"], summary["total"]),
            "average_quality": _average(summary_values["average_quality"]),
            "average_satisfaction": _average(summary_values["average_satisfaction"]),
            "calibration_ready": summary["total"] >= AI_CALIBRATION_MIN_SAMPLES,
            "minimum_calibration_samples": AI_CALIBRATION_MIN_SAMPLES,
        }
    )

    decision_counts = {
        row["decision"]: int(row["count"])
        for row in queryset.values("decision").annotate(count=Count("id"))
    }
    decisions = [
        {
            "decision": value,
            "label": label,
            "count": decision_counts.get(value, 0),
            "rate": _rate(decision_counts.get(value, 0), summary["total"]),
        }
        for value, label in SalesFeedbackDecision.choices
    ]

    reason_labels = dict(SalesFeedbackReason.choices)
    rejection_reasons = [
        {
            "reason": row["reason"],
            "label": reason_labels.get(row["reason"], row["reason"]),
            "count": int(row["count"]),
            "rate": _rate(row["count"], summary["rejected"] + summary["recycled"]),
        }
        for row in queryset.exclude(reason="")
        .exclude(reason=SalesFeedbackReason.GOOD_FIT)
        .values("reason")
        .annotate(count=Count("id"))
        .order_by("-count", "reason")
    ]

    band_labels = {
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "disqualified": "Disqualified",
        "": "Unknown",
    }
    by_band = []
    band_rows = queryset.values("qualification_band_snapshot").annotate(
        sample_size=Count("id"),
        accepted=Count("id", filter=Q(decision=SalesFeedbackDecision.ACCEPTED)),
        average_quality=Avg("quality_score"),
        average_satisfaction=Avg("satisfaction_score"),
    )
    band_order = {"high": 0, "medium": 1, "low": 2, "disqualified": 3, "": 4}
    for row in sorted(
        band_rows,
        key=lambda item: band_order.get(item["qualification_band_snapshot"], 5),
    ):
        band = row["qualification_band_snapshot"]
        by_band.append(
            {
                "band": band or "unknown",
                "label": band_labels.get(band, band or "Unknown"),
                "sample_size": int(row["sample_size"]),
                "accepted": int(row["accepted"]),
                "acceptance_rate": _rate(row["accepted"], row["sample_size"]),
                "average_quality": _average(row["average_quality"]),
                "average_satisfaction": _average(row["average_satisfaction"]),
            }
        )

    by_model = []
    for row in (
        queryset.values(
            "provider_snapshot",
            "model_snapshot",
            "prompt_version_snapshot",
        )
        .annotate(
            sample_size=Count("id"),
            accepted=Count("id", filter=Q(decision=SalesFeedbackDecision.ACCEPTED)),
            average_quality=Avg("quality_score"),
        )
        .order_by("-sample_size", "provider_snapshot", "model_snapshot")
    ):
        by_model.append(
            {
                "provider": row["provider_snapshot"] or "rules",
                "model": row["model_snapshot"] or "rules-v1",
                "prompt_version": row["prompt_version_snapshot"],
                "sample_size": int(row["sample_size"]),
                "accepted": int(row["accepted"]),
                "acceptance_rate": _rate(row["accepted"], row["sample_size"]),
                "average_quality": _average(row["average_quality"]),
            }
        )

    return {
        "summary": summary,
        "decisions": decisions,
        "rejection_reasons": rejection_reasons,
        "by_qualification_band": by_band,
        "by_model": by_model,
    }


def build_ai_calibration_context(
    *,
    org_id,
    min_samples: int = AI_CALIBRATION_MIN_SAMPLES,
) -> dict[str, Any] | None:
    """Return bounded aggregate feedback; raw notes and lead identity never leave CRM."""

    start = timezone.now() - timedelta(days=AI_CALIBRATION_LOOKBACK_DAYS)
    queryset = SDRSalesFeedback.objects.filter(
        org_id=org_id,
        submitted_at__gte=start,
    )
    total = queryset.count()
    if total < min_samples:
        return None

    accepted = queryset.filter(decision=SalesFeedbackDecision.ACCEPTED).count()
    band_rows = queryset.values("qualification_band_snapshot").annotate(
        sample_size=Count("id"),
        accepted=Count("id", filter=Q(decision=SalesFeedbackDecision.ACCEPTED)),
    )
    reason_labels = dict(SalesFeedbackReason.choices)
    reason_rows = (
        queryset.filter(
            decision__in=(
                SalesFeedbackDecision.REJECTED,
                SalesFeedbackDecision.RECYCLE,
            )
        )
        .exclude(reason="")
        .values("reason")
        .annotate(count=Count("id"))
        .order_by("-count", "reason")[:5]
    )
    return {
        "lookback_days": AI_CALIBRATION_LOOKBACK_DAYS,
        "sample_size": total,
        "overall_acceptance_rate": _rate(accepted, total),
        "acceptance_by_predicted_band": [
            {
                "band": row["qualification_band_snapshot"] or "unknown",
                "sample_size": int(row["sample_size"]),
                "acceptance_rate": _rate(row["accepted"], row["sample_size"]),
            }
            for row in sorted(
                band_rows,
                key=lambda item: item["qualification_band_snapshot"],
            )
        ],
        "top_rejection_reasons": [
            {
                "reason": row["reason"],
                "label": reason_labels.get(row["reason"], row["reason"]),
                "count": int(row["count"]),
            }
            for row in reason_rows
        ],
    }


def feedback_choices() -> dict[str, list[dict[str, str]]]:
    return {
        "decisions": [
            {"value": value, "label": label}
            for value, label in SalesFeedbackDecision.choices
        ],
        "reasons": [
            {"value": value, "label": label}
            for value, label in SalesFeedbackReason.choices
        ],
    }


def _rate(numerator, denominator) -> float:
    return round((numerator or 0) * 100 / denominator, 1) if denominator else 0.0


def _average(value) -> float | None:
    return round(float(value), 1) if value is not None else None
