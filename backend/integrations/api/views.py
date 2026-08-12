import logging
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, IsOrgAdmin
from integrations.api.serializers import (
    FacebookConversionSettingsSerializer,
    FacebookMessengerManualReplySerializer,
    FacebookMessengerSettingsSerializer,
    FacebookOAuthPageSelectionSerializer,
    FacebookOAuthSessionSerializer,
    FacebookOAuthStartSerializer,
    FacebookPageConnectionCreateSerializer,
    FacebookPageConnectionSerializer,
)
from integrations.models import (
    FacebookConversionSettings,
    FacebookMessengerMessage,
    FacebookMessengerReply,
    FacebookOAuthSession,
    FacebookPageConnection,
)
from integrations.providers.facebook.adapter import (
    FacebookLeadAdsAdapter,
    FacebookWebhookError,
)
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.conversions import (
    reconcile_recent_conversion_events,
)
from integrations.providers.facebook.jobs import enqueue_facebook_lead_event
from integrations.providers.facebook.messenger import (
    FacebookMessengerReplyUnavailable,
    FacebookMessengerUnavailable,
    enqueue_facebook_message_event,
    enqueue_manual_facebook_reply,
)
from integrations.providers.facebook.oauth import (
    FacebookOAuthConfigurationError,
    FacebookOAuthSelectionError,
    FacebookOAuthStateError,
    finish_facebook_oauth,
    select_facebook_pages,
    start_facebook_oauth,
)
from integrations.providers.facebook.service import (
    FacebookConnectionUnavailable,
    FacebookPageAlreadyConnected,
    FacebookPageIdentityMismatch,
    connect_facebook_page,
    set_facebook_messenger_enabled,
)
from leads.models import Lead
from sdr.models import LeadIntake

logger = logging.getLogger(__name__)


def _frontend_oauth_redirect(**params):
    """Redirect with non-sensitive result flags while preserving configured query params."""
    target = settings.META_OAUTH_FRONTEND_REDIRECT_URL
    if not target:
        return None
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value})
    return HttpResponseRedirect(
        urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    )


