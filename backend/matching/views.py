"""Tenant-isolated API views for people, evidence, opportunities, and matches."""

from datetime import timedelta
from uuid import UUID

from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.models import MatchingAccessLevel
from common.permissions import HasOrgContext, IsOrgAdmin
from matching.crm_import import (
    crm_candidates_queryset,
    preview_crm_person_import,
    safe_crm_candidate,
)
from matching.decisions import MatchDecisionError, apply_match_decision
from matching.feedback import (
    OUTCOMES_BY_TYPE,
    MatchFeedbackError,
    feedback_insights,
    feedback_overview,
    record_match_feedback,
)
from matching.governance import (
    GovernanceError,
    contact_eligibility,
    ensure_evidence_provenance,
    export_person,
    governance_revision_for_org,
    mutate_person_governance,
    review_evidence,
    safe_governance_evidence,
    safe_intent,
    safe_person_governance,
    safe_provenance,
    scan_governance_retention,
    update_evidence_provenance,
    upsert_contact_intent,
)
from matching.import_pipeline import (
    PersonImportServiceError,
    commit_person_import,
    preview_person_import,
    resolve_person_import_record,
)
from matching.models import (
    Evidence,
    EvidenceConfirmationStatus,
    EvidenceProcessingStatus,
    EvidenceProvenance,
    EvidenceSource,
    Match,
    MatchDecisionEvent,
    MatchFeedbackEvent,
    MatchFeedbackEventKind,
    MatchOpportunity,
    MatchProjectionState,
    MatchRevision,
    MatchRun,
    MatchScoringPolicy,
    MatchScoringPolicyVersion,
    MatchWeightSuggestion,
    Person,
    PersonContactIntent,
    PersonGovernanceStatus,
    PersonIdentity,
    PersonImportBatch,
    PersonImportRecord,
    PersonImportRecordStatus,
    PersonStatus,
)
from matching.onboarding import PersonOnboardingConflict, onboard_person
from matching.permissions import HasMatchingAccess, matching_capabilities
from matching.scoring import (
    ScoringPolicyError,
    create_policy_draft,
    generate_weight_suggestion,
    publish_policy_version,
    reject_policy_version,
    review_weight_suggestion,
)
from matching.serializers import (
    AsyncRecomputeMatchesSerializer,
    ContactEligibilitySerializer,
    CRMImportCandidateQuerySerializer,
    CRMImportPreviewRequestSerializer,
    EvidenceGovernanceUpdateSerializer,
    EvidenceReviewSerializer,
    EvidenceSafeSerializer,
    EvidenceSerializer,
    FeedbackQueueMatchSerializer,
    MatchDecisionEventSerializer,
    MatchFeedbackEventSerializer,
    MatchFeedbackMutationSerializer,
    MatchingCapabilitiesSerializer,
    MatchOpportunitySerializer,
    MatchRevisionSerializer,
    MatchRunSerializer,
    MatchScoringPolicyDraftSerializer,
    MatchScoringPolicyMutationSerializer,
    MatchScoringPolicySerializer,
    MatchScoringPolicyVersionSerializer,
    MatchSerializer,
    MatchStatusSerializer,
    MatchWeightSuggestionSerializer,
    PersonContactIntentMutationSerializer,
    PersonDeletionSerializer,
    PersonExportSerializer,
    PersonIdentitySafeSerializer,
    PersonIdentitySerializer,
    PersonImportBatchSerializer,
    PersonImportCommitRequestSerializer,
    PersonImportDecisionSerializer,
    PersonImportPreviewRequestSerializer,
    PersonImportRecordSerializer,
    PersonImportResolveRequestSerializer,
    PersonOnboardingRequestSerializer,
    PersonOnboardingResponseSerializer,
    PersonSerializer,
    RetentionScanSerializer,
    WeightSuggestionGenerateSerializer,
    WeightSuggestionReviewSerializer,
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


class PersonOnboardingView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.MANAGE}

    def post(self, request):
        raw_key = request.headers.get("Idempotency-Key", "").strip()
        try:
            idempotency_key = UUID(raw_key)
        except (TypeError, ValueError):
            return Response(
                {"idempotency_key": ["A valid UUID Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PersonOnboardingRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = onboard_person(
                org=request.org,
                requested_by=request.profile,
                idempotency_key=idempotency_key,
                validated_data=serializer.validated_data,
            )
        except PersonOnboardingConflict as exc:
            return Response(exc.as_dict(), status=status.HTTP_409_CONFLICT)

        response = PersonOnboardingResponseSerializer(result.as_response())
        response_status = (
            status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        )
        return Response(response.data, status=response_status)


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
        return self.list_response(queryset, PersonIdentitySafeSerializer, request)

    def post(self, request):
        serializer = PersonIdentitySerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        identity = serializer.save(org=request.org)
        return Response(
            PersonIdentitySafeSerializer(identity, context={"org": request.org}).data,
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
        return self.list_response(queryset, EvidenceSafeSerializer, request)

    def post(self, request):
        serializer = EvidenceSerializer(data=request.data, context={"org": request.org})
        serializer.is_valid(raise_exception=True)
        evidence = serializer.save(org=request.org)
        ensure_evidence_provenance(
            evidence=evidence,
            actor=request.profile,
            collection_method="manual",
        )
        return Response(
            EvidenceSafeSerializer(evidence).data,
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
            Match.objects.filter(
                org=request.org,
                opportunity_id=opportunity_id,
                projection_state=MatchProjectionState.CURRENT,
                person__status=PersonStatus.ACTIVE,
                person__governance_status=PersonGovernanceStatus.ACTIVE,
            )
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
            Match.objects.filter(
                org=request.org,
                id=match_id,
                projection_state=MatchProjectionState.CURRENT,
                person__status=PersonStatus.ACTIVE,
                person__governance_status=PersonGovernanceStatus.ACTIVE,
            )
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


class MatchFeedbackMutationView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.DECIDE}
    event_kind = MatchFeedbackEventKind.RECOMMENDATION

    def post(self, request, match_id):
        if not Match.objects.filter(
            org=request.org,
            id=match_id,
            projection_state=MatchProjectionState.CURRENT,
            person__status=PersonStatus.ACTIVE,
            person__governance_status=PersonGovernanceStatus.ACTIVE,
        ).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        idempotency_key = _uuid_idempotency_key(request)
        if idempotency_key is None:
            return Response(
                {"code": "invalid_idempotency_key", "detail": "A valid UUID Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = MatchFeedbackMutationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = record_match_feedback(
                org=request.org,
                match_id=match_id,
                event_kind=self.event_kind,
                expected_feedback_revision=data["expected_revision"],
                expected_ranking_revision=data["expected_ranking_revision"],
                idempotency_key=idempotency_key,
                actor=request.profile,
                reason_code=data["reason_code"],
                occurred_at=data["occurred_at"],
                verdict=data.get("verdict", ""),
                outcome_code=data.get("outcome_code", ""),
                note=data.get("note", ""),
                action=data["action"],
                supersedes_id=data.get("supersedes_id"),
                source=data["source"],
                attributions=data.get("attributions", ()),
            )
        except MatchFeedbackError as exc:
            return Response(exc.as_dict(), status=exc.status_code)
        match = (
            Match.objects.filter(
                org=request.org,
                id=match_id,
                projection_state=MatchProjectionState.CURRENT,
                person__status=PersonStatus.ACTIVE,
                person__governance_status=PersonGovernanceStatus.ACTIVE,
            )
            .select_related("person", "opportunity")
            .prefetch_related("evidence_links__evidence")
            .get()
        )
        event = MatchFeedbackEvent.objects.prefetch_related("attributions").get(
            org=request.org, id=result.event.id
        )
        return Response(
            {
                "replayed": result.replayed,
                "match": MatchSerializer(match, context={"org": request.org}).data,
                "event": MatchFeedbackEventSerializer(event).data,
            },
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class MatchOutcomeMutationView(MatchFeedbackMutationView):
    event_kind = MatchFeedbackEventKind.OUTCOME


class MatchFeedbackEventListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, match_id):
        if not Match.objects.filter(org=request.org, id=match_id).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = MatchFeedbackEvent.objects.filter(
            org=request.org, match_id=match_id
        ).prefetch_related("attributions")
        kind = request.query_params.get("kind", "").strip()
        if kind:
            queryset = queryset.filter(event_kind=kind)
        return self.list_response(queryset, MatchFeedbackEventSerializer, request)


class FeedbackMatchListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request):
        queryset = (
            Match.objects.filter(
                org=request.org,
                projection_state=MatchProjectionState.CURRENT,
                person__status=PersonStatus.ACTIVE,
                person__governance_status=PersonGovernanceStatus.ACTIVE,
            )
            .select_related("person", "opportunity")
            .prefetch_related("evidence_links__evidence")
        )
        opportunity_type = (
            request.query_params.get("type", "")
            or request.query_params.get("opportunity_type", "")
        ).strip()
        verdict = request.query_params.get("verdict", "").strip()
        outcome = request.query_params.get("outcome", "").strip()
        queue = request.query_params.get("queue", "").strip()
        window = request.query_params.get("window", "").strip()
        if opportunity_type:
            queryset = queryset.filter(opportunity__opportunity_type=opportunity_type)
        if verdict:
            queryset = queryset.filter(recommendation_verdict=verdict)
        if outcome:
            queryset = queryset.filter(latest_outcome_code=outcome)
        if queue == "pending_feedback":
            queryset = queryset.filter(recommendation_verdict="unknown")
        elif queue == "reviewed":
            queryset = queryset.exclude(recommendation_verdict="unknown")
        elif queue == "has_outcome":
            queryset = queryset.exclude(latest_outcome_code="")
        if window:
            try:
                days = min(max(int(window), 1), 365)
            except (TypeError, ValueError):
                days = 30
            queryset = queryset.filter(evaluated_at__gte=timezone.now() - timedelta(days=days))
        return self.list_response(queryset, FeedbackQueueMatchSerializer, request)


class FeedbackMatchDetailView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, match_id):
        match = (
            Match.objects.filter(
                org=request.org,
                id=match_id,
                projection_state=MatchProjectionState.CURRENT,
                person__status=PersonStatus.ACTIVE,
                person__governance_status=PersonGovernanceStatus.ACTIVE,
            )
            .select_related("person", "opportunity")
            .prefetch_related("evidence_links__evidence")
            .first()
        )
        if match is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        events = MatchFeedbackEvent.objects.filter(
            org=request.org, match=match
        ).prefetch_related("attributions")[:100]
        event_list = list(events)
        feedback_events = [
            item
            for item in event_list
            if item.event_kind == MatchFeedbackEventKind.RECOMMENDATION
        ]
        outcome_events = [
            item for item in event_list if item.event_kind == MatchFeedbackEventKind.OUTCOME
        ]
        current_event = feedback_events[0] if feedback_events else None
        return Response(
            {
                "match": MatchSerializer(match, context={"org": request.org}).data,
                "current_feedback": {
                    "accuracy": match.recommendation_verdict,
                    "revision": match.feedback_revision,
                    "evidence_assessments": (
                        MatchFeedbackEventSerializer(current_event).data["attributions"]
                        if current_event
                        else []
                    ),
                },
                "outcomes": MatchFeedbackEventSerializer(outcome_events, many=True).data,
                "events": MatchFeedbackEventSerializer(event_list, many=True).data,
                "available_milestones": [
                    {"code": code, "label": code.replace("_", " ").title()}
                    for code in sorted(
                        OUTCOMES_BY_TYPE.get(match.opportunity.opportunity_type, set())
                    )
                ],
                "capabilities": matching_capabilities(request.profile),
            }
        )


class FeedbackOverviewView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request):
        data = feedback_overview(
                org=request.org,
                opportunity_type=request.query_params.get("opportunity_type", "").strip(),
            )
        data["pending_suggestions"] = MatchWeightSuggestion.objects.filter(
            org=request.org, status="pending"
        ).count()
        return Response(data)


class FeedbackInsightsView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request):
        return Response(
            feedback_insights(
                org=request.org,
                opportunity_type=request.query_params.get("opportunity_type", "").strip(),
            )
        )


