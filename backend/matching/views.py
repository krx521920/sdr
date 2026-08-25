"""Tenant-isolated API views for people, evidence, opportunities, and matches."""

from uuid import UUID

from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.models import MatchingAccessLevel
from common.permissions import HasOrgContext
from matching.decisions import MatchDecisionError, apply_match_decision
from matching.models import (
    Evidence,
    Match,
    MatchDecisionEvent,
    MatchOpportunity,
    MatchRevision,
    MatchRun,
    Person,
    PersonIdentity,
)
from matching.permissions import HasMatchingAccess, matching_capabilities
from matching.serializers import (
    AsyncRecomputeMatchesSerializer,
    EvidenceSerializer,
    MatchDecisionEventSerializer,
    MatchingCapabilitiesSerializer,
    MatchOpportunitySerializer,
    MatchRevisionSerializer,
    MatchRunSerializer,
    MatchSerializer,
    MatchStatusSerializer,
    PersonIdentitySerializer,
    PersonSerializer,
)
from matching.services import (
    RecomputeEnqueueError,
    RecomputeIdempotencyConflict,
    enqueue_opportunity_recompute,
)


class MatchingAPIView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, HasMatchingAccess)
    matching_access_by_method = {}

    @staticmethod
    def limit(request):
        try:
            return min(max(int(request.query_params.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            return 100

    @staticmethod
    def list_response(queryset, serializer_class, request, *, context=None):
        count = queryset.count()
        context = {"request": request, "org": request.org, **(context or {})}
        return Response(
            {
                "count": count,
                "results": serializer_class(
                    queryset[: MatchingAPIView.limit(request)],
                    many=True,
                    context=context,
                ).data,
            }
        )


class PersonListCreateView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }

    def get(self, request):
        queryset = (
            Person.objects.filter(org=request.org)
            .annotate(evidence_count=Count("evidence"))
            .prefetch_related("identities")
        )
        status_filter = request.query_params.get("status", "").strip()
        availability = request.query_params.get("availability", "").strip()
        query = request.query_params.get("q", "").strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if availability:
            queryset = queryset.filter(availability=availability)
        if query:
            queryset = queryset.filter(display_name__icontains=query)
        return self.list_response(queryset, PersonSerializer, request)

    def post(self, request):
        serializer = PersonSerializer(data=request.data, context={"org": request.org})
        serializer.is_valid(raise_exception=True)
        person = serializer.save(org=request.org)
        person.evidence_count = 0
        return Response(
            PersonSerializer(person, context={"org": request.org}).data,
            status=status.HTTP_201_CREATED,
        )


class PersonDetailView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.MANAGE,
    }

    @staticmethod
    def get_object(request, person_id):
        return (
            Person.objects.filter(org=request.org, id=person_id)
            .annotate(evidence_count=Count("evidence"))
            .prefetch_related("identities")
            .first()
        )

    def get(self, request, person_id):
        person = self.get_object(request, person_id)
        if person is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(PersonSerializer(person, context={"org": request.org}).data)

    def patch(self, request, person_id):
        person = self.get_object(request, person_id)
        if person is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = PersonSerializer(
            person,
            data=request.data,
            partial=True,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        person = serializer.save()
        return Response(PersonSerializer(person, context={"org": request.org}).data)


class PersonIdentityListCreateView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }

    def get(self, request):
        queryset = PersonIdentity.objects.filter(org=request.org).select_related(
            "person"
        )
        person_id = request.query_params.get("person", "").strip()
        if person_id:
            queryset = queryset.filter(person_id=person_id)
        return self.list_response(queryset, PersonIdentitySerializer, request)

    def post(self, request):
        serializer = PersonIdentitySerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        identity = serializer.save(org=request.org)
        return Response(
            PersonIdentitySerializer(identity, context={"org": request.org}).data,
            status=status.HTTP_201_CREATED,
        )


class EvidenceListCreateView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }

    def get(self, request):
        queryset = Evidence.objects.filter(org=request.org).select_related("person")
        for field in ("person", "source", "kind"):
            value = request.query_params.get(field, "").strip()
            if value:
                queryset = queryset.filter(
                    **{f"{field}_id" if field == "person" else field: value}
                )
        return self.list_response(queryset, EvidenceSerializer, request)

    def post(self, request):
        serializer = EvidenceSerializer(data=request.data, context={"org": request.org})
        serializer.is_valid(raise_exception=True)
        evidence = serializer.save(org=request.org)
        return Response(
            EvidenceSerializer(evidence, context={"org": request.org}).data,
            status=status.HTTP_201_CREATED,
        )


