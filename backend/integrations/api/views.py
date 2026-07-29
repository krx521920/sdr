import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, IsOrgAdmin
from integrations.api.serializers import (
    FacebookOAuthPageSelectionSerializer,
    FacebookOAuthSessionSerializer,
    FacebookOAuthStartSerializer,
    FacebookPageConnectionCreateSerializer,
    FacebookPageConnectionSerializer,
)
from integrations.models import FacebookOAuthSession, FacebookPageConnection
from integrations.providers.facebook.adapter import (
    FacebookLeadAdsAdapter,
    FacebookWebhookError,
)
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.oauth import (
    FacebookOAuthConfigurationError,
    FacebookOAuthSelectionError,
    FacebookOAuthStateError,
    finish_facebook_oauth,
    select_facebook_pages,
    start_facebook_oauth,
)
from integrations.providers.facebook.service import (
    FacebookPageAlreadyConnected,
    FacebookPageIdentityMismatch,
    connect_facebook_page,
)
from integrations.tasks import process_facebook_lead

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
            events = adapter.parse_events(body=body)
        except FacebookWebhookError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            for event in events:
                process_facebook_lead.delay(event.as_payload())
        except Exception:
            logger.exception("Could not enqueue Facebook lead webhook")
            return Response(
                {"detail": "Lead processing queue is unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "accepted", "events": len(events)})
