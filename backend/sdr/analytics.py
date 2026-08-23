"""Organization-scoped SDR funnel and engagement analytics."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from statistics import median
from typing import Any

from django.db.models import Count, Q, QuerySet
from django.db.models.functions import TruncDate
from django.utils import timezone

from sdr.domain import QualificationBand
from sdr.models import (
    LeadDelivery,
    LeadDeliveryKind,
    LeadDeliveryStatus,
    LeadIntake,
    LeadIntakeSource,
    LeadIntakeStatus,
    LeadNurtureDelivery,
    NurtureDeliveryStatus,
    NurtureReplySentiment,
    SDRResponseSettings,
)
from sdr.sales_feedback import build_sales_feedback_calibration

MQL_BANDS = (QualificationBand.HIGH.value, QualificationBand.MEDIUM.value)

PROCESSED_FILTER = Q(status=LeadIntakeStatus.COMPLETED)
MQL_FILTER = PROCESSED_FILTER & Q(qualification_band__in=MQL_BANDS)
HANDOFF_FILTER = MQL_FILTER & Q(
    assigned_profile__isnull=False,
    crm_lead__isnull=False,
)
SQL_FILTER = HANDOFF_FILTER & Q(crm_lead__status="converted")


def build_sdr_analytics(*, org, days: int = 30) -> dict[str, Any]:
    """Build a bounded growth report from existing SDR and CRM facts."""

    now = timezone.now()
    current_timezone = timezone.get_current_timezone()
    local_today = timezone.localtime(now, current_timezone).date()
    start_date = local_today - timedelta(days=days - 1)
    start = timezone.make_aware(
        datetime.combine(start_date, time.min),
        current_timezone,
    )
    previous_start = start - timedelta(days=days)

    all_intakes = LeadIntake.objects.filter(org=org)
    current = all_intakes.filter(created_at__gte=start, created_at__lte=now)
    previous = all_intakes.filter(
        created_at__gte=previous_start,
        created_at__lt=start,
    )

    counts = _funnel_counts(current)
    previous_counts = _funnel_counts(previous)
    funnel = _funnel_rows(counts)
    engagement = _engagement_metrics(org=org, start=start, end=now)
    sources = _source_metrics(current)
    response_sla = _response_sla_metrics(
        org=org,
        start=start,
        end=now,
    )
    sales_feedback = build_sales_feedback_calibration(
        org=org,
        start=start,
        end=now,
    )

    return {
        "period": {
            "days": days,
            "start": start.isoformat(),
            "end": now.isoformat(),
            "previous_start": previous_start.isoformat(),
            "previous_end": start.isoformat(),
        },
        "kpis": {
            "received": _comparison(counts["received"], previous_counts["received"]),
            "mql": _comparison(counts["mql"], previous_counts["mql"]),
            "sql": _comparison(counts["sql"], previous_counts["sql"]),
            "mql_rate": {
                "value": _rate(counts["mql"], counts["received"]),
                "previous": _rate(
                    previous_counts["mql"],
                    previous_counts["received"],
                ),
            },
            "mql_to_sql_rate": {
                "value": _rate(counts["sql"], counts["mql"]),
                "previous": _rate(
                    previous_counts["sql"],
                    previous_counts["mql"],
                ),
            },
        },
        "funnel": funnel,
        "sources": sources,
        "trend": _daily_trend(
            current,
            start_date=start_date,
            days=days,
            tzinfo=current_timezone,
        ),
        "engagement": engagement,
        "response_sla": response_sla,
        "sales_feedback": sales_feedback,
        "insights": _growth_insights(
            funnel=funnel,
            sources=sources,
            engagement=engagement,
            response_sla=response_sla,
        ),
        "definitions": {
            "mql": "Completed intake with a high or medium qualification band.",
            "sales_handoff": (
                "MQL assigned to a sales profile and linked to a CRM lead."
            ),
            "sql": "Sales-handoff MQL whose CRM lead status is converted.",
            "sales_feedback": (
                "Sales verdicts submitted during the selected period; AI model "
                "calibration activates after 10 aggregate samples."
            ),
            "engagement_window": (
                "Nurture emails sent during the selected period; later delivery "
                "and interaction outcomes are attributed to those sends."
            ),
        },
    }


def _funnel_counts(queryset: QuerySet[LeadIntake]) -> dict[str, int]:
    result = queryset.aggregate(
        received=Count("id"),
        processed=Count("id", filter=PROCESSED_FILTER),
        mql=Count("id", filter=MQL_FILTER),
        sales_handoff=Count("id", filter=HANDOFF_FILTER),
        sql=Count("id", filter=SQL_FILTER),
        failed=Count("id", filter=Q(status=LeadIntakeStatus.FAILED)),
    )
    return {key: int(value or 0) for key, value in result.items()}


def _funnel_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    definitions = (
        ("received", "Received"),
        ("processed", "Processed"),
        ("mql", "MQL"),
        ("sales_handoff", "Sales handoff"),
        ("sql", "SQL"),
    )
    rows: list[dict[str, Any]] = []
    previous_count: int | None = None
    for key, label in definitions:
        count = counts[key]
        rows.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "from_previous_rate": (
                    100.0 if previous_count is None else _rate(count, previous_count)
                ),
                "dropoff_count": (
                    0 if previous_count is None else max(previous_count - count, 0)
                ),
            }
        )
        previous_count = count
    return rows


def _source_metrics(queryset: QuerySet[LeadIntake]) -> list[dict[str, Any]]:
    aggregated = {
        row["source"]: row
        for row in queryset.values("source").annotate(
            received=Count("id"),
            mql=Count("id", filter=MQL_FILTER),
            sales_handoff=Count("id", filter=HANDOFF_FILTER),
            sql=Count("id", filter=SQL_FILTER),
        )
    }
    rows = []
    for value, label in LeadIntakeSource.choices:
        values = aggregated.get(value, {})
        received = int(values.get("received", 0) or 0)
        mql = int(values.get("mql", 0) or 0)
        sales_handoff = int(values.get("sales_handoff", 0) or 0)
        sql = int(values.get("sql", 0) or 0)
        rows.append(
            {
                "source": value,
                "label": label,
                "received": received,
                "mql": mql,
                "sales_handoff": sales_handoff,
                "sql": sql,
                "mql_rate": _rate(mql, received),
                "sql_rate": _rate(sql, received),
            }
        )
    return rows


def _daily_trend(
    queryset: QuerySet[LeadIntake],
    *,
    start_date,
    days: int,
    tzinfo,
) -> list[dict[str, Any]]:
    aggregated = {
        row["day"]: row
        for row in queryset.annotate(
            day=TruncDate("created_at", tzinfo=tzinfo)
        )
        .values("day")
        .annotate(
            received=Count("id"),
            mql=Count("id", filter=MQL_FILTER),
            sql=Count("id", filter=SQL_FILTER),
        )
        .order_by("day")
    }
    return [
        {
            "date": day.isoformat(),
            "received": int(aggregated.get(day, {}).get("received", 0) or 0),
            "mql": int(aggregated.get(day, {}).get("mql", 0) or 0),
            "sql": int(aggregated.get(day, {}).get("sql", 0) or 0),
        }
        for day in (start_date + timedelta(days=offset) for offset in range(days))
    ]


def _engagement_metrics(*, org, start, end) -> dict[str, Any]:
    deliveries = LeadNurtureDelivery.objects.filter(
        org=org,
        status=NurtureDeliveryStatus.SENT,
        sent_at__gte=start,
        sent_at__lte=end,
    )
    summary = deliveries.aggregate(
        sent=Count("id"),
        delivered=Count("id", filter=Q(delivered_at__isnull=False)),
        opened=Count("id", filter=Q(opened_at__isnull=False)),
        clicked=Count("id", filter=Q(clicked_at__isnull=False)),
        replied=Count("id", filter=Q(replied_at__isnull=False)),
        positive_replies=Count(
            "id",
            filter=Q(reply_sentiment=NurtureReplySentiment.POSITIVE),
        ),
        bounced=Count("id", filter=Q(bounced_at__isnull=False)),
        complained=Count("id", filter=Q(complained_at__isnull=False)),
    )
    values = {key: int(value or 0) for key, value in summary.items()}
    sent = values["sent"]
    values.update(
        {
            "delivery_rate": _rate(values["delivered"], sent),
            "open_rate": _rate(values["opened"], sent),
            "click_rate": _rate(values["clicked"], sent),
            "reply_rate": _rate(values["replied"], sent),
            "positive_reply_rate": _rate(values["positive_replies"], sent),
            "bounce_rate": _rate(values["bounced"], sent),
            "complaint_rate": _rate(values["complained"], sent),
        }
    )

    variants = []
    for variant in ("A", "B"):
        variant_values = deliveries.filter(variant=variant).aggregate(
            sent=Count("id"),
            opened=Count("id", filter=Q(opened_at__isnull=False)),
            replied=Count("id", filter=Q(replied_at__isnull=False)),
            positive_replies=Count(
                "id",
                filter=Q(reply_sentiment=NurtureReplySentiment.POSITIVE),
            ),
        )
        variant_sent = int(variant_values["sent"] or 0)
        variants.append(
            {
                "variant": variant,
                "sent": variant_sent,
                "opened": int(variant_values["opened"] or 0),
                "replied": int(variant_values["replied"] or 0),
                "positive_replies": int(
                    variant_values["positive_replies"] or 0
                ),
                "open_rate": _rate(variant_values["opened"], variant_sent),
                "reply_rate": _rate(variant_values["replied"], variant_sent),
                "positive_reply_rate": _rate(
                    variant_values["positive_replies"],
                    variant_sent,
                ),
            }
        )
    values["variants"] = variants
    return values


def _response_sla_metrics(*, org, start, end) -> dict[str, Any]:
    configured_sla = (
        SDRResponseSettings.objects.filter(org=org)
        .values_list("response_sla_seconds", flat=True)
        .first()
        or 60
    )
    rows = LeadDelivery.objects.filter(
        org=org,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
        status=LeadDeliveryStatus.SENT,
        intake__created_at__gte=start,
        intake__created_at__lte=end,
        sent_at__isnull=False,
    ).values_list("intake__created_at", "sent_at")
    durations = [
        max(0, round((sent_at - created_at).total_seconds()))
        for created_at, sent_at in rows
    ]
    within_sla = sum(value <= configured_sla for value in durations)
    return {
        "sla_seconds": configured_sla,
        "sample_size": len(durations),
        "average_seconds": (
            round(sum(durations) / len(durations)) if durations else None
        ),
        "median_seconds": round(median(durations)) if durations else None,
        "within_sla": within_sla,
        "breached": len(durations) - within_sla,
        "within_sla_rate": _rate(within_sla, len(durations)),
    }


def _growth_insights(
    *,
    funnel: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    engagement: dict[str, Any],
    response_sla: dict[str, Any],
) -> list[dict[str, str]]:
    if not funnel[0]["count"]:
        return [
            {
                "level": "info",
                "title": "No inbound cohort yet",
                "detail": "No SDR intakes arrived during this reporting period.",
                "action": "Verify source connections or expand the reporting window.",
            }
        ]

    insights: list[dict[str, str]] = []
    dropoffs = []
    for previous, current in zip(funnel, funnel[1:]):
        if previous["count"]:
            dropoffs.append(
                (
                    100 - current["from_previous_rate"],
                    previous,
                    current,
                )
            )
    if dropoffs:
        _, previous, current = max(dropoffs, key=lambda item: item[0])
        actions = {
            "processed": "Inspect failed automation jobs and provider payload errors.",
            "mql": "Tighten source targeting or revisit the ICP and scoring criteria.",
            "sales_handoff": "Add routing coverage for the affected countries and sources.",
            "sql": "Review sales follow-up speed, objections, and lead acceptance criteria.",
        }
        insights.append(
            {
                "level": "warning",
                "title": f"Largest drop: {previous['label']} to {current['label']}",
                "detail": (
                    f"{current['dropoff_count']} leads dropped at this step; "
                    f"{current['from_previous_rate']:.1f}% advanced."
                ),
                "action": actions[current["key"]],
            }
        )

    qualified_sources = [row for row in sources if row["received"] >= 5]
    if qualified_sources:
        best = max(qualified_sources, key=lambda row: (row["mql_rate"], row["mql"]))
        insights.append(
            {
                "level": "success",
                "title": f"Best MQL source: {best['label']}",
                "detail": (
                    f"{best['mql']} of {best['received']} leads became MQLs "
                    f"({best['mql_rate']:.1f}%)."
                ),
                "action": "Use this source as the benchmark before reallocating spend.",
            }
        )

    if engagement["sent"] < 20:
        insights.append(
            {
                "level": "info",
                "title": "A/B sample is still small",
                "detail": f"Only {engagement['sent']} nurture emails were sent.",
                "action": "Collect at least 20 sends before choosing a message winner.",
            }
        )
    elif engagement["open_rate"] < 40:
        insights.append(
            {
                "level": "warning",
                "title": "Open rate is below the 40% target",
                "detail": f"Current open rate is {engagement['open_rate']:.1f}%.",
                "action": "Test the subject line, sender identity, and ICP relevance.",
            }
        )
    elif engagement["reply_rate"] < 8:
        insights.append(
            {
                "level": "warning",
                "title": "Reply rate is below the 8% target",
                "detail": f"Current reply rate is {engagement['reply_rate']:.1f}%.",
                "action": "Test a clearer CTA and more specific problem framing.",
            }
        )

    if response_sla["sample_size"] and response_sla["within_sla_rate"] < 90:
        insights.append(
            {
                "level": "warning",
                "title": "First-response SLA needs attention",
                "detail": (
                    f"{response_sla['breached']} acknowledgements exceeded the "
                    f"{response_sla['sla_seconds']}s target."
                ),
                "action": "Inspect queue latency and acknowledgement delivery failures.",
            }
        )

    return insights[:4]


def _comparison(current: int, previous: int) -> dict[str, Any]:
    return {
        "value": current,
        "previous": previous,
        "change_percent": (
            round((current - previous) * 100 / previous, 1) if previous else None
        ),
    }


def _rate(numerator: int | None, denominator: int | None) -> float:
    return round((numerator or 0) * 100 / denominator, 1) if denominator else 0.0