class MatchOpportunityListCreateView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }

    def get(self, request):
        queryset = (
            MatchOpportunity.objects.filter(org=request.org)
            .annotate(match_count=Count("matches"))
            .select_related("owner")
        )
        status_filter = request.query_params.get("status", "").strip()
        opportunity_type = request.query_params.get("type", "").strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if opportunity_type:
            queryset = queryset.filter(opportunity_type=opportunity_type)
        return self.list_response(queryset, MatchOpportunitySerializer, request)

    def post(self, request):
        serializer = MatchOpportunitySerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        opportunity = serializer.save(org=request.org)
        opportunity.match_count = 0
        return Response(
            MatchOpportunitySerializer(
                opportunity,
                context={"org": request.org},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class MatchOpportunityDetailView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.MANAGE,
    }

    @staticmethod
    def get_object(request, opportunity_id):
        return (
            MatchOpportunity.objects.filter(org=request.org, id=opportunity_id)
            .annotate(match_count=Count("matches"))
            .select_related("owner")
            .first()
        )

    def get(self, request, opportunity_id):
        opportunity = self.get_object(request, opportunity_id)
        if opportunity is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(
            MatchOpportunitySerializer(
                opportunity,
                context={"org": request.org},
            ).data
        )

    def patch(self, request, opportunity_id):
        opportunity = self.get_object(request, opportunity_id)
        if opportunity is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = MatchOpportunitySerializer(
            opportunity,
            data=request.data,
            partial=True,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        opportunity = serializer.save()
        return Response(
            MatchOpportunitySerializer(
                opportunity,
                context={"org": request.org},
            ).data
        )


class OpportunityMatchListRecomputeView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.RECOMPUTE,
    }

    @staticmethod
    def get_opportunity(request, opportunity_id):
        return MatchOpportunity.objects.filter(
            org=request.org,
            id=opportunity_id,
        ).first()

    def get(self, request, opportunity_id):
        if self.get_opportunity(request, opportunity_id) is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = (
            Match.objects.filter(org=request.org, opportunity_id=opportunity_id)
            .select_related("person", "opportunity")
            .prefetch_related("evidence_links__evidence")
        )
        status_filter = request.query_params.get("status", "").strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return self.list_response(queryset, MatchSerializer, request)

    def post(self, request, opportunity_id):
        # Compatibility path for early matching clients. All ranking writes now
        # flow through the durable, audited background-run endpoint.
        return OpportunityRecomputeView().post(request, opportunity_id)


class OpportunityRecomputeView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.RECOMPUTE}

    def post(self, request, opportunity_id):
        opportunity = MatchOpportunity.objects.filter(
            org=request.org,
            id=opportunity_id,
        ).first()
        if opportunity is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        raw_key = request.headers.get("Idempotency-Key", "").strip()
        try:
            idempotency_key = UUID(raw_key)
        except (TypeError, ValueError):
            return Response(
                {"idempotency_key": ["A valid UUID Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = AsyncRecomputeMatchesSerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        try:
            run = enqueue_opportunity_recompute(
                org=request.org,
                opportunity=opportunity,
                requested_by=request.profile,
                person_ids=serializer.validated_data.get("person_ids"),
                idempotency_key=idempotency_key,
            )
        except RecomputeIdempotencyConflict as exc:
            return Response(
                {"idempotency_key": [str(exc)]},
                status=status.HTTP_409_CONFLICT,
            )
        except RecomputeEnqueueError as exc:
            return Response(
                {"person_ids": [str(exc)]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            MatchRunSerializer(run, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED,
            headers={"Retry-After": "2"},
        )


class MatchRunDetailView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, run_id):
        run = (
            MatchRun.objects.filter(org=request.org, id=run_id)
            .select_related("automation_job", "opportunity", "requested_by")
            .first()
        )
        if run is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(MatchRunSerializer(run, context={"request": request}).data)


class OpportunityMatchRunListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, opportunity_id):
        if not MatchOpportunity.objects.filter(
            org=request.org,
            id=opportunity_id,
        ).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = MatchRun.objects.filter(
            org=request.org,
            opportunity_id=opportunity_id,
        ).select_related("automation_job", "opportunity", "requested_by")
        try:
            limit = min(max(int(request.query_params.get("limit", 50)), 1), 100)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 50, 0
        count = queryset.count()
        return Response(
            {
                "count": count,
                "results": MatchRunSerializer(
                    queryset[offset : offset + limit],
                    many=True,
                    context={"request": request},
                ).data,
            }
        )


class MatchDetailView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.DECIDE,
    }

    @staticmethod
    def get_object(request, match_id):
        return (
            Match.objects.filter(org=request.org, id=match_id)
            .select_related("person", "opportunity")
            .prefetch_related("evidence_links__evidence")
            .first()
        )

    def get(self, request, match_id):
        match = self.get_object(request, match_id)
        if match is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(MatchSerializer(match, context={"org": request.org}).data)

    def patch(self, request, match_id):
        if self.get_object(request, match_id) is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = MatchStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        header_key = request.headers.get("Idempotency-Key", "").strip()
        body_key = serializer.validated_data.get("idempotency_key", "")
        if header_key and body_key and header_key != body_key:
            return Response(
                {"idempotency_key": ["Header and body keys must match."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        idempotency_key = header_key or body_key
        if not idempotency_key:
            return Response(
                {"idempotency_key": ["Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = apply_match_decision(
                org=request.org,
                match_id=match_id,
                to_status=serializer.validated_data["status"],
                expected_decision_revision=serializer.validated_data[
                    "expected_revision"
                ],
                expected_ranking_revision=serializer.validated_data[
                    "expected_ranking_revision"
                ],
                reason_code=serializer.validated_data["reason_code"],
                reason=serializer.validated_data["reason"],
                actor=request.profile,
                idempotency_key=idempotency_key,
            )
        except MatchDecisionError as exc:
            return Response(exc.as_dict(), status=exc.status_code)
        match = self.get_object(request, result.match.id)
        return Response(MatchSerializer(match, context={"org": request.org}).data)


class MatchRevisionListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, match_id):
        if not Match.objects.filter(org=request.org, id=match_id).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = MatchRevision.objects.filter(
            org=request.org,
            match_id=match_id,
        )
        return self.list_response(queryset, MatchRevisionSerializer, request)


class MatchDecisionEventListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, match_id):
        if not Match.objects.filter(org=request.org, id=match_id).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = MatchDecisionEvent.objects.filter(
            org=request.org,
            match_id=match_id,
        )
        return self.list_response(queryset, MatchDecisionEventSerializer, request)


class MatchingCapabilitiesView(APIView):
    """Return the current profile's effective matching capabilities."""

    permission_classes = (IsAuthenticated, HasOrgContext)

    def get(self, request):
        serializer = MatchingCapabilitiesSerializer(
            matching_capabilities(request.profile)
        )
        return Response(serializer.data)
