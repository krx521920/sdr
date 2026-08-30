"""Durable Facebook Messenger intake and conversation handling."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.template import Context, Template, TemplateSyntaxError
from django.utils import timezone

from automation.errors import PermanentJobError, RetryableJobError
from automation.jobs import JobRequest
from automation.services import dispatch_job, enqueue_job
from automation.tenant_context import database_org_context
from integrations.models import (
    FacebookMessengerMessage,
    FacebookMessengerMessageStatus,
    FacebookMessengerReply,
    FacebookMessengerReplyKind,
    FacebookMessengerReplyStatus,
    FacebookPageConnection,
    FacebookPageRoute,
)
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.service import graph_client
from leads.models import Lead
from sdr.compliance import intake_data_restriction
from sdr.domain import CompanySnapshot, LeadCandidate, LeadIdentity, LeadSource
from sdr.models import (
    LeadIntake,
    LeadIntakeStatus,
    LeadLifecycleEvent,
    LeadLifecycleEventType,
)
from sdr.response import record_lifecycle_event
from sdr.services import (
    IntakeAlreadyProcessing,
    IntakeProcessingFailed,
    process_candidate_intake,
)

logger = logging.getLogger(__name__)

FACEBOOK_MESSENGER_JOB = "facebook.process_messenger_message"
FACEBOOK_MESSENGER_REPLY_JOB = "facebook.send_messenger_reply"
AUTO_REPLY_TEMPLATE_VARIABLES = frozenset({"organization_name", "page_name"})
AUTO_REPLY_TEMPLATE_VARIABLE_PATTERN = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*}}")


class FacebookMessengerUnavailable(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class FacebookMessengerAcceptance:
    message_id: UUID
    job_id: UUID
    replayed: bool


class FacebookMessengerReplyUnavailable(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FacebookMessengerReplyAcceptance:
    reply_id: UUID
    job_id: UUID
    replayed: bool


def validate_auto_reply_template(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Messenger auto-reply text is required.")
    if len(cleaned) > 2000:
        raise ValueError("Messenger auto-reply text cannot exceed 2,000 characters.")
    if "{%" in cleaned or "{#" in cleaned:
        raise ValueError("Template tags and comments are not allowed.")
    for variable in AUTO_REPLY_TEMPLATE_VARIABLE_PATTERN.findall(cleaned):
        if variable not in AUTO_REPLY_TEMPLATE_VARIABLES:
            raise ValueError(f'Unknown template variable: "{variable}".')
    remainder = AUTO_REPLY_TEMPLATE_VARIABLE_PATTERN.sub("", cleaned)
    if "{{" in remainder or "}}" in remainder:
        raise ValueError("Use only the documented simple template variables.")
    try:
        Template(cleaned)
    except TemplateSyntaxError as exc:
        raise ValueError(f"Invalid Messenger auto-reply template: {exc}") from exc
    return cleaned


def validate_manual_reply_body(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise FacebookMessengerReplyUnavailable(
            "Messenger reply text is required.",
            code="reply_body_required",
        )
    if len(cleaned) > 2000:
        raise FacebookMessengerReplyUnavailable(
            "Messenger reply text cannot exceed 2,000 characters.",
            code="reply_body_too_long",
        )
    return cleaned


@transaction.atomic
def enqueue_manual_facebook_reply(
    *,
    intake: LeadIntake,
    body: str,
    client_request_id: UUID,
    created_by_id: UUID,
) -> FacebookMessengerReplyAcceptance:
    intake = LeadIntake.objects.select_for_update().get(
        id=intake.id,
        org_id=intake.org_id,
    )
    restriction = intake_data_restriction(intake)
    if restriction:
        raise FacebookMessengerReplyUnavailable(
            restriction.reason,
            code=restriction.code,
        )
    cleaned_body = validate_manual_reply_body(body)
    message = (
        FacebookMessengerMessage.objects.filter(
            org_id=intake.org_id,
            intake=intake,
        )
        .select_related("connection")
        .order_by("-occurred_at", "-created_at")
        .first()
    )
    if message is None or message.connection is None:
        raise FacebookMessengerReplyUnavailable(
            "This lead does not have an active Messenger conversation.",
            code="messenger_conversation_unavailable",
        )
    connection = message.connection
    if not connection.is_active or not connection.messenger_enabled:
        raise FacebookMessengerReplyUnavailable(
            "Messenger intake is disabled for this Page.",
            code="messenger_disabled",
        )
    if message.occurred_at < timezone.now() - timedelta(hours=24):
        raise FacebookMessengerReplyUnavailable(
            "The standard 24-hour Messenger response window has expired.",
            code="outside_messaging_window",
        )

    reply, created = FacebookMessengerReply.objects.get_or_create(
        org_id=intake.org_id,
        client_request_id=client_request_id,
        defaults={
            "connection": connection,
            "trigger_message": message,
            "page_id": message.page_id,
            "recipient_psid": message.sender_psid,
            "kind": FacebookMessengerReplyKind.MANUAL,
            "body": cleaned_body,
            "created_by_id": created_by_id,
        },
    )
    if not created and (
        reply.kind != FacebookMessengerReplyKind.MANUAL
        or reply.trigger_message.intake_id != intake.id
        or reply.body != cleaned_body
    ):
        raise FacebookMessengerReplyUnavailable(
            "This Messenger reply request id was already used.",
            code="idempotency_conflict",
        )
    enqueued = enqueue_job(
        JobRequest(
            org_id=intake.org_id,
            name=FACEBOOK_MESSENGER_REPLY_JOB,
            idempotency_key=f"facebook-messenger-reply:{reply.id}",
            payload={
                "org_id": str(intake.org_id),
                "reply_id": str(reply.id),
            },
            max_attempts=6,
        )
    )
    if reply.status == FacebookMessengerReplyStatus.PENDING:
        FacebookMessengerReply.objects.filter(id=reply.id).update(
            status=FacebookMessengerReplyStatus.QUEUED
        )
    if not enqueued.terminal_replay:
        try:
            dispatch_job(enqueued.job)
        except Exception:
            logger.exception(
                "Manual Facebook Messenger reply %s was persisted but dispatch failed",
                reply.id,
            )
    return FacebookMessengerReplyAcceptance(
        reply_id=reply.id,
        job_id=enqueued.job.id,
        replayed=not created,
    )


def enqueue_facebook_message_event(
    event_payload: Mapping[str, Any],
) -> FacebookMessengerAcceptance:
    page_id = str(event_payload.get("page_id", "")).strip()
    provider_message_id = str(event_payload.get("message_id", "")).strip()
    sender_psid = str(event_payload.get("sender_psid", "")).strip()
    if not page_id or not provider_message_id or not sender_psid:
        raise FacebookMessengerUnavailable("Facebook message event is incomplete")
    route = FacebookPageRoute.objects.filter(page_id=page_id).only("org_id").first()
    if route is None:
        raise FacebookMessengerUnavailable(
            "No organization is connected to this Facebook Page"
        )

    with database_org_context(route.org_id):
        connection = (
            FacebookPageConnection.objects.filter(
                org_id=route.org_id,
                route__page_id=page_id,
                is_active=True,
                messenger_enabled=True,
            )
            .select_related("org")
            .first()
        )
        if connection is None:
            raise FacebookMessengerUnavailable(
                "Messenger intake is not enabled for this Facebook Page"
            )
        occurred_at = _parse_datetime(event_payload.get("occurred_at"))
        attachment_types = _attachment_types(event_payload.get("attachment_types"))
        conversation_intake = _conversation_intake(
            org_id=route.org_id,
            page_id=page_id,
            sender_psid=sender_psid,
        )
        restriction = (
            intake_data_restriction(conversation_intake)
            if conversation_intake
            else None
        )
        message, created = FacebookMessengerMessage.objects.get_or_create(
            org_id=route.org_id,
            page_id=page_id,
            message_id=provider_message_id[:255],
            defaults={
                "connection": connection,
                "intake": conversation_intake,
                "sender_psid": sender_psid[:128],
                "body": (
                    "" if restriction else str(event_payload.get("body", ""))[:10000]
                ),
                "attachment_types": [] if restriction else attachment_types,
                "occurred_at": occurred_at,
            },
        )
        reply_job = None
        if connection.messenger_auto_reply_enabled and not restriction:
            try:
                reply_job = _schedule_auto_reply(connection=connection, message=message)
            except ValueError:
                logger.exception(
                    "Facebook Messenger auto-reply settings are invalid for Page %s",
                    page_id,
                )
        enqueued = enqueue_job(
            JobRequest(
                org_id=route.org_id,
                name=FACEBOOK_MESSENGER_JOB,
                idempotency_key=f"facebook-message:{message.id}",
                payload={
                    "org_id": str(route.org_id),
                    "message_id": str(message.id),
                },
                max_attempts=6,
            )
        )
        if message.status == FacebookMessengerMessageStatus.RECEIVED:
            FacebookMessengerMessage.objects.filter(id=message.id).update(
                status=FacebookMessengerMessageStatus.QUEUED
            )
        if reply_job is not None:
            try:
                dispatch_job(reply_job)
            except Exception:
                logger.exception(
                    "Facebook Messenger reply for message %s was persisted but dispatch failed",
                    message.id,
                )
        if not enqueued.terminal_replay:
            try:
                dispatch_job(enqueued.job)
            except Exception:
                logger.exception(
                    "Facebook message %s was persisted but dispatch failed",
                    message.id,
                )
        return FacebookMessengerAcceptance(
            message_id=message.id,
            job_id=enqueued.job.id,
            replayed=not created,
        )


def process_facebook_messenger_reply_job(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        reply_id = UUID(str(payload["reply_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "Facebook Messenger reply payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    reply = (
        FacebookMessengerReply.objects.filter(id=reply_id, org_id=org_id)
        .select_related("connection", "trigger_message__intake")
        .first()
    )
    if reply is None:
        raise PermanentJobError(
            "Facebook Messenger reply no longer exists.",
            code="facebook_reply_not_found",
        )
    if reply.status in {
        FacebookMessengerReplyStatus.SENT,
        FacebookMessengerReplyStatus.SKIPPED,
    }:
        return _reply_result(reply)

    connection = reply.connection
    if (
        connection is None
        or not connection.is_active
        or not connection.messenger_enabled
        or (
            reply.kind == FacebookMessengerReplyKind.AUTO_ACKNOWLEDGEMENT
            and not connection.messenger_auto_reply_enabled
        )
    ):
        _skip_reply(reply, code="messenger_auto_reply_disabled")
        return _reply_result(reply)
    if reply.trigger_message.occurred_at < timezone.now() - timedelta(hours=24):
        _skip_reply(reply, code="outside_messaging_window")
        return _reply_result(reply)
    if connection.token_expires_at and connection.token_expires_at <= timezone.now():
        _skip_reply(reply, code="facebook_page_token_expired")
        return _reply_result(reply)

    pending_error = None
    pending_cause = None
    with transaction.atomic():
        intake = None
        if reply.trigger_message.intake_id:
            intake = (
                LeadIntake.objects.select_for_update()
                .filter(
                    id=reply.trigger_message.intake_id,
                    org_id=org_id,
                )
                .first()
            )
        if intake is None:
            intake = _conversation_intake(
                org_id=org_id,
                page_id=reply.trigger_message.page_id,
                sender_psid=reply.trigger_message.sender_psid,
                for_update=True,
            )
        reply = (
            FacebookMessengerReply.objects.select_for_update()
            .filter(id=reply.id, org_id=org_id)
            .select_related("connection", "trigger_message__intake")
            .get()
        )
        if reply.status in {
            FacebookMessengerReplyStatus.SENT,
            FacebookMessengerReplyStatus.SKIPPED,
        }:
            return _reply_result(reply)
        restriction = intake_data_restriction(intake) if intake else None
        if restriction:
            _skip_reply(reply, code=restriction.code)
            return _reply_result(reply)

        _start_reply(reply)
        try:
            response = graph_client().send_text_message(
                page_id=reply.page_id,
                recipient_psid=reply.recipient_psid,
                access_token=reply.connection.get_access_token(),
                text=reply.body,
            )
        except FacebookGraphAPIError as exc:
            _fail_reply(reply, code="facebook_reply_rejected", message=str(exc))
            error_type = RetryableJobError if exc.retryable else PermanentJobError
            pending_error = error_type(str(exc), code="facebook_reply_rejected")
            pending_cause = exc
        except Exception as exc:
            _fail_reply(reply, code="facebook_reply_delivery_failed", message=str(exc))
            pending_error = RetryableJobError(
                "The Facebook Messenger reply could not be delivered.",
                code="facebook_reply_delivery_failed",
            )
            pending_cause = exc
        else:
            provider_message_id = str(response.get("message_id", "")).strip()[:255]
            _complete_reply(reply, provider_message_id=provider_message_id)
            FacebookPageConnection.objects.filter(
                id=reply.connection_id,
                org_id=org_id,
            ).update(last_message_reply_at=reply.sent_at)
    if pending_error:
        raise pending_error from pending_cause
    return _reply_result(reply)


def process_facebook_messenger_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        org_id = UUID(str(payload["org_id"]))
        message_id = UUID(str(payload["message_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            "Facebook Messenger job payload is invalid.",
            code="invalid_job_payload",
        ) from exc

    message = (
        FacebookMessengerMessage.objects.filter(id=message_id, org_id=org_id)
        .select_related("connection", "intake__crm_lead")
        .first()
    )
    if message is None:
        raise PermanentJobError(
            "Facebook Messenger message no longer exists.",
            code="facebook_message_not_found",
        )
    if message.status in {
        FacebookMessengerMessageStatus.PROCESSED,
        FacebookMessengerMessageStatus.IGNORED,
    }:
        return {
            "message_id": str(message.id),
            "status": message.status,
            "replayed": True,
        }
    if (
        message.connection is None
        or not message.connection.is_active
        or not message.connection.messenger_enabled
    ):
        _finish_message(
            message,
            status=FacebookMessengerMessageStatus.IGNORED,
            error_code="messenger_disabled",
            error_message="Messenger intake is disabled for this Page.",
        )
        return {"message_id": str(message.id), "status": "ignored"}

    conversation_id = f"{message.page_id}:{message.sender_psid}"
    intake = LeadIntake.objects.filter(
        org_id=org_id,
        source=LeadSource.FACEBOOK_MESSENGER.value,
        source_record_id=conversation_id,
    ).first()
    if intake and intake.status == LeadIntakeStatus.COMPLETED:
        restriction = intake_data_restriction(intake)
        if restriction:
            FacebookMessengerMessage.objects.filter(
                id=message.id,
                org_id=message.org_id,
            ).update(body="", attachment_types=[])
            message.body = ""
            message.attachment_types = []
            _finish_message(
                message,
                intake=intake,
                status=FacebookMessengerMessageStatus.IGNORED,
                error_code=restriction.code,
            )
            return {
                "message_id": str(message.id),
                "intake_id": str(intake.id),
                "status": FacebookMessengerMessageStatus.IGNORED,
                "error_code": restriction.code,
                "replayed": True,
            }
        restriction = _append_follow_up(intake=intake, message=message)
        if restriction:
            FacebookMessengerMessage.objects.filter(
                id=message.id,
                org_id=message.org_id,
            ).update(body="", attachment_types=[])
            _finish_message(
                message,
                intake=intake,
                status=FacebookMessengerMessageStatus.IGNORED,
                error_code=restriction.code,
            )
            return {
                "message_id": str(message.id),
                "intake_id": str(intake.id),
                "status": FacebookMessengerMessageStatus.IGNORED,
                "error_code": restriction.code,
                "replayed": True,
            }
        _finish_message(message, intake=intake)
        return {
            "message_id": str(message.id),
            "intake_id": str(intake.id),
            "lead_id": str(intake.crm_lead_id) if intake.crm_lead_id else None,
            "replayed": True,
        }

    candidate = LeadCandidate(
        org_id=org_id,
        source=LeadSource.FACEBOOK_MESSENGER,
        source_record_id=conversation_id,
        identity=LeadIdentity(),
        company=CompanySnapshot(),
        attributes={
            "message": message.body,
            "facebook_page_id": message.page_id,
            "facebook_messenger_psid": message.sender_psid,
            "facebook_attachment_types": list(message.attachment_types),
        },
        received_at=message.occurred_at,
    )
    raw_payload = {
        "page_id": message.page_id,
        "sender_psid": message.sender_psid,
        "message_id": message.message_id,
        "body": message.body,
        "attachment_types": list(message.attachment_types),
        "occurred_at": message.occurred_at.isoformat(),
    }
    try:
        result = process_candidate_intake(candidate=candidate, raw_payload=raw_payload)
    except IntakeAlreadyProcessing as exc:
        _mark_retryable_failure(message, "conversation_processing", str(exc))
        raise RetryableJobError(
            "The Facebook Messenger conversation is already processing.",
            code="conversation_processing",
        ) from exc
    except IntakeProcessingFailed as exc:
        _mark_retryable_failure(message, "intake_processing_failed", str(exc))
        raise RetryableJobError(
            "The Facebook Messenger lead pipeline failed.",
            code="intake_processing_failed",
        ) from exc

    intake = LeadIntake.objects.get(id=result.intake_id, org_id=org_id)
    _finish_message(message, intake=intake)
    return {
        "message_id": str(message.id),
        "intake_id": str(result.intake_id),
        "lead_id": str(result.lead_id) if result.lead_id else None,
        "replayed": result.replayed,
    }


@transaction.atomic
def _append_follow_up(
    *,
    intake: LeadIntake,
    message: FacebookMessengerMessage,
) -> Any:
    intake = LeadIntake.objects.select_for_update().get(
        id=intake.id,
        org_id=intake.org_id,
    )
    restriction = intake_data_restriction(intake)
    if restriction:
        return restriction
    event_key = f"facebook-message:{message.message_id}"[:120]
    if LeadLifecycleEvent.objects.filter(
        org_id=intake.org_id,
        intake=intake,
        event_key=event_key,
    ).exists():
        return
    lead = (
        Lead.objects.select_for_update()
        .filter(
            id=intake.crm_lead_id,
            org_id=intake.org_id,
        )
        .first()
    )
    if lead is not None:
        timestamp = message.occurred_at.strftime("%Y-%m-%d %H:%M UTC")
        addition = f"Facebook Messenger ({timestamp}):\n{message.body}"
        lead.description = "\n\n".join(
            value for value in (lead.description or "", addition) if value
        )[-30000:]
        lead.save(update_fields=["description", "updated_at"])
    record_lifecycle_event(
        intake=intake,
        event_type=LeadLifecycleEventType.CHANNEL_MESSAGE_RECEIVED,
        event_key=event_key,
        data={
            "channel": "facebook_messenger",
            "message_id": message.message_id,
            "attachment_types": list(message.attachment_types),
        },
    )
    return None


def _conversation_intake(
    *,
    org_id: UUID,
    page_id: str,
    sender_psid: str,
    for_update: bool = False,
) -> LeadIntake | None:
    if not page_id or not sender_psid:
        return None
    queryset = LeadIntake.objects.filter(
        org_id=org_id,
        source=LeadSource.FACEBOOK_MESSENGER.value,
        source_record_id=f"{page_id}:{sender_psid}",
    )
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.first()


def _finish_message(
    message: FacebookMessengerMessage,
    *,
    intake: LeadIntake | None = None,
    status: str = FacebookMessengerMessageStatus.PROCESSED,
    error_code: str = "",
    error_message: str = "",
) -> None:
    now = timezone.now()
    FacebookMessengerMessage.objects.filter(
        id=message.id,
        org_id=message.org_id,
    ).update(
        intake=intake or message.intake,
        status=status,
        error_code=error_code[:80],
        error_message=error_message[:1000],
        processed_at=now,
    )
    if message.connection_id:
        connection = FacebookPageConnection.objects.filter(
            id=message.connection_id,
            org_id=message.org_id,
        ).first()
        if connection and (
            connection.last_message_at is None
            or connection.last_message_at < message.occurred_at
        ):
            FacebookPageConnection.objects.filter(id=connection.id).update(
                last_message_at=message.occurred_at
            )


def _mark_retryable_failure(
    message: FacebookMessengerMessage,
    code: str,
    error_message: str,
) -> None:
    FacebookMessengerMessage.objects.filter(
        id=message.id,
        org_id=message.org_id,
    ).update(
        status=FacebookMessengerMessageStatus.QUEUED,
        error_code=code[:80],
        error_message=(error_message or "Messenger intake failed.")[:1000],
    )


def _schedule_auto_reply(
    *,
    connection: FacebookPageConnection,
    message: FacebookMessengerMessage,
):
    template = validate_auto_reply_template(connection.messenger_auto_reply_template)
    organization_name = (
        connection.org.company_name or connection.org.name or connection.page_name
    )
    body = Template(template).render(
        Context(
            {
                "organization_name": organization_name or "our team",
                "page_name": connection.page_name or "our Page",
            }
        )
    )
    reply, _ = FacebookMessengerReply.objects.get_or_create(
        org_id=message.org_id,
        page_id=message.page_id,
        recipient_psid=message.sender_psid,
        kind=FacebookMessengerReplyKind.AUTO_ACKNOWLEDGEMENT,
        defaults={
            "connection": connection,
            "trigger_message": message,
            "body": body,
        },
    )
    if reply.status in {
        FacebookMessengerReplyStatus.SENT,
        FacebookMessengerReplyStatus.SKIPPED,
    }:
        return None
    enqueued = enqueue_job(
        JobRequest(
            org_id=message.org_id,
            name=FACEBOOK_MESSENGER_REPLY_JOB,
            idempotency_key=f"facebook-messenger-reply:{reply.id}",
            payload={
                "org_id": str(message.org_id),
                "reply_id": str(reply.id),
            },
            max_attempts=6,
        )
    )
    if reply.status == FacebookMessengerReplyStatus.PENDING:
        FacebookMessengerReply.objects.filter(id=reply.id).update(
            status=FacebookMessengerReplyStatus.QUEUED
        )
    return None if enqueued.terminal_replay else enqueued.job


def _start_reply(reply: FacebookMessengerReply) -> None:
    FacebookMessengerReply.objects.filter(
        id=reply.id,
        org_id=reply.org_id,
    ).update(
        status=FacebookMessengerReplyStatus.SENDING,
        attempt_count=reply.attempt_count + 1,
        error_code="",
        error_message="",
    )
    reply.status = FacebookMessengerReplyStatus.SENDING
    reply.attempt_count += 1


def _complete_reply(
    reply: FacebookMessengerReply,
    *,
    provider_message_id: str,
) -> None:
    now = timezone.now()
    FacebookMessengerReply.objects.filter(
        id=reply.id,
        org_id=reply.org_id,
    ).update(
        status=FacebookMessengerReplyStatus.SENT,
        provider_message_id=provider_message_id,
        error_code="",
        error_message="",
        sent_at=now,
    )
    reply.status = FacebookMessengerReplyStatus.SENT
    reply.provider_message_id = provider_message_id
    reply.sent_at = now


def _skip_reply(reply: FacebookMessengerReply, *, code: str) -> None:
    FacebookMessengerReply.objects.filter(
        id=reply.id,
        org_id=reply.org_id,
    ).update(
        status=FacebookMessengerReplyStatus.SKIPPED,
        error_code=code[:80],
        error_message="",
    )
    reply.status = FacebookMessengerReplyStatus.SKIPPED
    reply.error_code = code[:80]


def _fail_reply(
    reply: FacebookMessengerReply,
    *,
    code: str,
    message: str,
) -> None:
    safe_message = (message or "Messenger reply failed.")[:1000]
    FacebookMessengerReply.objects.filter(
        id=reply.id,
        org_id=reply.org_id,
    ).update(
        status=FacebookMessengerReplyStatus.FAILED,
        error_code=code[:80],
        error_message=safe_message,
    )
    reply.status = FacebookMessengerReplyStatus.FAILED
    reply.error_code = code[:80]
    reply.error_message = safe_message


def _reply_result(reply: FacebookMessengerReply) -> dict[str, Any]:
    return {
        "reply_id": str(reply.id),
        "status": reply.status,
        "provider_message_id": reply.provider_message_id,
        "sent_at": reply.sent_at.isoformat() if reply.sent_at else None,
    }


def _parse_datetime(value: Any):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timezone.is_aware(parsed):
                return parsed
        except ValueError:
            pass
    return timezone.now()


def _attachment_types(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(
        dict.fromkeys(str(item).strip()[:32] for item in value if str(item).strip())
    )
