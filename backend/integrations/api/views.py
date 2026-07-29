import logging

from django.conf import settings
from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, IsOrgAdmin
from integrations.api.serializers import (
    FacebookPageConnectionCreateSerializer,
    FacebookPageConnectionSerializer,
)
from integrations.models import FacebookPageConnection
from integrations.providers.facebook.adapter import (
    FacebookLeadAdsAdapter,
    FacebookWebhookError,
)
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.service import (
    FacebookPageAlreadyConnected,
    FacebookPageIdentityMismatch,
    connect_facebook_page,
)
from integrations.tasks import process_facebook_lead

logger = logging.getLogger(__name__)


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
