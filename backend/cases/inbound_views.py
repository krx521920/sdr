"""Inbound email webhook + admin mailbox CRUD endpoints.

The webhook is intentionally public (no auth). All trust is anchored on the
minimal mailbox-to-tenant bootstrap, the mailbox's exact SNS Topic ARN, and
SNS signature verification.
"""

from __future__ import annotations

import json
import logging
import secrets

from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from automation.tenant_context import database_org_context
from cases.inbound.parser import parse_raw_email
from cases.inbound.pipeline import ingest
from cases.inbound.sns import (
    SNSVerificationError,
    confirm_subscription,
    validate_sns_topic_binding,
    verify_sns_message,
)
from cases.models import InboundMailbox, InboundMailboxWebhookRoute
from cases.serializer import InboundMailboxSerializer
from common.permissions import HasOrgContext

logger = logging.getLogger(__name__)


def _is_admin(profile):
    return profile.role == "ADMIN" or getattr(profile, "is_admin", False)


def _admin_required():
    return Response(
        {"error": True, "errors": "Admin access required"},
        status=status.HTTP_403_FORBIDDEN,
    )


class InboundMailboxWebhookView(APIView):
    """Public endpoint where AWS SNS POSTs for one configured mailbox.

    URL: `/api/cases/inbound/<mailbox_id>/`. A minimal non-RLS route resolves
    the opaque mailbox UUID to an org.  The actual mailbox is then reloaded
    under forced RLS, and its exact SNS topic/account/region binding is checked
    before any subscription confirmation or message ingestion.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)

    @extend_schema(
        tags=["InboundEmail"],
        request=inline_serializer(
            name="SNSInboundPayload",
            fields={
                "Type": serializers.CharField(),
                "Message": serializers.CharField(),
                "Signature": serializers.CharField(),
                "SigningCertURL": serializers.CharField(),
            },
        ),
        responses={
            200: inline_serializer(
                name="InboundWebhookResponse",
                fields={
                    "ok": serializers.BooleanField(),
                    "case_id": serializers.CharField(allow_null=True, required=False),
                    "dropped": serializers.BooleanField(required=False),
                    "reason": serializers.CharField(required=False),
                },
            )
        },
    )
    def post(self, request, mailbox_id, *args, **kwargs):
        route = (
            InboundMailboxWebhookRoute.objects.filter(mailbox_id=mailbox_id)
            .only("mailbox_id", "org_id")
            .first()
        )
        if route is None:
            return self._mailbox_not_found()

        with database_org_context(route.org_id):
            mailbox = (
                InboundMailbox.objects.filter(
                    pk=route.mailbox_id,
                    org_id=route.org_id,
                    is_active=True,
                )
                .select_related("org")
                .first()
            )
            if mailbox is None:
                # A missing, inactive, or mismatched scoped row is deliberately
                # indistinguishable from an unknown opaque mailbox UUID.
                return self._mailbox_not_found()
            return self._post_for_mailbox(request, mailbox)

    @staticmethod
    def _mailbox_not_found():
        return Response(
            {"error": True, "errors": "Mailbox not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    def _post_for_mailbox(self, request, mailbox):
        if mailbox.provider != "ses":
            # Other providers wired into the same URL space land here.
            return Response(
                {
                    "error": True,
                    "errors": f"Provider {mailbox.provider!r} not yet supported",
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        # SNS posts the JSON body in `request.body` — DRF may have parsed it.
        try:
            payload = (
                request.data
                if isinstance(request.data, dict)
                else json.loads(request.body or b"{}")
            )
        except (ValueError, TypeError):
            return Response(
                {"error": True, "errors": "Body is not valid JSON"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # A valid AWS signature alone is insufficient: any AWS customer
            # can create a signed SNS topic.  Bind the signed fields to this
            # mailbox's configured topic, account, partition, and region before
            # fetching a certificate or following a confirmation URL.
            validate_sns_topic_binding(payload, mailbox.sns_topic_arn)
            verify_sns_message(payload)
        except SNSVerificationError:
            # Do not log the exception text: network/parsing exceptions can
            # contain signed URLs, and subscription URLs contain a bearer-like
            # confirmation token.
            logger.warning("SNS verification failed for mailbox=%s", mailbox.id)
            return Response(
                {"error": True, "errors": "Signature verification failed"},
                status=status.HTTP_403_FORBIDDEN,
            )

        msg_type = payload.get("Type")
        if msg_type == "SubscriptionConfirmation":
            try:
                confirm_subscription(
                    payload,
                    expected_topic_arn=mailbox.sns_topic_arn,
                )
            except Exception as exc:
                logger.warning(
                    "SNS subscription confirmation failed for mailbox=%s "
                    "error_type=%s",
                    mailbox.id,
                    type(exc).__name__,
                )
                return Response(
                    {"error": True, "errors": "SubscribeURL fetch failed"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            return Response({"ok": True, "subscribed": True})

        if msg_type == "UnsubscribeConfirmation":  # pragma: no cover — informational
            logger.info("SNS unsubscribe for mailbox=%s", mailbox.id)
            return Response({"ok": True, "unsubscribed": True})

        if msg_type != "Notification":
            return Response(
                {"error": True, "errors": f"Unsupported SNS Type: {msg_type!r}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # SES with "SNS Notification with full content" puts the raw RFC-5322
        # email in payload.Message as a plain string. Older SES configurations
        # send a JSON envelope (`{"notificationType":"Received","content":"..."}`);
        # peel that off if we see it.
        raw_message = payload.get("Message", "") or ""
        try:
            envelope = json.loads(raw_message)
            if isinstance(envelope, dict) and "content" in envelope:
                raw_message = envelope.get("content") or ""
        except (ValueError, TypeError):
            pass

        if not raw_message:
            return Response(
                {"error": True, "errors": "SNS Message body is empty"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parsed = parse_raw_email(raw_message)
        result = ingest(parsed, mailbox)

        return Response(
            {
                "ok": True,
                "case_id": str(result.case.id) if result.case else None,
                "dropped": result.dropped,
                "reason": result.drop_reason,
                "created_case": result.created_case,
            },
            status=status.HTTP_200_OK,
        )


class InboundMailboxListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    @extend_schema(
        tags=["InboundEmail"], responses={200: InboundMailboxSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        if not _is_admin(request.profile):
            return _admin_required()
        org = request.profile.org
        qs = InboundMailbox.objects.filter(org=org).order_by("address")
        return Response({"mailboxes": InboundMailboxSerializer(qs, many=True).data})

    @extend_schema(
        tags=["InboundEmail"],
        request=InboundMailboxSerializer,
        responses={201: InboundMailboxSerializer},
    )
    def post(self, request, *args, **kwargs):
        if not _is_admin(request.profile):
            return _admin_required()
        org = request.profile.org
        data = dict(request.data)
        # Auto-generate a webhook secret on create when the admin didn't paste one.
        if not data.get("webhook_secret"):
            data["webhook_secret"] = secrets.token_urlsafe(32)
        serializer = InboundMailboxSerializer(data=data, context={"org": org})
        if not serializer.is_valid():
            return Response(
                {"error": True, "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            with transaction.atomic():
                serializer.save(org=org)
        except IntegrityError:
            return Response(
                {
                    "error": True,
                    "errors": "Mailbox address or SNS topic binding already exists",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class InboundMailboxDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    def _get_object(self, pk, org):
        return InboundMailbox.objects.filter(pk=pk, org=org).first()

    @extend_schema(tags=["InboundEmail"], responses={200: InboundMailboxSerializer})
    def get(self, request, pk, *args, **kwargs):
        if not _is_admin(request.profile):
            return _admin_required()
        obj = self._get_object(pk, request.profile.org)
        if not obj:
            return Response(
                {"error": True, "errors": "Mailbox not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(InboundMailboxSerializer(obj).data)

    @extend_schema(
        tags=["InboundEmail"],
        request=InboundMailboxSerializer,
        responses={200: InboundMailboxSerializer},
    )
    def put(self, request, pk, *args, **kwargs):
        if not _is_admin(request.profile):
            return _admin_required()
        org = request.profile.org
        obj = self._get_object(pk, org)
        if not obj:
            return Response(
                {"error": True, "errors": "Mailbox not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = InboundMailboxSerializer(
            obj, data=request.data, partial=True, context={"org": org}
        )
        if not serializer.is_valid():
            return Response(
                {"error": True, "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return Response(
                {
                    "error": True,
                    "errors": "Mailbox address or SNS topic binding already exists",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(serializer.data)

    @extend_schema(
        tags=["InboundEmail"],
        responses={
            200: inline_serializer(
                name="MailboxDeleteResponse",
                fields={
                    "error": serializers.BooleanField(),
                    "message": serializers.CharField(),
                },
            )
        },
    )
    def delete(self, request, pk, *args, **kwargs):
        if not _is_admin(request.profile):
            return _admin_required()
        obj = self._get_object(pk, request.profile.org)
        if not obj:
            return Response(
                {"error": True, "errors": "Mailbox not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        obj.delete()
        return Response({"error": False, "message": "Mailbox deleted"})
