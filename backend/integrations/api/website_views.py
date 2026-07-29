from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext
from integrations.api.website_serializers import (
    WebsiteLeadAcceptedSerializer,
    WebsiteLeadIntakeSerializer,
)
from integrations.providers.website.jobs import enqueue_website_intake
from sdr.models import LeadIntakeStatus


class WebsiteLeadIntakeView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    @extend_schema(
        tags=["SDR"],
        request=WebsiteLeadIntakeSerializer,
        responses={
            200: WebsiteLeadAcceptedSerializer,
            202: WebsiteLeadAcceptedSerializer,
        },
        description=(
            "Submit a server-side website lead using the organization API key "
            "in the Token request header. The lead is persisted before a 202 response "
            "and processed asynchronously. source_record_id is the idempotency key."
        ),
    )
    def post(self, request):
        serializer = WebsiteLeadIntakeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = enqueue_website_intake(
            org_id=request.org.id,
            payload=serializer.validated_data,
        )
        response = WebsiteLeadAcceptedSerializer(
            {
                "intake_id": result.intake_id,
                "job_id": result.job_id,
                "status": result.status,
                "lead_id": result.lead_id,
                "replayed": result.replayed,
                "status_url": f"/api/sdr/intakes/{result.intake_id}/",
            }
        ).data
        response_status = (
            status.HTTP_200_OK
            if result.status == LeadIntakeStatus.COMPLETED
            else status.HTTP_202_ACCEPTED
        )
        return Response(response, status=response_status)