class FacebookOAuthStartView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=None,
        responses={201: FacebookOAuthStartSerializer},
    )
    def post(self, request):
        try:
            result = start_facebook_oauth(
                org_id=request.org.id,
                profile_id=request.profile.id,
            )
        except FacebookOAuthConfigurationError:
            return Response(
                {"detail": "Facebook OAuth is not configured for this deployment."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            FacebookOAuthStartSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )


class FacebookOAuthCallbackView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    @extend_schema(tags=["Integrations - Facebook"], responses={302: None})
    def get(self, request):
        if request.query_params.get("error"):
            redirect_response = _frontend_oauth_redirect(
                facebook_oauth_error="authorization_denied"
            )
            if redirect_response:
                return redirect_response
            return Response(
                {"detail": "Facebook authorization was not completed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        code = request.query_params.get("code", "").strip()
        state_value = request.query_params.get("state", "").strip()
        if not code or not state_value:
            redirect_response = _frontend_oauth_redirect(
                facebook_oauth_error="invalid_callback"
            )
            if redirect_response:
                return redirect_response
            return Response(
                {"detail": "Facebook OAuth callback is incomplete."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            oauth_session = finish_facebook_oauth(code=code, state=state_value)
        except FacebookOAuthStateError:
            error_code = "invalid_state"
        except FacebookGraphAPIError:
            logger.exception("Meta rejected the Facebook OAuth callback")
            error_code = "provider_error"
        except Exception:
            logger.exception("Facebook OAuth callback failed")
            error_code = "authorization_failed"
        else:
            redirect_response = _frontend_oauth_redirect(
                facebook_oauth_session=oauth_session.id
            )
            if redirect_response:
                return redirect_response
            return Response(FacebookOAuthSessionSerializer(oauth_session).data)

        redirect_response = _frontend_oauth_redirect(facebook_oauth_error=error_code)
        if redirect_response:
            return redirect_response
        return Response(
            {"detail": "Facebook authorization could not be completed."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class FacebookOAuthSessionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Facebook"],
        responses={200: FacebookOAuthSessionSerializer},
    )
    def get(self, request, session_id):
        oauth_session = FacebookOAuthSession.objects.filter(
            id=session_id,
            org=request.org,
            initiated_by_profile=request.profile,
        ).first()
        if oauth_session is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(FacebookOAuthSessionSerializer(oauth_session).data)


class FacebookOAuthPageSelectionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookOAuthPageSelectionSerializer,
        responses={201: FacebookPageConnectionSerializer(many=True)},
    )
    def post(self, request, session_id):
        serializer = FacebookOAuthPageSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            connections = select_facebook_pages(
                org_id=request.org.id,
                profile_id=request.profile.id,
                session_id=session_id,
                page_ids=serializer.validated_data["page_ids"],
            )
        except FacebookOAuthSession.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except FacebookOAuthSelectionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except (FacebookPageAlreadyConnected, FacebookPageIdentityMismatch) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except FacebookGraphAPIError as exc:
            response_status = (
                status.HTTP_502_BAD_GATEWAY
                if exc.retryable
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(
                {"detail": "Meta could not connect the selected Facebook Page."},
                status=response_status,
            )
        return Response(
            FacebookPageConnectionSerializer(connections, many=True).data,
            status=status.HTTP_201_CREATED,
        )


class FacebookPageConnectionListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Facebook"],
        responses={200: FacebookPageConnectionSerializer(many=True)},
    )
    def get(self, request):
        connections = FacebookPageConnection.objects.filter(
            org=request.org
        ).select_related("route")
        return Response(FacebookPageConnectionSerializer(connections, many=True).data)

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookPageConnectionCreateSerializer,
        responses={201: FacebookPageConnectionSerializer},
    )
    def post(self, request):
        serializer = FacebookPageConnectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            connection = connect_facebook_page(
                org_id=request.org.id,
                **serializer.validated_data,
            )
        except (FacebookPageAlreadyConnected, FacebookPageIdentityMismatch) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except FacebookGraphAPIError as exc:
            response_status = (
                status.HTTP_502_BAD_GATEWAY
                if exc.retryable
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(
                {"detail": "Meta rejected the Page access token."},
                status=response_status,
            )
        return Response(
            FacebookPageConnectionSerializer(connection).data,
            status=status.HTTP_201_CREATED,
        )


class FacebookPageConnectionDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["Integrations - Facebook"], responses={204: None})
    def delete(self, request, connection_id):
        connection = FacebookPageConnection.objects.filter(
            id=connection_id, org=request.org
        ).first()
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        connection.route.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookMessengerSettingsSerializer,
        responses={200: FacebookPageConnectionSerializer},
    )
    def patch(self, request, connection_id):
        serializer = FacebookMessengerSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = (
            FacebookPageConnection.objects.filter(
                id=connection_id,
                org=request.org,
            )
            .select_related("route")
            .first()
        )
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        requested = serializer.validated_data
        messenger_enabled = requested.get(
            "messenger_enabled",
            connection.messenger_enabled,
        )
        auto_reply_enabled = requested.get(
            "messenger_auto_reply_enabled",
            connection.messenger_auto_reply_enabled,
        )
        if requested.get("messenger_auto_reply_enabled") is True and not messenger_enabled:
            return Response(
                {
                    "detail": (
                        "Messenger intake must be enabled before automatic replies."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not messenger_enabled:
            auto_reply_enabled = False
        try:
            if (
                "messenger_enabled" in requested
                and messenger_enabled != connection.messenger_enabled
            ):
                set_facebook_messenger_enabled(
                    connection,
                    enabled=messenger_enabled,
                )
        except FacebookGraphAPIError as exc:
            response_status = (
                status.HTTP_502_BAD_GATEWAY
                if exc.retryable
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(
                {
                    "detail": (
                        "Meta could not enable Messenger for this Page. "
                        "Reconnect with the pages_messaging permission."
                    )
                },
                status=response_status,
            )
        update_fields = []
        if "messenger_auto_reply_enabled" in requested:
            connection.messenger_auto_reply_enabled = auto_reply_enabled
            update_fields.append("messenger_auto_reply_enabled")
        if "messenger_auto_reply_template" in requested:
            connection.messenger_auto_reply_template = requested[
                "messenger_auto_reply_template"
            ]
            update_fields.append("messenger_auto_reply_template")
        if update_fields:
            connection.save(update_fields=[*update_fields, "updated_at"])
        return Response(FacebookPageConnectionSerializer(connection).data)


class FacebookMessengerConversationView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    @staticmethod
    def _lead_and_intake(request, lead_id):
        lead = Lead.objects.filter(id=lead_id, org=request.org).first()
        if lead is None:
            raise NotFound("Lead not found.")
        intake = (
            LeadIntake.objects.filter(
                org=request.org,
                crm_lead=lead,
                source="facebook_messenger",
            )
            .select_related("assigned_profile")
            .order_by("-created_at")
            .first()
        )
        is_admin = (
            request.user.is_superuser
            or request.profile.role == "ADMIN"
            or request.profile.is_organization_admin
        )
        is_assigned = lead.assigned_to.filter(id=request.profile.id).exists() or (
            intake is not None and intake.assigned_profile_id == request.profile.id
        )
        if not is_admin and lead.created_by_id != request.user.id and not is_assigned:
            raise PermissionDenied("You do not have access to this lead conversation.")
        return lead, intake

    @staticmethod
    def _conversation_payload(lead, intake):
        if intake is None:
            return {
                "available": False,
                "lead_id": str(lead.id),
                "messages": [],
                "can_reply": False,
                "reply_unavailable_reason": "not_messenger_lead",
            }

        inbound_messages = list(
            FacebookMessengerMessage.objects.filter(
                org_id=intake.org_id,
                intake=intake,
            )
            .select_related("connection")
            .order_by("occurred_at", "created_at")
        )
        replies = list(
            FacebookMessengerReply.objects.filter(
                org_id=intake.org_id,
                trigger_message__intake=intake,
            )
            .select_related("created_by")
            .order_by("created_at")
        )
        timeline = []
        for message in inbound_messages:
            timeline.append(
                {
                    "id": str(message.id),
                    "direction": "inbound",
                    "body": message.body,
                    "attachment_types": list(message.attachment_types),
                    "status": message.status,
                    "kind": "customer",
                    "provider_message_id": message.message_id,
                    "sent_by": "Customer",
                    "timestamp": message.occurred_at,
                    "error_code": message.error_code,
                }
            )
        for reply in replies:
            timeline.append(
                {
                    "id": str(reply.id),
                    "direction": "outbound",
                    "body": reply.body,
                    "attachment_types": [],
                    "status": reply.status,
                    "kind": reply.kind,
                    "provider_message_id": reply.provider_message_id,
                    "sent_by": (
                        reply.created_by.email if reply.created_by else "Automation"
                    ),
                    "timestamp": reply.sent_at or reply.created_at,
                    "error_code": reply.error_code,
                }
            )
        timeline.sort(key=lambda item: item["timestamp"])
        for item in timeline:
            item["timestamp"] = item["timestamp"].isoformat()

        latest = inbound_messages[-1] if inbound_messages else None
        connection = latest.connection if latest else None
        window_expires_at = (
            latest.occurred_at + timedelta(hours=24) if latest is not None else None
        )
        can_reply = bool(
            latest
            and connection
            and connection.is_active
            and connection.messenger_enabled
            and window_expires_at
            and window_expires_at > timezone.now()
        )
        if latest is None:
            unavailable_reason = "no_inbound_message"
        elif connection is None or not connection.is_active:
            unavailable_reason = "page_disconnected"
        elif not connection.messenger_enabled:
            unavailable_reason = "messenger_disabled"
        elif not can_reply:
            unavailable_reason = "outside_messaging_window"
        else:
            unavailable_reason = ""
        return {
            "available": True,
            "intake_id": str(intake.id),
            "lead_id": str(lead.id),
            "page_id": latest.page_id if latest else "",
            "page_name": connection.page_name if connection else "Facebook Page",
            "messages": timeline,
            "can_reply": can_reply,
            "reply_window_expires_at": (
                window_expires_at.isoformat() if window_expires_at else None
            ),
            "reply_unavailable_reason": unavailable_reason,
        }

    @extend_schema(tags=["Integrations - Facebook"])
    def get(self, request, lead_id):
        lead, intake = self._lead_and_intake(request, lead_id)
        return Response(self._conversation_payload(lead, intake))

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookMessengerManualReplySerializer,
    )
    def post(self, request, lead_id):
        lead, intake = self._lead_and_intake(request, lead_id)
        if intake is None:
            raise NotFound("This lead does not have a Messenger conversation.")
        serializer = FacebookMessengerManualReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            accepted = enqueue_manual_facebook_reply(
                intake=intake,
                body=serializer.validated_data["body"],
                client_request_id=serializer.validated_data["client_request_id"],
                created_by_id=request.user.id,
            )
        except FacebookMessengerReplyUnavailable as exc:
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reply = FacebookMessengerReply.objects.get(
            id=accepted.reply_id,
            org=request.org,
        )
        return Response(
            {
                "reply_id": str(reply.id),
                "status": reply.status,
                "job_id": str(accepted.job_id),
                "replayed": accepted.replayed,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class FacebookConversionSettingsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _configuration(request):
        configuration, _ = FacebookConversionSettings.objects.get_or_create(
            org=request.org
        )
        return configuration

    @extend_schema(
        tags=["Integrations - Facebook"],
        responses={200: FacebookConversionSettingsSerializer},
    )
    def get(self, request):
        return Response(
            FacebookConversionSettingsSerializer(self._configuration(request)).data
        )

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookConversionSettingsSerializer,
        responses={200: FacebookConversionSettingsSerializer},
    )
    def put(self, request):
        return self._save(request, partial=False)

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookConversionSettingsSerializer,
        responses={200: FacebookConversionSettingsSerializer},
    )
    def patch(self, request):
        return self._save(request, partial=True)

    def _save(self, request, *, partial):
        serializer = FacebookConversionSettingsSerializer(
            self._configuration(request),
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        configuration = serializer.save()
        backfilled = 0
        if configuration.is_enabled:
            backfilled = reconcile_recent_conversion_events(org_id=request.org.id)
        response_data = dict(FacebookConversionSettingsSerializer(configuration).data)
        response_data["backfilled_events"] = backfilled
        return Response(response_data)


class FacebookWebhookView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request):
        mode = request.query_params.get("hub.mode")
        verify_token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")
        if (
            mode == "subscribe"
            and challenge is not None
            and settings.META_WEBHOOK_VERIFY_TOKEN
            and verify_token == settings.META_WEBHOOK_VERIFY_TOKEN
        ):
            return HttpResponse(challenge, content_type="text/plain")
        return Response(
            {"detail": "Webhook verification failed."},
            status=status.HTTP_403_FORBIDDEN,
        )

    def post(self, request):
        body = request.body
        adapter = FacebookLeadAdsAdapter(app_secret=settings.META_APP_SECRET)
        if not adapter.verify_signature(headers=request.headers, body=body):
            return Response(
                {"detail": "Invalid webhook signature."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            lead_events = adapter.parse_events(body=body)
            message_events = adapter.parse_message_events(body=body)
        except FacebookWebhookError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            for event in lead_events:
                try:
                    enqueue_facebook_lead_event(event.as_payload())
                except FacebookConnectionUnavailable:
                    logger.warning(
                        "Ignoring Facebook lead webhook for an unconnected Page: %s",
                        event.page_id,
                    )
            for event in message_events:
                try:
                    enqueue_facebook_message_event(event.as_payload())
                except FacebookMessengerUnavailable:
                    logger.info(
                        "Ignoring Messenger webhook for a disabled Page: %s",
                        event.page_id,
                    )
        except Exception:
            logger.exception("Could not persist or dispatch Facebook lead webhook")
            return Response(
                {"detail": "Lead processing queue is unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "status": "accepted",
                "events": len(lead_events) + len(message_events),
                "lead_events": len(lead_events),
                "messenger_events": len(message_events),
            }
        )
