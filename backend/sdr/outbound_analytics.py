"""Campaign-scoped outbound funnel, ICP, channel, and step analytics."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from sdr.domain import QualificationBand
from sdr.models import (
    LeadIntakeStatus,
    LeadNurtureDelivery,
    NurtureDeliveryStatus,
    NurtureReplySentiment,
    OutboundProspectStatus,
    SDROutboundCampaign,
    SDROutboundProspect,
)
from sdr.provider_ports import ProviderAdapterUnavailable, outbound_channel_adapter

MQL_BANDS = (QualificationBand.HIGH.value, QualificationBand.MEDIUM.value)
PROSPECT_MQL_FILTER = Q(
    intake__status=LeadIntakeStatus.COMPLETED,
    intake__qualification_band__in=MQL_BANDS,
)
PROSPECT_HANDOFF_FILTER = PROSPECT_MQL_FILTER & Q(
    intake__assigned_profile__isnull=False,
    intake__crm_lead__isnull=False,
)
PROSPECT_SQL_FILTER = PROSPECT_HANDOFF_FILTER & Q(
    intake__crm_lead__status="converted"
)
SENT_FILTER = Q(
    status=NurtureDeliveryStatus.SENT,
    sent_at__isnull=False,
)


def build_outbound_campaign_analytics(
    *,
    org,
    campaign: SDROutboundCampaign,
) -> dict[str, Any]:
    """Build lifetime metrics using prospect-linked delivery attribution."""

    prospects = SDROutboundProspect.objects.filter(org=org, campaign=campaign)
    deliveries = LeadNurtureDelivery.objects.filter(
        org=org,
        enrollment__intake__outbound_prospect__campaign=campaign,
    )
    cohort = _cohort_metrics(prospects)
    return {
        "campaign": {
            "id": str(campaign.id),
            "name": campaign.name,
            "status": campaign.status,
            "icp_description": campaign.icp_description,
            "channels": campaign.channels,
            "sequence_id": str(campaign.sequence_id) if campaign.sequence_id else None,
            "sequence_name": campaign.sequence.name if campaign.sequence_id else "",
        },
        "period": {
            "kind": "campaign_lifetime",
            "start": (campaign.launched_at or campaign.created_at).isoformat(),
            "end": timezone.now().isoformat(),
        },
        "cohort": cohort,
        "email": _email_metrics(deliveries),
        "linkedin": _linkedin_metrics(org=org, campaign=campaign),
        "whatsapp": _whatsapp_metrics(org=org, campaign=campaign),
        "steps": _step_metrics(campaign=campaign, deliveries=deliveries),
        "icp": {
            "description": campaign.icp_description,
            "industries": _segment_metrics(prospects, "industry"),
            "countries": _segment_metrics(prospects, "country"),
            "qualification_bands": _qualification_metrics(prospects),
        },
        "definitions": {
            "attribution": (
                "Email outcomes are attributed through the outbound prospect's "
                "intake and nurture enrollment, not only the selected sequence."
            ),
            "mql": (
                "Promoted prospect with a completed intake and a high or medium "
                "qualification band."
            ),
            "sales_handoff": (
                "MQL assigned to a sales profile and linked to a CRM lead."
            ),
            "sql": "Sales-handoff MQL whose CRM lead status is converted.",
            "rates": "Email engagement and bounce rates use sent emails as denominator.",
        },
    }


def _cohort_metrics(prospects: QuerySet[SDROutboundProspect]) -> dict[str, Any]:
    summary = prospects.aggregate(
        prospects=Count("id"),
        promoted=Count(
            "id",
            filter=Q(status=OutboundProspectStatus.PROMOTED),
        ),
        enrolled=Count(
            "id",
            filter=Q(intake__nurture_enrollment__isnull=False),
            distinct=True,
        ),
        mql=Count("id", filter=PROSPECT_MQL_FILTER, distinct=True),
        sales_handoff=Count(
            "id",
            filter=PROSPECT_HANDOFF_FILTER,
            distinct=True,
        ),
        sql=Count("id", filter=PROSPECT_SQL_FILTER, distinct=True),
    )
    values = _integers(summary)
    values.update(
        {
            "promotion_rate": _rate(values["promoted"], values["prospects"]),
            "mql_rate": _rate(values["mql"], values["prospects"]),
            "sql_rate": _rate(values["sql"], values["prospects"]),
            "mql_to_sql_rate": _rate(values["sql"], values["mql"]),
        }
    )
    return values


def _email_metrics(deliveries: QuerySet[LeadNurtureDelivery]) -> dict[str, Any]:
    values = _delivery_aggregate(deliveries)
    values["variants"] = [
        {
            "variant": variant,
            **_delivery_aggregate(deliveries.filter(variant=variant)),
        }
        for variant in ("A", "B")
    ]
    return values


def _delivery_aggregate(
    deliveries: QuerySet[LeadNurtureDelivery],
) -> dict[str, Any]:
    summary = deliveries.aggregate(
        sent=Count("id", filter=SENT_FILTER),
        failed=Count(
            "id",
            filter=Q(status=NurtureDeliveryStatus.FAILED),
        ),
        delivered=Count(
            "id",
            filter=SENT_FILTER & Q(delivered_at__isnull=False),
        ),
        opened=Count(
            "id",
            filter=SENT_FILTER & Q(opened_at__isnull=False),
        ),
        clicked=Count(
            "id",
            filter=SENT_FILTER & Q(clicked_at__isnull=False),
        ),
        replied=Count(
            "id",
            filter=SENT_FILTER & Q(replied_at__isnull=False),
        ),
        positive_replies=Count(
            "id",
            filter=SENT_FILTER
            & Q(reply_sentiment=NurtureReplySentiment.POSITIVE),
        ),
        bounced=Count(
            "id",
            filter=SENT_FILTER & Q(bounced_at__isnull=False),
        ),
        complained=Count(
            "id",
            filter=SENT_FILTER & Q(complained_at__isnull=False),
        ),
    )
    return _with_email_rates(_integers(summary))


def _step_metrics(
    *,
    campaign: SDROutboundCampaign,
    deliveries: QuerySet[LeadNurtureDelivery],
) -> list[dict[str, Any]]:
    grouped = {
        (int(row["step_position"]), row["variant"]): _with_email_rates(
            _integers({key: row[key] for key in _DELIVERY_METRIC_KEYS})
        )
        for row in _grouped_delivery_rows(deliveries)
    }
    configured = {}
    if campaign.sequence_id:
        configured = {
            step.position: {
                "subject_a": step.subject_a,
                "subject_b": step.subject_b,
                "variant_b_percent": step.variant_b_percent,
            }
            for step in campaign.sequence.steps.all()
        }
    positions = sorted(
        set(configured) | {position for position, _variant in grouped}
    )
    results = []
    for position in positions:
        variants = []
        for variant in ("A", "B"):
            metrics = grouped.get(
                (position, variant),
                _with_email_rates({key: 0 for key in _DELIVERY_METRIC_KEYS}),
            )
            variants.append({"variant": variant, **metrics})
        totals = {
            key: sum(row[key] for row in variants)
            for key in _DELIVERY_METRIC_KEYS
        }
        results.append(
            {
                "position": position,
                **configured.get(
                    position,
                    {"subject_a": "", "subject_b": "", "variant_b_percent": 0},
                ),
                **_with_email_rates(totals),
                "variants": variants,
            }
        )
    return results


def _grouped_delivery_rows(deliveries):
    return deliveries.values("step_position", "variant").annotate(
        sent=Count("id", filter=SENT_FILTER),
        failed=Count("id", filter=Q(status=NurtureDeliveryStatus.FAILED)),
        delivered=Count(
            "id",
            filter=SENT_FILTER & Q(delivered_at__isnull=False),
        ),
        opened=Count(
            "id",
            filter=SENT_FILTER & Q(opened_at__isnull=False),
        ),
        clicked=Count(
            "id",
            filter=SENT_FILTER & Q(clicked_at__isnull=False),
        ),
        replied=Count(
            "id",
            filter=SENT_FILTER & Q(replied_at__isnull=False),
        ),
        positive_replies=Count(
            "id",
            filter=SENT_FILTER
            & Q(reply_sentiment=NurtureReplySentiment.POSITIVE),
        ),
        bounced=Count(
            "id",
            filter=SENT_FILTER & Q(bounced_at__isnull=False),
        ),
        complained=Count(
            "id",
            filter=SENT_FILTER & Q(complained_at__isnull=False),
        ),
    )


def _segment_metrics(
    prospects: QuerySet[SDROutboundProspect],
    field: str,
) -> list[dict[str, Any]]:
    delivery_path = "intake__nurture_enrollment__deliveries"
    rows = (
        prospects.values(field)
        .annotate(
            prospects=Count("id", distinct=True),
            promoted=Count(
                "id",
                filter=Q(status=OutboundProspectStatus.PROMOTED),
                distinct=True,
            ),
            mql=Count("id", filter=PROSPECT_MQL_FILTER, distinct=True),
            sql=Count("id", filter=PROSPECT_SQL_FILTER, distinct=True),
            sent=Count(
                delivery_path,
                filter=Q(
                    **{
                        f"{delivery_path}__status": NurtureDeliveryStatus.SENT,
                        f"{delivery_path}__sent_at__isnull": False,
                    }
                ),
                distinct=True,
            ),
            opened=Count(
                delivery_path,
                filter=Q(
                    **{
                        f"{delivery_path}__status": NurtureDeliveryStatus.SENT,
                        f"{delivery_path}__sent_at__isnull": False,
                        f"{delivery_path}__opened_at__isnull": False,
                    }
                ),
                distinct=True,
            ),
            replied=Count(
                delivery_path,
                filter=Q(
                    **{
                        f"{delivery_path}__status": NurtureDeliveryStatus.SENT,
                        f"{delivery_path}__sent_at__isnull": False,
                        f"{delivery_path}__replied_at__isnull": False,
                    }
                ),
                distinct=True,
            ),
        )
        .order_by("-prospects", field)[:10]
    )
    results = []
    for row in rows:
        values = _integers(
            {
                key: row[key]
                for key in (
                    "prospects",
                    "promoted",
                    "mql",
                    "sql",
                    "sent",
                    "opened",
                    "replied",
                )
            }
        )
        results.append(
            {
                "value": row[field] or "Unknown",
                **values,
                "mql_rate": _rate(values["mql"], values["prospects"]),
                "sql_rate": _rate(values["sql"], values["prospects"]),
                "open_rate": _rate(values["opened"], values["sent"]),
                "reply_rate": _rate(values["replied"], values["sent"]),
            }
        )
    return results


def _qualification_metrics(
    prospects: QuerySet[SDROutboundProspect],
) -> list[dict[str, Any]]:
    rows = (
        prospects.filter(intake__isnull=False)
        .values("intake__qualification_band")
        .annotate(prospects=Count("id", distinct=True))
        .order_by("-prospects", "intake__qualification_band")
    )
    return [
        {
            "value": row["intake__qualification_band"] or "unscored",
            "prospects": int(row["prospects"] or 0),
        }
        for row in rows
    ]


def _whatsapp_metrics(*, org, campaign) -> dict[str, Any]:
    try:
        metrics = outbound_channel_adapter("whatsapp").campaign_metrics(
            org_id=org.id,
            campaign_id=campaign.id,
        )
    except ProviderAdapterUnavailable:
        metrics = {}
    return {
        "queued": int(metrics.get("queued", 0) or 0),
        "sent": int(metrics.get("sent", 0) or 0),
        "delivered": int(metrics.get("delivered", 0) or 0),
        "read": int(metrics.get("read", 0) or 0),
        "failed": int(metrics.get("failed", 0) or 0),
        "delivery_rate": float(metrics.get("delivery_rate", 0) or 0),
        "read_rate": float(metrics.get("read_rate", 0) or 0),
    }


def _linkedin_metrics(*, org, campaign) -> dict[str, Any]:
    try:
        metrics = outbound_channel_adapter("linkedin").campaign_metrics(
            org_id=org.id,
            campaign_id=campaign.id,
        )
    except ProviderAdapterUnavailable:
        metrics = {}
    return {
        "queued": int(metrics.get("queued", 0) or 0),
        "sent": int(metrics.get("sent", 0) or 0),
        "failed": int(metrics.get("failed", 0) or 0),
        "skipped": int(metrics.get("skipped", 0) or 0),
    }


_DELIVERY_METRIC_KEYS = (
    "sent",
    "failed",
    "delivered",
    "opened",
    "clicked",
    "replied",
    "positive_replies",
    "bounced",
    "complained",
)


def _with_email_rates(values: dict[str, int]) -> dict[str, Any]:
    sent = values["sent"]
    return {
        **values,
        "delivery_rate": _rate(values["delivered"], sent),
        "open_rate": _rate(values["opened"], sent),
        "click_rate": _rate(values["clicked"], sent),
        "reply_rate": _rate(values["replied"], sent),
        "positive_reply_rate": _rate(values["positive_replies"], sent),
        "bounce_rate": _rate(values["bounced"], sent),
        "complaint_rate": _rate(values["complained"], sent),
    }


def _integers(values: dict[str, Any]) -> dict[str, int]:
    return {key: int(value or 0) for key, value in values.items()}


def _rate(numerator: Any, denominator: Any) -> float:
    if not denominator:
        return 0.0
    return round((int(numerator or 0) / int(denominator)) * 100, 1)
