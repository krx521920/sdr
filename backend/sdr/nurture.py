"""Durable, tenant-scoped SDR nurture sequence execution."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import transaction
from django.template import Context, Template, TemplateSyntaxError
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.jobs import JobRequest
from automation.models import AutomationJobStatus
from automation.services import dispatch_job, enqueue_job
from sdr.compliance import evaluate_contact
from sdr.email_execution import (
    EmailExecutionIntent,
    bound_email_execution,
    claim_email_execution,
    email_execution_request_id,
    email_send_intent,
    release_reserved_email_execution,
    settle_email_delivered,
    settle_email_provider_accepted,
    settle_email_provider_unknown,
)
from sdr.email_safety import (
    CAMPAIGN_SAFETY_PAUSE_REASON,
    reserve_delivery_send,
)
from sdr.models import (
    LeadIntake,
    LeadIntakeSource,
    LeadIntakeStatus,
    LeadLifecycleEventType,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    NurtureReplySentiment,
    SDRNurtureSequence,
    SDRNurtureStep,
)
from sdr.provider_ports import ExecutionSafetyError, ExternalRequestStatus
from sdr.response import record_lifecycle_event, validate_message_template
from sdr.suppression import unsubscribe_url
from sdr.tracking import build_tracked_email_content

logger = logging.getLogger(__name__)

NURTURE_EMAIL_JOB = "sdr.send_nurture_email"
TERMINAL_ENROLLMENT_STATUSES = frozenset(
    {
        NurtureEnrollmentStatus.COMPLETED,
        NurtureEnrollmentStatus.CANCELLED,
        NurtureEnrollmentStatus.REPLIED,
        NurtureEnrollmentStatus.CONVERTED,
    }
)
STOPPED_LEAD_STATUSES = frozenset({"converted", "closed"})


def auto_enroll_intake(intake: LeadIntake) -> LeadNurtureEnrollment | None:
    """Enroll a completed intake in the first matching active sequence."""

    email = _lead_email(intake)
    if intake.status != LeadIntakeStatus.COMPLETED or not email:
        return None
    # Outbound contact is always an explicit campaign decision.  An empty or
    # broadly matching auto-enroll rule must never contact an imported list.
    if intake.source == LeadIntakeSource.OUTBOUND:
        return None
    existing = LeadNurtureEnrollment.objects.filter(
        org_id=intake.org_id,
        intake=intake,
    ).first()
    if existing:
        if _email_not_permitted(
            intake, email, event_key="nurture:auto-enroll:existing"
        ):
            if existing.status not in TERMINAL_ENROLLMENT_STATUSES:
                stop_enrollment(
                    existing,
                    status=NurtureEnrollmentStatus.CANCELLED,
                    reason="The email address is on the tenant suppression list.",
                )
            return None
        ensure_enrollment_schedule(existing)
        return existing

    if _email_not_permitted(intake, email, event_key="nurture:auto-enroll"):
        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.NURTURE_SUPPRESSED,
            event_key="nurture:suppressed:auto-enroll",
            data={"reason": "active_email_suppression"},
        )
        return None

    sequences = (
        SDRNurtureSequence.objects.filter(
            org_id=intake.org_id,
            is_active=True,
            auto_enroll=True,
        )
        .prefetch_related("steps")
        .order_by("priority", "created_at", "id")
    )
    sequence = next(
        (
            candidate
            for candidate in sequences
            if candidate.steps.all()
            and _matches_intake_source(candidate.sources, intake.source)
            and _matches(candidate.qualification_bands, intake.qualification_band)
        ),
        None,
    )
    if sequence is None:
        return None

    with transaction.atomic():
        locked_intake = LeadIntake.objects.select_for_update().get(
            id=intake.id,
            org_id=intake.org_id,
        )
        enrollment, created = LeadNurtureEnrollment.objects.get_or_create(
            org_id=intake.org_id,
            intake=locked_intake,
            defaults={
                "sequence": sequence,
                "lead_id": intake.crm_lead_id,
            },
        )
    if created:
        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.NURTURE_ENROLLED,
            event_key="nurture:enrolled",
            data={"sequence_id": str(sequence.id), "sequence_name": sequence.name},
        )
    ensure_enrollment_schedule(enrollment)
    return enrollment


def enroll_intake_in_sequence(
    intake: LeadIntake,
    sequence: SDRNurtureSequence,
) -> LeadNurtureEnrollment | None:
    """Explicitly enroll one completed intake in a tenant-owned sequence."""

    email = _lead_email(intake)
    if intake.status != LeadIntakeStatus.COMPLETED or not email:
        return None
    if sequence.org_id != intake.org_id:
        raise ValueError("The nurture sequence belongs to another organization.")
    if not sequence.is_active or not sequence.steps.exists():
        raise ValueError("The nurture sequence must be enabled and contain a step.")

    existing = LeadNurtureEnrollment.objects.filter(
        org_id=intake.org_id,
        intake=intake,
    ).first()
    if existing:
        if existing.sequence_id != sequence.id:
            raise ValueError("The intake is already enrolled in another sequence.")
        ensure_enrollment_schedule(existing)
        return existing

    if _email_not_permitted(intake, email, event_key="nurture:explicit-enroll"):
        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.NURTURE_SUPPRESSED,
            event_key="nurture:suppressed:explicit-enroll",
            data={"reason": "active_email_suppression"},
        )
        return None

    with transaction.atomic():
        locked_intake = LeadIntake.objects.select_for_update().get(
            id=intake.id,
            org_id=intake.org_id,
        )
        enrollment, created = LeadNurtureEnrollment.objects.get_or_create(
            org_id=intake.org_id,
            intake=locked_intake,
            defaults={
                "sequence": sequence,
                "lead_id": intake.crm_lead_id,
            },
        )
    if created:
        record_lifecycle_event(
            intake=intake,
            event_type=LeadLifecycleEventType.NURTURE_ENROLLED,
            event_key="nurture:enrolled",
            data={"sequence_id": str(sequence.id), "sequence_name": sequence.name},
        )
    ensure_enrollment_schedule(enrollment)
    return enrollment


def ensure_enrollment_schedule(
    enrollment: LeadNurtureEnrollment,
) -> LeadNurtureDelivery | None:
    """Ensure an active enrollment has one durable next-step delivery."""

    enrollment = (
        LeadNurtureEnrollment.objects.select_related(
            "sequence",
            "intake__crm_lead",
            "intake__org",
            "lead",
        )
        .prefetch_related("sequence__steps", "deliveries")
        .get(id=enrollment.id, org_id=enrollment.org_id)
    )
    if enrollment.status != NurtureEnrollmentStatus.ACTIVE:
        return None
    recipient = _lead_email(enrollment.intake)
    if _email_not_permitted(
        enrollment.intake,
        recipient,
        event_key=f"nurture:schedule:{enrollment.id}",
    ):
        stop_enrollment(
            enrollment,
            status=NurtureEnrollmentStatus.CANCELLED,
            reason="The email address is on the tenant suppression list.",
        )
        return None
    stopped_status = _lead_stop_status(enrollment)
    if stopped_status:
        stop_enrollment(
            enrollment,
            status=stopped_status,
            reason="The CRM lead is no longer eligible for nurture.",
        )
        return None
    if not enrollment.sequence.is_active:
        pause_enrollment(enrollment, reason="The nurture sequence is disabled.")
        return None

    outstanding = next(
        (
            delivery
            for delivery in enrollment.deliveries.all()
            if delivery.step_position > enrollment.current_step_position
            and delivery.status
            in {
                NurtureDeliveryStatus.PENDING,
                NurtureDeliveryStatus.SENDING,
                NurtureDeliveryStatus.FAILED,
            }
        ),
        None,
    )
    if outstanding:
        _enqueue_delivery(outstanding)
        return outstanding

    delivered_positions = {
        item.step_position
        for item in enrollment.deliveries.all()
        if item.status in {NurtureDeliveryStatus.SENT, NurtureDeliveryStatus.SKIPPED}
    }
    next_step = next(
        (
            step
            for step in enrollment.sequence.steps.all()
            if step.position not in delivered_positions
            and step.position > enrollment.current_step_position
        ),
        None,
    )
    if next_step is None:
        _complete_enrollment(enrollment)
        return None

    previous = max(
        (
            item
            for item in enrollment.deliveries.all()
            if item.status == NurtureDeliveryStatus.SENT and item.sent_at
        ),
        key=lambda item: item.step_position,
        default=None,
    )
    base_time = previous.sent_at if previous else enrollment.enrolled_at
    scheduled_for = base_time + timedelta(minutes=next_step.delay_minutes)
    delivery = _create_delivery(
        enrollment=enrollment,
        step=next_step,
        scheduled_for=scheduled_for,
    )
    _enqueue_delivery(delivery)
    return delivery


def process_nurture_email_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    delivery = _load_delivery(payload)
    enrollment = delivery.enrollment
    try:
        execution_request_id = email_execution_request_id(payload)
        execution_request = bound_email_execution(
            org=enrollment.intake.org,
            delivery_id=delivery.id,
            request_id=execution_request_id,
        )
    except ExecutionSafetyError as exc:
        _fail_delivery(delivery, code=exc.code, message=exc.detail)
        raise PermanentJobError(exc.detail, code=exc.code) from exc

    if execution_request is not None and execution_request.status in {
        ExternalRequestStatus.ACCEPTED,
        ExternalRequestStatus.DELIVERED,
    }:
        return _recover_accepted_nurture_email(delivery, execution_request)
    if execution_request is not None and execution_request.status != (
        ExternalRequestStatus.RESERVED
    ):
        if execution_request.status not in {
            ExternalRequestStatus.SENDING,
            ExternalRequestStatus.UNKNOWN,
        }:
            _fail_delivery(
                delivery,
                code="email_execution_not_replayable",
                message="The approved email execution was already attempted.",
            )
        raise PermanentJobError(
            "The approved email execution cannot be replayed.",
            code="email_execution_not_replayable",
        )

    if delivery.status == NurtureDeliveryStatus.SENT:
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="email_delivery_already_sent",
        )
        ensure_enrollment_schedule(enrollment)
        return _delivery_result(delivery)
    if delivery.status == NurtureDeliveryStatus.SKIPPED:
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="email_delivery_already_skipped",
        )
        return _delivery_result(delivery)
    if enrollment.status in TERMINAL_ENROLLMENT_STATUSES:
        _skip_delivery(delivery, code=f"enrollment_{enrollment.status}")
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code=f"enrollment_{enrollment.status}",
        )
        return _delivery_result(delivery)
    if _email_not_permitted(
        enrollment.intake,
        delivery.recipient,
        event_key=f"nurture:send:{delivery.id}",
    ):
        stop_enrollment(
            enrollment,
            status=NurtureEnrollmentStatus.CANCELLED,
            reason="The email address is on the tenant suppression list.",
        )
        _skip_delivery(delivery, code="email_suppressed")
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="email_suppressed",
        )
        return _delivery_result(delivery)
    if enrollment.status == NurtureEnrollmentStatus.PAUSED:
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="enrollment_paused",
        )
        return {**_delivery_result(delivery), "paused": True}

    stopped_status = _lead_stop_status(enrollment)
    if stopped_status:
        stop_enrollment(
            enrollment,
            status=stopped_status,
            reason="The CRM lead is no longer eligible for nurture.",
        )
        _skip_delivery(delivery, code=f"lead_{stopped_status}")
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code=f"lead_{stopped_status}",
        )
        return _delivery_result(delivery)
    if not enrollment.sequence.is_active:
        pause_enrollment(enrollment, reason="The nurture sequence is disabled.")
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="nurture_sequence_disabled",
        )
        return {**_delivery_result(delivery), "paused": True}

    try:
        validate_email(delivery.recipient)
        validate_message_template(delivery.subject_template)
        validate_message_template(delivery.body_template)
    except (ValidationError, ValueError) as exc:
        _fail_delivery(delivery, code="invalid_nurture_message", message=str(exc))
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="invalid_nurture_message",
        )
        raise PermanentJobError(str(exc), code="invalid_nurture_message") from exc

    try:
        safety = reserve_delivery_send(delivery)
    except Exception as exc:
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="email_safety_check_failed",
        )
        _fail_delivery(
            delivery,
            code="email_safety_check_failed",
            message="The nurture email safety check could not be completed.",
        )
        raise PermanentJobError(
            "The nurture email safety check could not be completed.",
            code="email_safety_check_failed",
        ) from exc
    if not safety.allowed:
        if safety.reason == "campaign_safety_hold":
            pause_enrollment(
                enrollment,
                reason=CAMPAIGN_SAFETY_PAUSE_REASON,
            )
            release_reserved_email_execution(
                org=enrollment.intake.org,
                request=execution_request,
                error_code="campaign_safety_hold",
            )
            return {
                **_delivery_result(delivery),
                "paused": True,
                "safety_reason": safety.reason,
            }
        try:
            _enqueue_delivery(
                delivery,
                execution_request_id=(
                    execution_request.id if execution_request is not None else None
                ),
            )
        except Exception as exc:
            release_reserved_email_execution(
                org=enrollment.intake.org,
                request=execution_request,
                error_code="email_local_defer_failed",
            )
            _fail_delivery(
                delivery,
                code="email_local_defer_failed",
                message="The deferred email could not be queued safely.",
            )
            raise PermanentJobError(
                "The deferred email could not be queued safely.",
                code="email_local_defer_failed",
            ) from exc
        return {
            **_delivery_result(delivery),
            "deferred": True,
            "safety_reason": safety.reason,
            "next_attempt_at": (
                safety.next_attempt_at.isoformat() if safety.next_attempt_at else None
            ),
            "used_today": safety.used_today,
            "daily_limit": safety.daily_limit,
        }
    try:
        email, intent = _build_nurture_email(delivery)
        sending_request = claim_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            intent=intent,
        )
    except ExecutionSafetyError as exc:
        if execution_request is not None:
            current_request = bound_email_execution(
                org=enrollment.intake.org,
                delivery_id=delivery.id,
                request_id=execution_request.id,
            )
            if current_request.status in {
                ExternalRequestStatus.ACCEPTED,
                ExternalRequestStatus.DELIVERED,
            }:
                return _recover_accepted_nurture_email(
                    delivery,
                    current_request,
                )
            if current_request.status in {
                ExternalRequestStatus.SENDING,
                ExternalRequestStatus.UNKNOWN,
            }:
                raise PermanentJobError(
                    "The approved email execution cannot be replayed.",
                    code="email_execution_not_replayable",
                ) from exc
        _fail_delivery(delivery, code=exc.code, message=exc.detail)
        raise PermanentJobError(exc.detail, code=exc.code) from exc
    except (TemplateSyntaxError, ValueError) as exc:
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="invalid_nurture_message",
        )
        _fail_delivery(delivery, code="invalid_nurture_message", message=str(exc))
        raise PermanentJobError(str(exc), code="invalid_nurture_message") from exc
    except Exception as exc:
        release_reserved_email_execution(
            org=enrollment.intake.org,
            request=execution_request,
            error_code="email_local_start_failed",
        )
        _fail_delivery(
            delivery,
            code="email_local_start_failed",
            message="The nurture email could not be prepared safely.",
        )
        raise PermanentJobError(
            "The nurture email could not be prepared safely.",
            code="email_local_start_failed",
        ) from exc

    try:
        sent = email.send(fail_silently=False)
        if sent != 1:
            raise RuntimeError("The email backend did not accept the message.")
        accepted_request = settle_email_provider_accepted(
            org=enrollment.intake.org,
            request=sending_request,
        )
    except Exception as exc:
        try:
            settle_email_provider_unknown(
                org=enrollment.intake.org,
                request=sending_request,
            )
        except Exception:
            logger.exception(
                "Could not settle uncertain nurture email execution %s",
                getattr(sending_request, "id", None),
            )
        _fail_delivery(
            delivery,
            code="email_execution_outcome_unknown",
            message="The email provider outcome is unknown; automatic replay is disabled.",
        )
        raise PermanentJobError(
            "The email provider outcome is unknown; automatic replay is disabled.",
            code="email_execution_outcome_unknown",
        ) from exc

    return _finish_accepted_nurture_email(
        delivery,
        accepted_request,
        provider_message_id=str(email.extra_headers.get("message_id") or ""),
    )


def nurture_email_execution_intent(
    delivery: LeadNurtureDelivery,
) -> EmailExecutionIntent:
    """Return the exact immutable snapshot an administrator must approve."""

    _, intent = _build_nurture_email(delivery)
    return intent


def enqueue_approved_nurture_delivery(
    delivery: LeadNurtureDelivery,
    *,
    execution_request_id: UUID,
):
    """Queue one approved provider attempt with retries disabled."""

    bound_email_execution(
        org=delivery.enrollment.intake.org,
        delivery_id=delivery.id,
        request_id=execution_request_id,
    )
    return _enqueue_delivery(
        delivery,
        execution_request_id=execution_request_id,
    )


def _build_nurture_email(
    delivery: LeadNurtureDelivery,
) -> tuple[EmailMultiAlternatives, EmailExecutionIntent]:
    enrollment = delivery.enrollment
    validate_email(delivery.recipient)
    validate_message_template(delivery.subject_template)
    validate_message_template(delivery.body_template)
    context = _template_context(enrollment.intake)
    subject = Template(delivery.subject_template).render(Context(context)).strip()[:255]
    body = Template(delivery.body_template).render(Context(context))
    opt_out_url = unsubscribe_url(delivery)
    tracked_body, html_body = build_tracked_email_content(
        delivery,
        body,
        unsubscribe=opt_out_url,
    )
    from_email = enrollment.sequence.from_email or settings.DEFAULT_FROM_EMAIL
    headers = {
        "List-Unsubscribe": f"<{opt_out_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-SES-MESSAGE-TAGS": (f"sdr_org={delivery.org_id},sdr_delivery={delivery.id}"),
    }
    email = EmailMultiAlternatives(
        subject=subject,
        body=tracked_body,
        from_email=from_email,
        to=[delivery.recipient],
        headers=headers,
    )
    email.attach_alternative(html_body, "text/html")
    intent = email_send_intent(
        org=enrollment.intake.org,
        recipient=delivery.recipient,
        subject=subject,
        text_body=tracked_body,
        html_body=html_body,
        from_email=from_email,
        headers=headers,
    )
    return email, intent


def _recover_accepted_nurture_email(delivery, execution_request):
    return _finish_accepted_nurture_email(
        delivery,
        execution_request,
        provider_message_id=delivery.provider_message_id,
    )


def _finish_accepted_nurture_email(
    delivery: LeadNurtureDelivery,
    execution_request,
    *,
    provider_message_id: str,
) -> Mapping[str, Any]:
    try:
        _finalize_nurture_email_delivery(
            delivery,
            provider_message_id=provider_message_id,
        )
        settle_email_delivered(
            org=delivery.enrollment.intake.org,
            request=execution_request,
        )
    except Exception as exc:
        _fail_delivery(
            delivery,
            code="email_local_state_incomplete",
            message=(
                "The provider accepted the email, but local completion is incomplete; "
                "automatic provider replay is disabled."
            ),
        )
        raise PermanentJobError(
            "The provider accepted the email, but local completion is incomplete.",
            code="email_local_state_incomplete",
        ) from exc
    return _delivery_result(delivery)


def _finalize_nurture_email_delivery(
    delivery: LeadNurtureDelivery,
    *,
    provider_message_id: str = "",
) -> None:
    with transaction.atomic():
        # Lock only the delivery row here.  PostgreSQL rejects FOR UPDATE when
        # Django expands nullable ``select_related`` paths (step, lead, or
        # crm_lead) into outer joins.  Load the related rows separately after
        # the delivery claim so the provider-accepted result can always be
        # converged locally without weakening the row lock.
        locked_delivery = LeadNurtureDelivery.objects.select_for_update().get(
            id=delivery.id,
            org_id=delivery.org_id,
        )
        locked_enrollment = LeadNurtureEnrollment.objects.select_for_update().get(
            id=locked_delivery.enrollment_id,
            org_id=delivery.org_id,
        )
        locked_intake = locked_enrollment.intake
        if locked_delivery.status != NurtureDeliveryStatus.SENT:
            _complete_delivery(
                locked_delivery,
                provider_message_id=provider_message_id,
            )
        next_position = max(
            locked_enrollment.current_step_position,
            locked_delivery.step_position,
        )
        LeadNurtureEnrollment.objects.filter(
            id=locked_enrollment.id,
            org_id=locked_enrollment.org_id,
        ).update(
            current_step_position=next_position,
            next_run_at=None,
            stop_reason="",
        )
        locked_enrollment.current_step_position = next_position
        locked_enrollment.next_run_at = None
        locked_enrollment.stop_reason = ""
        if locked_enrollment.lead_id:
            locked_enrollment.lead.last_contacted = timezone.localdate()
            locked_enrollment.lead.save(update_fields=["last_contacted", "updated_at"])
        record_lifecycle_event(
            intake=locked_intake,
            event_type=LeadLifecycleEventType.NURTURE_EMAIL_SENT,
            event_key=f"nurture:step:{locked_delivery.step_position}",
            data={
                "sequence_id": str(locked_enrollment.sequence_id),
                "step": locked_delivery.step_position,
                "variant": locked_delivery.variant,
            },
        )
        delivery.status = locked_delivery.status
        delivery.sent_at = locked_delivery.sent_at
        delivery.provider_message_id = locked_delivery.provider_message_id
        enrollment = locked_enrollment
        enrollment.current_step_position = next_position
        enrollment.next_run_at = None
        enrollment.stop_reason = ""
    ensure_enrollment_schedule(enrollment)


def pause_enrollment(
    enrollment: LeadNurtureEnrollment,
    *,
    reason: str = "Paused by a user.",
) -> LeadNurtureEnrollment:
    if enrollment.status in TERMINAL_ENROLLMENT_STATUSES:
        return enrollment
    LeadNurtureEnrollment.objects.filter(
        id=enrollment.id,
        org_id=enrollment.org_id,
    ).update(
        status=NurtureEnrollmentStatus.PAUSED,
        stop_reason=reason[:500],
        next_run_at=None,
    )
    enrollment.status = NurtureEnrollmentStatus.PAUSED
    enrollment.stop_reason = reason[:500]
    enrollment.next_run_at = None
    _record_stop_event(enrollment, "paused")
    return enrollment


def resume_enrollment(enrollment: LeadNurtureEnrollment) -> LeadNurtureEnrollment:
    if enrollment.status != NurtureEnrollmentStatus.PAUSED:
        return enrollment
    LeadNurtureEnrollment.objects.filter(
        id=enrollment.id,
        org_id=enrollment.org_id,
    ).update(
        status=NurtureEnrollmentStatus.ACTIVE,
        stop_reason="",
        resume_count=enrollment.resume_count + 1,
    )
    enrollment.status = NurtureEnrollmentStatus.ACTIVE
    enrollment.stop_reason = ""
    enrollment.resume_count += 1
    pending = (
        enrollment.deliveries.filter(
            status__in=(NurtureDeliveryStatus.PENDING, NurtureDeliveryStatus.FAILED)
        )
        .order_by("step_position")
        .first()
    )
    if pending:
        if pending.scheduled_for < timezone.now():
            pending.scheduled_for = timezone.now()
            pending.status = NurtureDeliveryStatus.PENDING
            pending.save(update_fields=["scheduled_for", "status", "updated_at"])
        _enqueue_delivery(pending)
    else:
        ensure_enrollment_schedule(enrollment)
    return enrollment


def stop_enrollment(
    enrollment: LeadNurtureEnrollment,
    *,
    status: str,
    reason: str,
    reply_sentiment: str = "",
) -> LeadNurtureEnrollment:
    if status not in TERMINAL_ENROLLMENT_STATUSES - {NurtureEnrollmentStatus.COMPLETED}:
        raise ValueError("Unsupported nurture stop status.")
    now = timezone.now()
    LeadNurtureEnrollment.objects.filter(
        id=enrollment.id,
        org_id=enrollment.org_id,
    ).update(
        status=status,
        stop_reason=reason[:500],
        next_run_at=None,
        completed_at=now,
    )
    enrollment.status = status
    enrollment.stop_reason = reason[:500]
    enrollment.next_run_at = None
    enrollment.completed_at = now
    if status == NurtureEnrollmentStatus.REPLIED:
        latest = (
            enrollment.deliveries.filter(status=NurtureDeliveryStatus.SENT)
            .order_by("-sent_at")
            .first()
        )
        if latest:
            sentiment = (
                reply_sentiment
                if reply_sentiment in NurtureReplySentiment.values
                else NurtureReplySentiment.NEUTRAL
            )
            latest.replied_at = now
            latest.reply_sentiment = sentiment
            latest.save(update_fields=["replied_at", "reply_sentiment", "updated_at"])
    _record_stop_event(enrollment, status)
    return enrollment


def reconcile_nurture_jobs(*, org_id: UUID, limit: int = 100) -> int:
    """Recover enrollment or next-step scheduling gaps after interruptions."""

    touched = 0
    cutoff = timezone.now() - timedelta(days=1)
    intakes = LeadIntake.objects.filter(
        org_id=org_id,
        status=LeadIntakeStatus.COMPLETED,
        processed_at__gte=cutoff,
    ).select_related("crm_lead", "org")[:limit]
    for intake in intakes:
        if auto_enroll_intake(intake):
            touched += 1
    enrollments = LeadNurtureEnrollment.objects.filter(
        org_id=org_id,
        status=NurtureEnrollmentStatus.ACTIVE,
    ).select_related("sequence", "intake__crm_lead", "lead")[:limit]
    for enrollment in enrollments:
        if ensure_enrollment_schedule(enrollment):
            touched += 1
    return touched


def _create_delivery(
    *,
    enrollment: LeadNurtureEnrollment,
    step: SDRNurtureStep,
    scheduled_for,
) -> LeadNurtureDelivery:
    variant = _choose_variant(enrollment, step)
    subject = step.subject_b if variant == "B" else step.subject_a
    body = step.body_b if variant == "B" else step.body_a
    delivery, _ = LeadNurtureDelivery.objects.get_or_create(
        org_id=enrollment.org_id,
        enrollment=enrollment,
        step_position=step.position,
        defaults={
            "step": step,
            "variant": variant,
            "recipient": _lead_email(enrollment.intake),
            "subject_template": subject,
            "body_template": body,
            "scheduled_for": scheduled_for,
        },
    )
    LeadNurtureEnrollment.objects.filter(
        id=enrollment.id,
        org_id=enrollment.org_id,
    ).update(next_run_at=delivery.scheduled_for)
    enrollment.next_run_at = delivery.scheduled_for
    return delivery


def _enqueue_delivery(
    delivery: LeadNurtureDelivery,
    *,
    execution_request_id: UUID | None = None,
):
    if execution_request_id is None and not getattr(
        settings, "ALLOW_UNGUARDED_PROVIDER_IO", False
    ):
        # In production the durable delivery is the first-stage review item.
        # Do not manufacture a job that must fail closed before an
        # administrator reserves its exact email execution intent.
        return None
    enrollment = delivery.enrollment
    execution_suffix = (
        f":execution:{execution_request_id}" if execution_request_id is not None else ""
    )
    job_payload = {
        "org_id": str(delivery.org_id),
        "delivery_id": str(delivery.id),
    }
    if execution_request_id is not None:
        job_payload["execution_request_id"] = str(execution_request_id)
    enqueued = enqueue_job(
        JobRequest(
            org_id=delivery.org_id,
            name=NURTURE_EMAIL_JOB,
            idempotency_key=(
                f"nurture-delivery:{delivery.id}:defer:{delivery.deferral_count}:"
                f"dispatch:{enrollment.resume_count}{execution_suffix}"
            ),
            payload=job_payload,
            scheduled_for=delivery.scheduled_for,
            max_attempts=1 if execution_request_id is not None else 5,
        )
    )
    if (
        enqueued.job.status
        in {AutomationJobStatus.PENDING, AutomationJobStatus.RETRY_SCHEDULED}
        and enqueued.job.scheduled_for <= timezone.now()
    ):
        try:
            dispatch_job(enqueued.job)
        except Exception:
            logger.exception("Could not dispatch nurture delivery %s", delivery.id)
    return enqueued.job


def _load_delivery(payload: Mapping[str, Any]) -> LeadNurtureDelivery:
    try:
        org_id = UUID(str(payload["org_id"]))
        delivery_id = UUID(str(payload["delivery_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "The nurture delivery payload is invalid.",
            code="invalid_nurture_payload",
        ) from exc
    delivery = (
        LeadNurtureDelivery.objects.filter(id=delivery_id, org_id=org_id)
        .select_related(
            "enrollment__sequence",
            "enrollment__intake__crm_lead",
            "enrollment__intake__org",
            "enrollment__lead",
        )
        .first()
    )
    if delivery is None:
        raise PermanentJobError(
            "The nurture delivery no longer exists.",
            code="nurture_delivery_not_found",
        )
    return delivery


def _start_delivery(delivery: LeadNurtureDelivery) -> None:
    LeadNurtureDelivery.objects.filter(
        id=delivery.id,
        org_id=delivery.org_id,
    ).update(
        status=NurtureDeliveryStatus.SENDING,
        attempt_count=delivery.attempt_count + 1,
        last_error_code="",
        last_error_message="",
    )
    delivery.status = NurtureDeliveryStatus.SENDING
    delivery.attempt_count += 1


def _complete_delivery(
    delivery: LeadNurtureDelivery,
    *,
    provider_message_id: str = "",
) -> None:
    now = timezone.now()
    LeadNurtureDelivery.objects.filter(
        id=delivery.id,
        org_id=delivery.org_id,
    ).update(
        status=NurtureDeliveryStatus.SENT,
        sent_at=now,
        provider_message_id=provider_message_id[:255],
        last_error_code="",
        last_error_message="",
    )
    delivery.status = NurtureDeliveryStatus.SENT
    delivery.sent_at = now
    delivery.provider_message_id = provider_message_id[:255]


def _fail_delivery(
    delivery: LeadNurtureDelivery,
    *,
    code: str,
    message: str,
) -> None:
    LeadNurtureDelivery.objects.filter(
        id=delivery.id,
        org_id=delivery.org_id,
    ).update(
        status=NurtureDeliveryStatus.FAILED,
        last_error_code=code[:80],
        last_error_message=(message or "Delivery failed.")[:2000],
    )
    delivery.status = NurtureDeliveryStatus.FAILED


def _skip_delivery(delivery: LeadNurtureDelivery, *, code: str) -> None:
    LeadNurtureDelivery.objects.filter(
        id=delivery.id,
        org_id=delivery.org_id,
    ).update(
        status=NurtureDeliveryStatus.SKIPPED,
        last_error_code=code[:80],
        last_error_message="",
    )
    delivery.status = NurtureDeliveryStatus.SKIPPED


def _complete_enrollment(enrollment: LeadNurtureEnrollment) -> None:
    if enrollment.status != NurtureEnrollmentStatus.ACTIVE:
        return
    now = timezone.now()
    LeadNurtureEnrollment.objects.filter(
        id=enrollment.id,
        org_id=enrollment.org_id,
    ).update(
        status=NurtureEnrollmentStatus.COMPLETED,
        next_run_at=None,
        completed_at=now,
        stop_reason="Sequence completed.",
    )
    enrollment.status = NurtureEnrollmentStatus.COMPLETED
    enrollment.completed_at = now
    enrollment.next_run_at = None


def _record_stop_event(enrollment: LeadNurtureEnrollment, status: str) -> None:
    record_lifecycle_event(
        intake=enrollment.intake,
        event_type=LeadLifecycleEventType.NURTURE_STOPPED,
        event_key=f"nurture:stopped:{status}",
        data={"sequence_id": str(enrollment.sequence_id), "status": status},
    )


def _lead_stop_status(enrollment: LeadNurtureEnrollment) -> str:
    lead = enrollment.lead or enrollment.intake.crm_lead
    if lead is None or not lead.is_active:
        return NurtureEnrollmentStatus.CANCELLED
    if (lead.status or "").lower() in STOPPED_LEAD_STATUSES:
        return NurtureEnrollmentStatus.CONVERTED
    return ""


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


def _email_not_permitted(
    intake: LeadIntake,
    email: str,
    *,
    event_key: str,
) -> bool:
    return not evaluate_contact(
        org_id=intake.org_id,
        channel="email",
        identifier=email,
        intake=intake,
        event_key=event_key,
    ).allowed


def _template_context(intake: LeadIntake) -> dict[str, Any]:
    identity = intake.normalized_payload.get("identity", {})
    raw = intake.raw_payload
    lead = intake.crm_lead
    return {
        "first_name": (
            getattr(lead, "first_name", "")
            or identity.get("first_name")
            or raw.get("first_name")
            or "there"
        ),
        "last_name": (
            getattr(lead, "last_name", "")
            or identity.get("last_name")
            or raw.get("last_name")
            or ""
        ),
        "company_name": (
            getattr(lead, "company_name", "")
            or intake.normalized_payload.get("company", {}).get("name")
            or raw.get("company_name")
            or "your team"
        ),
        "organization_name": intake.org.company_name or intake.org.name or "our team",
        "qualification_band": intake.qualification_band,
        "qualification_score": intake.qualification_score,
    }


def _choose_variant(
    enrollment: LeadNurtureEnrollment,
    step: SDRNurtureStep,
) -> str:
    if not step.variant_b_percent or not step.subject_b or not step.body_b:
        return "A"
    digest = hashlib.sha256(f"{enrollment.id}:{step.position}".encode()).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    return "B" if bucket < step.variant_b_percent else "A"


def _matches(configured_values: list[str], actual: str) -> bool:
    return not configured_values or actual in configured_values


def _matches_intake_source(configured_values: list[str], actual: str) -> bool:
    if actual == LeadIntakeSource.OUTBOUND:
        return actual in configured_values
    return _matches(configured_values, actual)


def _delivery_result(delivery: LeadNurtureDelivery) -> dict[str, Any]:
    return {
        "delivery_id": str(delivery.id),
        "status": delivery.status,
        "step": delivery.step_position,
        "variant": delivery.variant,
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
    }