class ScoringPolicyListCreateView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }

    def get(self, request):
        queryset = MatchScoringPolicy.objects.filter(org=request.org).select_related(
            "active_version"
        ).prefetch_related("active_version__events")
        opportunity_type = request.query_params.get("opportunity_type", "").strip()
        if opportunity_type:
            queryset = queryset.filter(opportunity_type=opportunity_type)
        return self.list_response(queryset, MatchScoringPolicySerializer, request)

    def post(self, request):
        key = _uuid_idempotency_key(request)
        if key is None:
            return Response(
                {"code": "invalid_idempotency_key", "detail": "A valid UUID Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = MatchScoringPolicyDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = create_policy_draft(
                org=request.org,
                actor=request.profile,
                idempotency_key=key,
                **serializer.validated_data,
            )
        except ScoringPolicyError as exc:
            return Response(exc.as_dict(), status=exc.status_code)
        return Response(
            {
                "replayed": result.replayed,
                "policy": MatchScoringPolicySerializer(result.policy).data,
                "version": MatchScoringPolicyVersionSerializer(result.version).data,
            },
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class ScoringPolicyVersionListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, policy_id):
        if not MatchScoringPolicy.objects.filter(org=request.org, id=policy_id).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = MatchScoringPolicyVersion.objects.filter(
            org=request.org, policy_id=policy_id
        ).prefetch_related("events")
        return self.list_response(queryset, MatchScoringPolicyVersionSerializer, request)


class AdminMatchingAPIView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)


