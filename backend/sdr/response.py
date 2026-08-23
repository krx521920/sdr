"""Reliable acknowledgement and sales handoff delivery for completed intakes."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.template import Context, Template, TemplateSyntaxError
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from common import notifications
from sdr.compliance import intake_data_restriction
from sdr.models import (
    LeadDelivery,
    LeadDeliveryKind,
    LeadDeliveryStatus,
    LeadIntake,
    LeadIntakeSource,
    LeadIntakeStatus,
    LeadLifecycleEvent,
    LeadLifecycleEventType,
    SDRResponseSettings,
)
from sdr.provider_ports import (
    ProviderAdapterError,
    ProviderAdapterUnavailable,
    research_result_sink_adapter,
)

logger = logging.getLogger(__name__)

ACKNOWLEDGEMENT_JOB = "sdr.send_acknowledgement"
SALES_IN_APP_JOB = "sdr.notify_sales_in_app"
SALES_FEISHU_JOB = "sdr.notify_sales_feishu"

ALLOWED_FEISHU_WEBHOOK_HOSTS = frozenset(
    {
        "open.feishu.cn",
        "open.larksuite.com",
    }
)
ALLOWED_TEMPLATE_VARIABLES = frozenset(
    {
        "first_name",
        "last_name",
        "company_name",
        "organization_name",
        "qualification_band",
        "qualification_score",
    }
)
TEMPLATE_VARIABLE_PATTERN = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*}}")


def validate_feishu_webhook_url(value: str) -> str:
    """Accept only official custom-bot endpoints to prevent webhook SSRF."""

    cleaned = value.strip()
    parsed = urlparse(cleaned)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_FEISHU_WEBHOOK_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or len(parsed.path.rsplit("/", 1)[-1]) < 8
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter an official Feishu or Lark custom-bot webhook URL.")
    return cleaned


def validate_message_template(value: str) -> str:
    if "{%" in value or "{#" in value:
        raise ValueError("Template tags and comments are not allowed.")
    for variable in TEMPLATE_VARIABLE_PATTERN.findall(value):
        if variable not in ALLOWED_TEMPLATE_VARIABLES:
            raise ValueError(f'Unknown template variable: "{variable}".')
    remainder = TEMPLATE_VARIABLE_PATTERN.sub("", value)
    if "{{" in remainder or "}}" in remainder:
        raise ValueError("Use only the documented simple template variables.")
    try:
        Template(value)
    except TemplateSyntaxError as exc:
        raise ValueError(f"Invalid message template: {exc}") from exc
    return value


def response_settings_for(org_id: UUID) -> SDRResponseSettings:
    configuration, _ = SDRResponseSettings.objects.get_or_create(org_id=org_id)
    return configuration


def record_lifecycle_event(
    *,
    intake: LeadIntake,
    event_type: str,
    event_key: str,
    data: Mapping[str, Any] | None = None,
) -> LeadLifecycleEvent:
    event, _ = LeadLifecycleEvent.objects.get_or_create(
        org_id=intake.org_id,
        intake=intake,
        event_key=event_key[:120],
        defaults={
            "event_type": event_type,
            "data": dict(data or {}),
        },
    )
    return event


def schedule_post_handoff_jobs(intake: LeadIntake) -> list:
    """Persist and best-effort publish every enabled handoff delivery."""

    if intake.status != LeadIntakeStatus.COMPLETED or intake_data_restriction(intake):
        return []
    configuration = response_settings_for(intake.org_id)
    jobs = []
    acknowledgement_job = (
        None
        if intake.source == LeadIntakeSource.OUTBOUND
        else schedule_acknowledgement_job(intake)
    )
    if acknowledgement_job:
        jobs.append(acknowledgement_job)

    if configuration.sales_in_app_enabled and intake.assigned_profile_id:
        jobs.append(
            _create_delivery_job(
                intake=intake,
                kind=LeadDeliveryKind.SALES_IN_APP,
                recipient=str(intake.assigned_profile_id),
                job_name=SALES_IN_APP_JOB,
            )
        )

    if (
        configuration.feishu_enabled
        and configuration.feishu_webhook_ciphertext
        and intake.assigned_profile_id
    ):
        jobs.append(
            _create_delivery_job(
                intake=intake,
                kind=LeadDeliveryKind.SALES_FEISHU,
                recipient="feishu-group",
                job_name=SALES_FEISHU_JOB,
            )
        )

    try:
        sink = research_result_sink_adapter("feishu_base")
        if sink.is_ready(org_id=intake.org_id):
            jobs.append(sink.enqueue(intake=intake))
    except ProviderAdapterUnavailable:
        logger.debug("Feishu Base research-result sink is not registered")
    except ProviderAdapterError:
        logger.exception("Could not schedule Feishu Base sync for intake %s", intake.id)

    for job in jobs:
        if job.name == ACKNOWLEDGEMENT_JOB:
            continue
        try:
            dispatch_job(job)
        except Exception:
            logger.exception("Could not dispatch SDR response job %s", job.id)
    return jobs


def schedule_acknowledgement_job(intake: LeadIntake):
    """Queue the customer acknowledgement without waiting for qualification."""

    if intake_data_restriction(intake):
        return None
    configuration = response_settings_for(intake.org_id)
    email = _lead_email(intake)
    if not configuration.acknowledgement_email_enabled or not email:
        return None
    job = _create_delivery_job(
        intake=intake,
        kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
        recipient=email,
        job_name=ACKNOWLEDGEMENT_JOB,
    )
    try:
        dispatch_job(job)
    except Exception:
        logger.exception("Could not dispatch SDR acknowledgement job %s", job.id)
    return job


def reconcile_recent_response_jobs(*, org_id: UUID, limit: int = 100) -> int:
    """Recover response jobs missed after a worker or broker interruption."""

    cutoff = timezone.now() - timedelta(days=1)
    intakes = list(
        LeadIntake.objects.filter(
            org_id=org_id,
            status=LeadIntakeStatus.COMPLETED,
            processed_at__gte=cutoff,
        )
        .select_related("crm_lead", "assigned_profile__user")
        .order_by("processed_at")[:limit]
    )
    scheduled = 0
    for intake in intakes:
        scheduled += len(schedule_post_handoff_jobs(intake))
    return scheduled


def _create_delivery_job(
    *,
    intake: LeadIntake,
    kind: str,
    recipient: str,
    job_name: str,
):
    delivery, _ = LeadDelivery.objects.get_or_create(
        org_id=intake.org_id,
        intake=intake,
        kind=kind,
        recipient=recipient,
    )
    enqueued = enqueue_job(
        JobRequest(
            org_id=intake.org_id,
            name=job_name,
            idempotency_key=f"delivery:{delivery.id}",
            payload={
                "org_id": str(intake.org_id),
                "intake_id": str(intake.id),
                "delivery_id": str(delivery.id),
            },
            max_attempts=5,
        )
    )
    return enqueued.job


def process_acknowledgement_email_job(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    intake, delivery = _load_delivery(
        payload,
        expected_kind=LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL,
    )
    if delivery.status in (LeadDeliveryStatus.SENT, LeadDeliveryStatus.SKIPPED):
        return _delivery_result(delivery)

    restriction = intake_data_restriction(intake)
    if restriction:
        _skip_delivery(delivery, code=restriction.code)
        return _delivery_result(delivery)

    configuration = response_settings_for(intake.org_id)
    if not configuration.acknowledgement_email_enabled:
        _skip_delivery(delivery, code="acknowledgement_disabled")
        return _delivery_result(delivery)

    email = _lead_email(intake)
    try:
        validate_email(email)
    except ValidationError:
        _skip_delivery(delivery, code="recipient_email_unavailable")
        return _delivery_result(delivery)

    _start_delivery(delivery)
    context = _template_context(intake)
    try:
        subject = _render_message_template(
            configuration.acknowledgement_subject, context
        )
        body = _render_message_template(configuration.acknowledgement_body, context)
        sent = send_mail(
            subject=subject.strip()[:255],
            message=body,
            from_email=(
                configuration.acknowledgement_from_email or settings.DEFAULT_FROM_EMAIL
            ),
            recipient_list=[email],
            fail_silently=False,
        )
        if sent != 1:
            raise RuntimeError("The email backend did not accept the message.")
    except (TemplateSyntaxError, ValueError) as exc:
        _fail_delivery(delivery, code="invalid_email_template", message=str(exc))
        raise PermanentJobError(str(exc), code="invalid_email_template") from exc
    except Exception as exc:
        _fail_delivery(delivery, code="email_delivery_failed", message=str(exc))
        raise RetryableJobError(
            "The acknowledgement email could not be delivered.",
            code="email_delivery_failed",
        ) from exc

    _complete_delivery(delivery)
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.ACKNOWLEDGEMENT_SENT,
        event_key="delivery:acknowledgement_email",
        data={"recipient": email},
    )
    return _delivery_result(delivery)


def process_sales_in_app_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    intake, delivery = _load_delivery(
        payload,
        expected_kind=LeadDeliveryKind.SALES_IN_APP,
    )
    if delivery.status in (LeadDeliveryStatus.SENT, LeadDeliveryStatus.SKIPPED):
        return _delivery_result(delivery)
    restriction = intake_data_restriction(intake)
    if restriction:
        _skip_delivery(delivery, code=restriction.code)
        return _delivery_result(delivery)
    configuration = response_settings_for(intake.org_id)
    if not configuration.sales_in_app_enabled:
        _skip_delivery(delivery, code="sales_in_app_disabled")
        return _delivery_result(delivery)
    profile = intake.assigned_profile
    if profile is None or not profile.is_active:
        _skip_delivery(delivery, code="assignee_unavailable")
        return _delivery_result(delivery)

    _start_delivery(delivery)
    try:
        notification = notifications.create(
            profile,
            "sdr_lead_assigned",
            entity=intake.crm_lead,
            entity_name=_company_name(intake),
            link=_crm_lead_path(intake),
            data={
                "intake_id": str(intake.id),
                "qualification_score": intake.qualification_score,
                "qualification_band": intake.qualification_band,
                "routing_reason": intake.routing_reason,
            },
        )
        if notification is None:
            _skip_delivery(delivery, code="assignee_unavailable")
            return _delivery_result(delivery)
    except Exception as exc:
        _fail_delivery(delivery, code="in_app_delivery_failed", message=str(exc))
        raise RetryableJobError(
            "The sales in-app notification could not be delivered.",
            code="in_app_delivery_failed",
        ) from exc

    _complete_delivery(delivery)
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.SALES_NOTIFIED,
        event_key="delivery:sales_in_app",
        data={"profile_id": str(profile.id)},
    )
    return _delivery_result(delivery)


def process_sales_feishu_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    intake, delivery = _load_delivery(
        payload,
        expected_kind=LeadDeliveryKind.SALES_FEISHU,
    )
    if delivery.status in (LeadDeliveryStatus.SENT, LeadDeliveryStatus.SKIPPED):
        return _delivery_result(delivery)
    restriction = intake_data_restriction(intake)
    if restriction:
        _skip_delivery(delivery, code=restriction.code)
        return _delivery_result(delivery)
    configuration = response_settings_for(intake.org_id)
    if not configuration.feishu_enabled or not configuration.feishu_webhook_ciphertext:
        _skip_delivery(delivery, code="feishu_disabled")
        return _delivery_result(delivery)

    try:
        webhook_url = validate_feishu_webhook_url(configuration.get_feishu_webhook())
    except ValueError as exc:
        _fail_delivery(delivery, code="invalid_feishu_webhook", message=str(exc))
        raise PermanentJobError(str(exc), code="invalid_feishu_webhook") from exc

    _start_delivery(delivery)
    try:
        response = requests.post(
            webhook_url,
            json={
                "msg_type": "text",
                "content": {"text": _sales_handoff_text(intake)},
            },
            timeout=settings.SDR_FEISHU_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableJobError(
                "Feishu temporarily rejected the sales notification.",
                code="feishu_temporarily_unavailable",
            )
        if response.status_code >= 400:
            raise PermanentJobError(
                "Feishu rejected the configured webhook request.",
                code="feishu_webhook_rejected",
            )
        try:
            response_body = response.json()
        except ValueError as exc:
            raise RetryableJobError(
                "Feishu returned an invalid response.",
                code="invalid_feishu_response",
            ) from exc
        if response_body.get("code", response_body.get("StatusCode", 0)) != 0:
            raise PermanentJobError(
                str(response_body.get("msg") or "Feishu rejected the message."),
                code="feishu_message_rejected",
            )
    except PermanentJobError as exc:
        _fail_delivery(delivery, code=exc.code, message=str(exc))
        raise
    except RetryableJobError as exc:
        _fail_delivery(delivery, code=exc.code, message=str(exc))
        raise
    except requests.RequestException as exc:
        _fail_delivery(delivery, code="feishu_network_error", message=str(exc))
        raise RetryableJobError(
            "The Feishu webhook could not be reached.",
            code="feishu_network_error",
        ) from exc

    _complete_delivery(delivery)
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.SALES_NOTIFIED,
        event_key="delivery:sales_feishu",
        data={"channel": "feishu"},
    )
    return _delivery_result(delivery)


def _load_delivery(payload: Mapping[str, Any], *, expected_kind: str):
    try:
        org_id = UUID(str(payload["org_id"]))
        intake_id = UUID(str(payload["intake_id"]))
        delivery_id = UUID(str(payload["delivery_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The SDR delivery payload is invalid.",
            code="invalid_delivery_payload",
        ) from exc
    delivery = (
        LeadDelivery.objects.filter(
            id=delivery_id,
            org_id=org_id,
            intake_id=intake_id,
            kind=expected_kind,
        )
        .select_related(
            "intake__org",
            "intake__crm_lead",
            "intake__assigned_profile__user",
        )
        .first()
    )
    if delivery is None:
        raise PermanentJobError(
            "The SDR delivery record no longer exists.",
            code="delivery_not_found",
        )
    return delivery.intake, delivery


def _start_delivery(delivery: LeadDelivery) -> None:
    LeadDelivery.objects.filter(id=delivery.id, org_id=delivery.org_id).update(
        status=LeadDeliveryStatus.SENDING,
        attempt_count=delivery.attempt_count + 1,
        last_error_code="",
        last_error_message="",
    )
    delivery.status = LeadDeliveryStatus.SENDING
    delivery.attempt_count += 1


def _complete_delivery(delivery: LeadDelivery) -> None:
    now = timezone.now()
    LeadDelivery.objects.filter(id=delivery.id, org_id=delivery.org_id).update(
        status=LeadDeliveryStatus.SENT,
        sent_at=now,
        last_error_code="",
        last_error_message="",
    )
    delivery.status = LeadDeliveryStatus.SENT
    delivery.sent_at = now


def _skip_delivery(delivery: LeadDelivery, *, code: str) -> None:
    LeadDelivery.objects.filter(id=delivery.id, org_id=delivery.org_id).update(
        status=LeadDeliveryStatus.SKIPPED,
        last_error_code=code[:80],
        last_error_message="",
    )
    delivery.status = LeadDeliveryStatus.SKIPPED
    delivery.last_error_code = code[:80]


def _fail_delivery(
    delivery: LeadDelivery,
    *,
    code: str,
    message: str,
) -> None:
    safe_message = (message or "Delivery failed.")[:2000]
    LeadDelivery.objects.filter(id=delivery.id, org_id=delivery.org_id).update(
        status=LeadDeliveryStatus.FAILED,
        last_error_code=code[:80],
        last_error_message=safe_message,
    )
    delivery.status = LeadDeliveryStatus.FAILED
    delivery.last_error_code = code[:80]
    delivery.last_error_message = safe_message


def _delivery_result(delivery: LeadDelivery) -> dict[str, Any]:
    return {
        "delivery_id": str(delivery.id),
        "status": delivery.status,
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
    }


def _render_message_template(value: str, context: Mapping[str, Any]) -> str:
    validate_message_template(value)
    return Template(value).render(Context(dict(context)))


def _lead_email(intake: LeadIntake) -> str:
    if intake.crm_lead and intake.crm_lead.email:
        return intake.crm_lead.email.strip().lower()
    return (
        str(
            intake.normalized_payload.get("identity", {}).get("email")
            or intake.raw_payload.get("email")
            or ""
        )
        .strip()
        .lower()
    )


def _company_name(intake: LeadIntake) -> str:
    if intake.crm_lead:
        return intake.crm_lead.company_name or intake.crm_lead.title or "Inbound lead"
    return str(
        intake.normalized_payload.get("company", {}).get("name")
        or intake.raw_payload.get("company_name")
        or "Inbound lead"
    )


def _template_context(intake: LeadIntake) -> dict[str, Any]:
    identity = intake.normalized_payload.get("identity", {})
    raw = intake.raw_payload
    organization_name = intake.org.company_name or intake.org.name or "our team"
    return {
        "first_name": identity.get("first_name") or raw.get("first_name") or "there",
        "last_name": identity.get("last_name") or raw.get("last_name") or "",
        "company_name": _company_name(intake),
        "organization_name": organization_name,
        "qualification_band": intake.qualification_band,
        "qualification_score": intake.qualification_score,
    }


def _crm_lead_path(intake: LeadIntake) -> str:
    return f"/leads/{intake.crm_lead_id}" if intake.crm_lead_id else "/leads"


def _sales_handoff_text(intake: LeadIntake) -> str:
    lead = intake.crm_lead
    assignee = intake.assigned_profile
    assignee_name = (
        assignee.user.name or assignee.user.email
        if assignee and assignee.user
        else "Sales"
    )
    contact_name = (
        " ".join(
            filter(
                None,
                [
                    getattr(lead, "first_name", ""),
                    getattr(lead, "last_name", ""),
                ],
            )
        )
        or "Unknown contact"
    )
    research_summary = ""
    try:
        research_summary = intake.inspection.research_summary
    except LeadIntake.inspection.RelatedObjectDoesNotExist:
        pass
    lead_url = (
        f"{settings.FRONTEND_URL.rstrip('/')}{_crm_lead_path(intake)}"
        if intake.crm_lead_id
        else settings.FRONTEND_URL.rstrip("/")
    )
    parts = [
        "New SDR lead assigned",
        f"Sales owner: {assignee_name}",
        f"Contact: {contact_name}",
        f"Company: {_company_name(intake)}",
        f"Score: {intake.qualification_score or 0} ({intake.qualification_band or 'unknown'})",
        f"Source: {intake.source}",
    ]
    if research_summary:
        parts.append(f"Research: {research_summary[:1000]}")
    if intake.routing_reason:
        parts.append(f"Routing: {intake.routing_reason[:500]}")
    parts.append(f"CRM: {lead_url}")
    return "\n".join(parts)[:3500]
