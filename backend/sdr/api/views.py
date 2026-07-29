import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext
from sdr.api.serializers import (
    WebsiteLeadIntakeResponseSerializer,
    WebsiteLeadIntakeSerializer,
)
from sdr.services import (
    IntakeAlreadyProcessing,
    IntakeProcessingFailed,
    process_website_intake,
)

logger = logging.getLogger(__name__)


class WebsiteLeadIntakeView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    @extend_schema(
        tags=["SDR"],
        request=WebsiteLeadIntakeSerializer,
        responses={
            200: WebsiteLeadIntakeResponseSerializer,
            201: WebsiteLeadIntakeResponseSerializer,
            202: WebsiteLeadIntakeResponseSerializer,
        },
        description=(
            "Submit a server-side website lead using the organization API key "
            "in the Token request header. source_record_id is the idempotency key."
        ),
    )
    def post(self, request):
        serializer = WebsiteLeadIntakeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = process_website_intake(
                org_id=request.org.id,
                payload=serializer.validated_data,
            )
        except IntakeAlreadyProcessing as exc:
            return Response(
                {
                    "intake_id": exc.intake_id,
                    "status": "processing",
                },
                status=status.HTTP_202_ACCEPTED,
            )
        except IntakeProcessingFailed as exc:
            logger.exception("Website lead intake failed for org=%s", request.org.id)
            return Response(
                {
                    "detail": "Lead intake failed and was retained for retry.",
                    "intake_id": exc.intake_id,
                    "status": "failed",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        response = WebsiteLeadIntakeResponseSerializer(result).data
        response_status = (
            status.HTTP_201_CREATED
            if result.crm_created and not result.replayed
            else status.HTTP_200_OK
        )
        return Response(response, status=response_status)