class ScoringPolicyVersionPublishView(AdminMatchingAPIView):
    def post(self, request, version_id):
        key = _uuid_idempotency_key(request)
        if key is None:
            return Response(
                {"code": "invalid_idempotency_key", "detail": "A valid UUID Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = MatchScoringPolicyMutationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if data["action"] == "reject":
                result = reject_policy_version(
                    org=request.org,
                    version_id=version_id,
                    expected_revision=data["expected_revision"],
                    idempotency_key=key,
                    actor=request.profile,
                    reason_code=data["reason_code"],
                )
            else:
                result = publish_policy_version(
                    org=request.org,
                    version_id=version_id,
                    expected_revision=data["expected_revision"],
                    idempotency_key=key,
                    actor=request.profile,
                )
        except ScoringPolicyError as exc:
            return Response(exc.as_dict(), status=exc.status_code)
        return Response(
            {
                "replayed": result.replayed,
                "policy": MatchScoringPolicySerializer(result.policy).data,
                "version": MatchScoringPolicyVersionSerializer(result.version).data,
            },
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class WeightSuggestionListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request):
        queryset = MatchWeightSuggestion.objects.filter(org=request.org)
        status_filter = request.query_params.get("status", "").strip()
        opportunity_type = request.query_params.get("opportunity_type", "").strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if opportunity_type:
            queryset = queryset.filter(opportunity_type=opportunity_type)
        return self.list_response(queryset, MatchWeightSuggestionSerializer, request)


class WeightSuggestionGenerateView(AdminMatchingAPIView):

    def post(self, request):
        key = _uuid_idempotency_key(request)
        if key is None:
            return Response(
                {"code": "invalid_idempotency_key", "detail": "A valid UUID Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = WeightSuggestionGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            suggestion, replayed = generate_weight_suggestion(
                org=request.org,
                actor=request.profile,
                idempotency_key=key,
                **serializer.validated_data,
            )
        except ScoringPolicyError as exc:
            return Response(exc.as_dict(), status=exc.status_code)
        return Response(
            {
                "replayed": replayed,
                "suggestion": MatchWeightSuggestionSerializer(suggestion).data,
            },
            status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
        )


class WeightSuggestionReviewView(AdminMatchingAPIView):
    def post(self, request, suggestion_id):
        key = _uuid_idempotency_key(request)
        if key is None:
            return Response(
                {"code": "invalid_idempotency_key", "detail": "A valid UUID Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = WeightSuggestionReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = review_weight_suggestion(
                org=request.org,
                suggestion_id=suggestion_id,
                actor=request.profile,
                idempotency_key=key,
                **serializer.validated_data,
            )
        except ScoringPolicyError as exc:
            return Response(exc.as_dict(), status=exc.status_code)
        return Response(
            {
                "replayed": result.replayed,
                "suggestion": MatchWeightSuggestionSerializer(result.suggestion).data,
                "draft": (
                    MatchScoringPolicyVersionSerializer(result.draft).data
                    if result.draft
                    else None
                ),
            },
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class MatchingCapabilitiesView(APIView):
    """Return the current profile's effective matching capabilities."""

    permission_classes = (IsAuthenticated, HasOrgContext)

    def get(self, request):
        serializer = MatchingCapabilitiesSerializer(
            matching_capabilities(request.profile)
        )
        return Response(serializer.data)


def _uuid_idempotency_key(request):
    raw_key = request.headers.get("Idempotency-Key", "").strip()
    try:
        return UUID(raw_key)
    except (TypeError, ValueError):
        return None


def _governance_error_response(exc):
    return Response(exc.as_dict(), status=exc.status_code)


def _governance_key(request, validated_data):
    key = _uuid_idempotency_key(request)
    if key is None:
        raise GovernanceError(
            code="invalid_idempotency_key",
            detail="A valid UUID Idempotency-Key header is required.",
        )
    body_key = validated_data.pop("idempotency_key", None)
    if body_key is not None and body_key != key:
        raise GovernanceError(
            code="idempotency_key_mismatch",
            detail="The body and header Idempotency-Key values must match.",
            status_code=409,
        )
    return key


def _governance_people_queryset(org):
    return Person.objects.filter(org=org).prefetch_related(
        Prefetch(
            "evidence",
            queryset=Evidence.objects.select_related("provenance").order_by(
                "-observed_at"
            ),
        ),
        Prefetch(
            "contact_intents",
            queryset=PersonContactIntent.objects.select_related("identity").order_by(
                "channel",
                "purpose",
            ),
        ),
    )


class GovernancePersonListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request):
        queryset = _governance_people_queryset(request.org)
        query = request.query_params.get("q", "").strip()[:100]
        source = request.query_params.get("source", "").strip()
        kind = request.query_params.get("kind", "").strip()
        queue = request.query_params.get("queue", "").strip()
        if query:
            queryset = queryset.filter(
                Q(display_name__icontains=query)
                | Q(current_title__icontains=query)
                | Q(current_company__icontains=query)
            )
        if source:
            queryset = queryset.filter(evidence__source=source)
        if kind:
            queryset = queryset.filter(evidence__kind=kind)
        now = timezone.now()
        if queue == "pending_ai":
            queryset = queryset.filter(
                evidence__source="ai",
                evidence__provenance__confirmation_status=(
                    EvidenceConfirmationStatus.PENDING
                ),
            )
        elif queue == "expiring":
            cutoff = now + timedelta(days=30)
            queryset = queryset.filter(
                Q(evidence__valid_until__gt=now, evidence__valid_until__lte=cutoff)
                | Q(
                    evidence__provenance__retention_until__gt=now,
                    evidence__provenance__retention_until__lte=cutoff,
                )
            )
        elif queue == "expired":
            queryset = queryset.filter(
                Q(evidence__valid_until__lte=now)
                | Q(evidence__provenance__retention_until__lte=now)
            )
        elif queue == "blocked":
            queryset = queryset.filter(
                Q(
                    evidence__provenance__processing_status__in=[
                        EvidenceProcessingStatus.RESTRICTED,
                        EvidenceProcessingStatus.DELETION_REQUESTED,
                        EvidenceProcessingStatus.ANONYMIZED,
                    ]
                )
                | ~Q(governance_status=PersonGovernanceStatus.ACTIVE)
            )
        elif queue == "deletion_requested":
            queryset = queryset.filter(
                governance_status=PersonGovernanceStatus.DELETION_REQUESTED
            )
        elif queue:
            return Response(
                {"queue": ["Unknown governance queue."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.distinct().order_by("display_name", "id")
        count = queryset.count()
        results = [
            safe_person_governance(person)
            for person in queryset[: self.limit(request)]
        ]
        now = timezone.now()
        summary = {
            "total": Person.objects.filter(org=request.org).count(),
            "pending_ai": EvidenceProvenance.objects.filter(
                org=request.org,
                evidence__source="ai",
                confirmation_status=EvidenceConfirmationStatus.PENDING,
            ).count(),
            "expiring": EvidenceProvenance.objects.filter(org=request.org)
            .filter(
                Q(evidence__valid_until__gt=now, evidence__valid_until__lte=now + timedelta(days=30))
                | Q(retention_until__gt=now, retention_until__lte=now + timedelta(days=30))
            )
            .count(),
            "expired": EvidenceProvenance.objects.filter(org=request.org)
            .filter(Q(evidence__valid_until__lte=now) | Q(retention_until__lte=now))
            .count(),
            "blocked": EvidenceProvenance.objects.filter(org=request.org)
            .exclude(processing_status=EvidenceProcessingStatus.ACTIVE)
            .count(),
            "deletion_requested": Person.objects.filter(
                org=request.org,
                governance_status=PersonGovernanceStatus.DELETION_REQUESTED,
            ).count(),
            "revision": governance_revision_for_org(request.org),
        }
        return Response(
            {
                "count": count,
                "summary": summary,
                "results": results,
                "capabilities": matching_capabilities(request.profile),
            }
        )


class GovernancePersonDetailView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, person_id):
        person = _governance_people_queryset(request.org).filter(id=person_id).first()
        if person is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        data = safe_person_governance(person)
        data["evidence"] = [
            safe_governance_evidence(item.provenance)
            for item in person.evidence.all()
            if hasattr(item, "provenance")
        ]
        data["capabilities"] = matching_capabilities(request.profile)
        return Response(data)


class EvidenceGovernanceView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "PATCH": MatchingAccessLevel.MANAGE,
    }

    @staticmethod
    def _get(request, evidence_id):
        return (
            EvidenceProvenance.objects.filter(
                org=request.org,
                evidence_id=evidence_id,
            )
            .select_related("evidence")
            .first()
        )

    def get(self, request, evidence_id):
        provenance = self._get(request, evidence_id)
        if provenance is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(safe_provenance(provenance))

    def patch(self, request, evidence_id):
        serializer = EvidenceGovernanceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            key = _governance_key(request, data)
            expected_revision = data.pop("expected_revision")
            result = update_evidence_provenance(
                org=request.org,
                evidence_id=evidence_id,
                actor=request.profile,
                idempotency_key=key,
                expected_revision=expected_revision,
                changes=data,
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        return Response(
            {
                "governance": safe_provenance(result.value),
                "replayed": result.replayed,
                "match_run_ids": [str(value) for value in result.match_run_ids],
            }
        )


class EvidenceReviewView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.MANAGE}

    def post(self, request, evidence_id):
        serializer = EvidenceReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            key = _governance_key(request, data)
            result = review_evidence(
                org=request.org,
                evidence_id=evidence_id,
                actor=request.profile,
                idempotency_key=key,
                expected_revision=data["expected_revision"],
                action=data["action"],
                reason_code=data["reason_code"],
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        return Response(
            {
                "evidence": safe_governance_evidence(result.value),
                "replayed": result.replayed,
                "match_run_ids": [str(value) for value in result.match_run_ids],
            }
        )


class PersonContactIntentView(MatchingAPIView):
    matching_access_by_method = {
        "GET": MatchingAccessLevel.READ,
        "POST": MatchingAccessLevel.MANAGE,
    }

    def get(self, request, person_id):
        if not Person.objects.filter(org=request.org, id=person_id).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = PersonContactIntent.objects.filter(
            org=request.org,
            person_id=person_id,
        ).select_related("identity").order_by("channel", "purpose")
        return Response(
            {
                "count": queryset.count(),
                "results": [safe_intent(item) for item in queryset[: self.limit(request)]],
            }
        )

    def post(self, request, person_id):
        serializer = PersonContactIntentMutationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            key = _governance_key(request, data)
            result = upsert_contact_intent(
                org=request.org,
                person_id=person_id,
                actor=request.profile,
                idempotency_key=key,
                **data,
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        return Response(
            {
                "intent": safe_intent(result.value),
                "replayed": result.replayed,
                "match_run_ids": [],
            },
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class PersonContactEligibilityView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.READ}

    def post(self, request, person_id):
        serializer = ContactEligibilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            key = _governance_key(request, data)
            result = contact_eligibility(
                org=request.org,
                person_id=person_id,
                idempotency_key=key,
                **data,
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        return Response(result)


class GovernanceAdminAPIView(MatchingAPIView):
    permission_classes = (
        IsAuthenticated,
        HasOrgContext,
        HasMatchingAccess,
        IsOrgAdmin,
    )


class PersonExportView(GovernanceAdminAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.READ}

    def post(self, request, person_id):
        serializer = PersonExportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            key = _governance_key(request, data)
            result = export_person(
                org=request.org,
                person_id=person_id,
                actor=request.profile,
                idempotency_key=key,
                expected_revision=data["expected_revision"],
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        return Response({"export": result.value, "replayed": result.replayed})


class PersonDeletionView(GovernanceAdminAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.READ}

    def post(self, request, person_id):
        serializer = PersonDeletionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            key = _governance_key(request, data)
            confirmation = data.pop("confirm_person_id", None)
            if data["action"] == "anonymize" and confirmation != person_id:
                raise GovernanceError(
                    code="person_confirmation_mismatch",
                    detail="confirm_person_id must match the person being anonymized.",
                )
            result = mutate_person_governance(
                org=request.org,
                person_id=person_id,
                actor=request.profile,
                idempotency_key=key,
                expected_revision=data["expected_revision"],
                action=data["action"],
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        person = _governance_people_queryset(request.org).get(id=person_id)
        payload = safe_person_governance(person)
        payload["replayed"] = result.replayed
        payload["match_run_ids"] = [str(value) for value in result.match_run_ids]
        return Response(payload)


class RetentionScanView(GovernanceAdminAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.READ}

    def post(self, request):
        serializer = RetentionScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            _governance_key(request, data)
            expected_revision = data.pop("expected_revision")
            if expected_revision != governance_revision_for_org(request.org):
                raise GovernanceError(
                    code="governance_revision_conflict",
                    detail="Governance data changed; refresh before scanning retention.",
                    status_code=409,
                )
            result = scan_governance_retention(
                org=request.org,
                execute=data["execute"],
                limit=data["limit"],
                actor=request.profile,
            )
        except GovernanceError as exc:
            return _governance_error_response(exc)
        return Response(result)


def _import_error_response(exc):
    conflict_codes = {
        "import_idempotency_conflict",
        "import_already_committed",
        "import_revision_conflict",
        "decision_idempotency_conflict",
        "conflict_not_open",
        "source_record_conflict_requires_skip",
    }
    response_status = (
        status.HTTP_409_CONFLICT
        if exc.code in conflict_codes
        else status.HTTP_400_BAD_REQUEST
    )
    return Response(exc.as_dict(), status=response_status)


class PersonImportPreviewView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.MANAGE}
    parser_classes = (MultiPartParser,)

    def post(self, request):
        idempotency_key = _uuid_idempotency_key(request)
        if idempotency_key is None:
            return Response(
                {"idempotency_key": ["A valid UUID Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = PersonImportPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = serializer.validated_data["file"]
        file_bytes = upload.read()
        try:
            result = preview_person_import(
                org=request.org,
                requested_by=request.profile,
                idempotency_key=idempotency_key,
                file_bytes=file_bytes,
                filename=upload.name,
                mapping=serializer.validated_data["mapping"],
            )
        except PersonImportServiceError as exc:
            return _import_error_response(exc)
        data = PersonImportBatchSerializer(result.batch).data
        data["replayed"] = result.replayed
        data["records"] = PersonImportRecordSerializer(
            result.batch.records.select_related("batch", "person", "conflict"),
            many=True,
        ).data
        return Response(
            data,
            status=(status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED),
        )


class CRMImportCandidateListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.MANAGE}

    def get(self, request):
        serializer = CRMImportCandidateQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        queryset = crm_candidates_queryset(
            org=request.org,
            entity_type=data["entity_type"],
            search=data.get("search", ""),
        )
        count = queryset.count()
        page = data["page"]
        page_size = data["page_size"]
        offset = (page - 1) * page_size
        return Response(
            {
                "count": count,
                "page": page,
                "page_size": page_size,
                "results": [
                    safe_crm_candidate(item, entity_type=data["entity_type"])
                    for item in queryset[offset : offset + page_size]
                ],
            }
        )


class CRMImportPreviewView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.MANAGE}

    def post(self, request):
        idempotency_key = _uuid_idempotency_key(request)
        if idempotency_key is None:
            return Response(
                {"idempotency_key": ["A valid UUID Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = CRMImportPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = preview_crm_person_import(
                org=request.org,
                requested_by=request.profile,
                idempotency_key=idempotency_key,
                entity_type=data["entity_type"],
                record_ids=data["record_ids"],
            )
        except LookupError:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except PersonImportServiceError as exc:
            return _import_error_response(exc)
        response_data = PersonImportBatchSerializer(result.batch).data
        response_data["replayed"] = result.replayed
        response_data["records"] = PersonImportRecordSerializer(
            result.batch.records.select_related("batch", "person", "conflict"), many=True
        ).data
        return Response(
            response_data,
            status=(status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED),
        )


class PersonImportBatchListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request):
        queryset = PersonImportBatch.objects.filter(org=request.org).select_related(
            "automation_job"
        )
        status_filter = request.query_params.get("status", "").strip()
        source_filter = request.query_params.get("source", "").strip().lower()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if source_filter:
            if source_filter not in EvidenceSource.values:
                return Response(
                    {"source": ["Unknown import source."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(source=source_filter)
        return self.list_response(queryset, PersonImportBatchSerializer, request)


class PersonImportBatchDetailView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, batch_id):
        batch = PersonImportBatch.objects.filter(
            org=request.org,
            id=batch_id,
        ).select_related("automation_job").first()
        if batch is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(PersonImportBatchSerializer(batch).data)


class PersonImportRecordListView(MatchingAPIView):
    matching_access_by_method = {"GET": MatchingAccessLevel.READ}

    def get(self, request, batch_id):
        if not PersonImportBatch.objects.filter(org=request.org, id=batch_id).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = PersonImportRecord.objects.filter(
            org=request.org,
            batch_id=batch_id,
        ).select_related("batch", "person", "conflict")
        status_filter = request.query_params.get("status", "").strip()
        valid_statuses = {value for value, _label in PersonImportRecordStatus.choices}
        if status_filter:
            if status_filter not in valid_statuses:
                return Response(
                    {"status": ["Unknown import record status."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(status=status_filter)
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 500)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return Response(
                {"pagination": ["limit and offset must be integers."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        records = list(queryset[offset : offset + limit])
        candidate_ids = {
            str(person_id)
            for record in records
            if hasattr(record, "conflict")
            for person_id in record.conflict.person_ids
        }
        candidate_people = {
            str(person.id): person
            for person in Person.objects.filter(org=request.org, id__in=candidate_ids)
        }
        return Response(
            {
                "count": queryset.count(),
                "results": PersonImportRecordSerializer(
                    records,
                    many=True,
                    context={"candidate_people": candidate_people},
                ).data,
            }
        )


class PersonImportCommitView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.MANAGE}

    def post(self, request, batch_id):
        idempotency_key = _uuid_idempotency_key(request)
        if idempotency_key is None:
            return Response(
                {"idempotency_key": ["A valid UUID Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = PersonImportCommitRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = PersonImportBatch.objects.filter(org=request.org, id=batch_id).first()
        if batch is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            result = commit_person_import(
                org=request.org,
                requested_by=request.profile,
                batch=batch,
                expected_revision=serializer.validated_data["expected_revision"],
                idempotency_key=idempotency_key,
            )
        except PersonImportServiceError as exc:
            return _import_error_response(exc)
        data = PersonImportBatchSerializer(
            result.batch,
            context={"request": request},
        ).data
        data["replayed"] = result.replayed
        return Response(
            data,
            status=status.HTTP_202_ACCEPTED,
            headers={"Retry-After": "2"},
        )


class PersonImportRecordResolveView(MatchingAPIView):
    matching_access_by_method = {"POST": MatchingAccessLevel.MANAGE}

    def post(self, request, record_id):
        idempotency_key = _uuid_idempotency_key(request)
        if idempotency_key is None:
            return Response(
                {"idempotency_key": ["A valid UUID Idempotency-Key is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = PersonImportResolveRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = PersonImportRecord.objects.filter(org=request.org, id=record_id).first()
        if record is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            result = resolve_person_import_record(
                org=request.org,
                actor=request.profile,
                record=record,
                action=serializer.validated_data["action"],
                person_id=serializer.validated_data.get("person_id"),
                expected_revision=serializer.validated_data["expected_revision"],
                idempotency_key=idempotency_key,
            )
        except PersonImportServiceError as exc:
            return _import_error_response(exc)
        return Response(
            {
                "replayed": result.replayed,
                "record": PersonImportRecordSerializer(result.record).data,
                "decision": PersonImportDecisionSerializer(result.decision).data,
            },
            status=(status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED),
        )
