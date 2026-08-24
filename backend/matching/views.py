"""Tenant-isolated API views for people, evidence, opportunities, and matches."""

from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, HasSalesAccess
from matching.models import Evidence, Match, MatchOpportunity, Person, PersonIdentity
from matching.serializers import (
    EvidenceSerializer,
    MatchOpportunitySerializer,
    MatchSerializer,
    MatchStatusSerializer,
    PersonIdentitySerializer,
    PersonSerializer,
    RecomputeMatchesSerializer,
)
from matching.services import MAX_SYNC_RECOMPUTE_PEOPLE, recompute_opportunity_matches


class MatchingAPIView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, HasSalesAccess)

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
        opportunity = self.get_opportunity(request, opportunity_id)
        if opportunity is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = RecomputeMatchesSerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        person_ids = serializer.validated_data.get("person_ids")
        if person_ids is None:
            active_people = Person.objects.filter(org=request.org, status="active")
            active_people_count = active_people[
                : MAX_SYNC_RECOMPUTE_PEOPLE + 1
            ].count()
            if active_people_count > MAX_SYNC_RECOMPUTE_PEOPLE:
                return Response(
                    {
                        "person_ids": [
                            "Synchronous recompute is limited to "
                            f"{MAX_SYNC_RECOMPUTE_PEOPLE} people; provide an explicit subset."
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        people = (
            Person.objects.filter(org=request.org, id__in=person_ids)
            if person_ids is not None
            else None
        )
        try:
            matches = recompute_opportunity_matches(
                org=request.org,
                opportunity=opportunity,
                people=people,
            )
        except ValueError as exc:
            return Response(
                {"person_ids": [str(exc)]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        match_ids = [item.id for item in matches]
        matches = (
            Match.objects.filter(org=request.org, id__in=match_ids)
            .select_related("person", "opportunity")
            .prefetch_related("evidence_links__evidence")
        )
        ordered = sorted(matches, key=lambda item: item.rank or 0)
        return Response(
            {
                "count": len(ordered),
                "results": MatchSerializer(
                    ordered,
                    many=True,
                    context={"org": request.org},
                ).data,
            },
            status=status.HTTP_200_OK,
        )


class MatchDetailView(MatchingAPIView):
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
        match = self.get_object(request, match_id)
        if match is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = MatchStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match.status = serializer.validated_data["status"]
        match.save(update_fields=["status", "updated_at", "updated_by"])
        return Response(MatchSerializer(match, context={"org": request.org}).data)
