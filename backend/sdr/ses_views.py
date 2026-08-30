"""Public AWS SNS endpoint for SES nurture delivery feedback."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from cases.inbound.sns import (
    SNSVerificationError,
    confirm_subscription,
    parse_sns_topic_arn,
    validate_sns_topic_binding,
    verify_sns_message,
)
from common.tasks import set_rls_context
from sdr.ses_feedback import parse_ses_feedback, process_ses_feedback

logger = logging.getLogger(__name__)


class SESFeedbackWebhookView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request, *args, **kwargs):
        try:
            payload = (
                request.data
                if isinstance(request.data, dict)
                else json.loads(request.body or b"{}")
            )
        except (TypeError, ValueError):
            return Response(
                {"ok": False, "error": "invalid_json"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        configured_topic_arn = str(
            getattr(settings, "AWS_SES_FEEDBACK_SNS_TOPIC_ARN", "") or ""
        ).strip()
        if not configured_topic_arn:
            logger.error("SES feedback SNS topic binding is not configured")
            return Response(
                {"ok": False, "error": "configuration_error"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            expected_topic_arn = parse_sns_topic_arn(configured_topic_arn).arn
        except SNSVerificationError:
            logger.error("SES feedback SNS topic binding is invalid")
            return Response(
                {"ok": False, "error": "configuration_error"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            validate_sns_topic_binding(payload, expected_topic_arn)
            verify_sns_message(payload)
        except SNSVerificationError:
            logger.warning("Rejected an invalid or unbound SES feedback SNS message")
            return Response(
                {"ok": False, "error": "invalid_signature"},
                status=status.HTTP_403_FORBIDDEN,
            )

        message_type = payload.get("Type")
        if message_type == "SubscriptionConfirmation":
            try:
                confirm_subscription(
                    payload,
                    expected_topic_arn=expected_topic_arn,
                )
            except Exception as exc:
                logger.warning(
                    "Could not confirm SES feedback SNS subscription error_type=%s",
                    type(exc).__name__,
                )
                return Response(
                    {"ok": False, "error": "subscription_confirmation_failed"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            return Response({"ok": True, "subscribed": True})
        if message_type == "UnsubscribeConfirmation":
            return Response({"ok": True, "unsubscribed": True})
        if message_type != "Notification":
            return Response(
                {"ok": False, "error": "unsupported_message_type"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        feedback = parse_ses_feedback(
            str(payload.get("Message") or ""),
            sns_message_id=str(payload.get("MessageId") or ""),
        )
        if feedback is None:
            return Response({"ok": True, "status": "ignored"})
        set_rls_context(feedback.org_id)
        return Response({"ok": True, **process_ses_feedback(feedback)})
