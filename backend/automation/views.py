from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from automation.models import AutomationJob, AutomationJobStatus
from automation.serializers import (
    AutomationJobDetailSerializer,
    AutomationJobSerializer,
)
from automation.services import (
    AutomationJobStateError,
    dispatch_job,
    replay_dead_letter,
)
from common.permissions import HasOrgContext, IsOrgAdmin


def _bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


class AutomationJobListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Automation"],
        responses={200: AutomationJobSerializer(many=True)},
    )
    def get(self, request):
        base = AutomationJob.objects.filter(org=request.org)
        summary = {
            row["status"]: row["count"]
            for row in base.values("status").annotate(count=Count("id"))
        }
        queryset = base
        status_filter = request.query_params.get("status", "").strip()
        if status_filter in AutomationJobStatus.values:
            queryset = queryset.filter(status=status_filter)
        name_filter = request.query_params.get("name", "").strip()
        if name_filter:
            queryset = queryset.filter(name=name_filter[:160])

        limit = _bounded_int(
            request.query_params.get("limit"),
            default=50,
            minimum=1,
            maximum=100,
        )
        offset = _bounded_int(
            request.query_params.get("offset"),
            default=0,
            minimum=0,
            maximum=1_000_000,
        )
        count = queryset.count()
        jobs = queryset.order_by("-created_at")[offset : offset + limit]
        return Response(
            {
                "count": count,
                "summary": summary,
                "results": AutomationJobSerializer(jobs, many=True).data,
            }
        )


class AutomationJobDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["Automation"], responses={200: AutomationJobDetailSerializer})
    def get(self, request, job_id):
        job = (
            AutomationJob.objects.filter(id=job_id, org=request.org)
            .prefetch_related("attempts")
            .first()
        )
        if job is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(AutomationJobDetailSerializer(job).data)


class AutomationJobRetryView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Automation"], request=None, responses={200: AutomationJobSerializer}
    )
    def post(self, request, job_id):
        if not AutomationJob.objects.filter(id=job_id, org=request.org).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            job = replay_dead_letter(job_id=job_id, org_id=request.org.id)
        except AutomationJobStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        try:
            dispatch_job(job)
        except Exception:
            job.refresh_from_db()
            return Response(
                AutomationJobSerializer(job).data,
                status=status.HTTP_202_ACCEPTED,
            )
        job.refresh_from_db()
        return Response(AutomationJobSerializer(job).data)
