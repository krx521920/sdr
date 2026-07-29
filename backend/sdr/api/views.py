from uuid import uuid4

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, IsOrgAdmin
from common.utils import COUNTRIES
from sdr.api.serializers import (
    LeadInspectionSerializer,
    SDRIntelligenceSettingsSerializer,
    SDRRoutingPreviewSerializer,
    SDRRoutingRuleSerializer,
)
from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.models import (
    LeadInspection,
    LeadInspectionStatus,
    LeadIntakeSource,
    SDRIntelligenceSettings,
    SDRRoutingRule,
    SDRRoutingStrategy,
)
from sdr.routing import RuleBasedSalesRouter


class SDRRoutingRuleListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Routing"], responses={200: SDRRoutingRuleSerializer(many=True)}
    )
    def get(self, request):
        rules = (
            SDRRoutingRule.objects.filter(org=request.org)
            .prefetch_related("members__profile__user")
            .order_by("priority", "created_at", "id")
        )
        return Response(
            {
                "rules": SDRRoutingRuleSerializer(rules, many=True).data,
                "countries": [
                    {"code": code, "name": str(name)} for code, name in COUNTRIES
                ],
                "sources": [
                    {"value": value, "label": label}
                    for value, label in LeadIntakeSource.choices
                ],
                "qualification_bands": [
                    {"value": band.value, "label": band.value.title()}
                    for band in QualificationBand
                ],
                "strategies": [
                    {"value": value, "label": label}
                    for value, label in SDRRoutingStrategy.choices
                ],
            }
        )

    @extend_schema(
        tags=["SDR Routing"],
        request=SDRRoutingRuleSerializer,
        responses={201: SDRRoutingRuleSerializer},
    )
    def post(self, request):
        serializer = SDRRoutingRuleSerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        rule = serializer.save(org=request.org)
        rule = SDRRoutingRule.objects.prefetch_related("members__profile__user").get(
            id=rule.id, org=request.org
        )
        return Response(
            SDRRoutingRuleSerializer(rule).data,
            status=status.HTTP_201_CREATED,
        )


class SDRRoutingRuleDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _get_rule(request, rule_id):
        return (
            SDRRoutingRule.objects.filter(id=rule_id, org=request.org)
            .prefetch_related("members__profile__user")
            .first()
        )

    @extend_schema(tags=["SDR Routing"], responses={200: SDRRoutingRuleSerializer})
    def get(self, request, rule_id):
        rule = self._get_rule(request, rule_id)
        if rule is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(SDRRoutingRuleSerializer(rule).data)

    def put(self, request, rule_id):
        return self._update(request, rule_id, partial=False)

    def patch(self, request, rule_id):
        return self._update(request, rule_id, partial=True)

    def _update(self, request, rule_id, *, partial):
        rule = self._get_rule(request, rule_id)
        if rule is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDRRoutingRuleSerializer(
            rule,
            data=request.data,
            partial=partial,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        rule = self._get_rule(request, rule.id)
        return Response(SDRRoutingRuleSerializer(rule).data)

    def delete(self, request, rule_id):
        rule = self._get_rule(request, rule_id)
        if rule is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SDRRoutingPreviewView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Routing"], request=SDRRoutingPreviewSerializer)
    def post(self, request):
        serializer = SDRRoutingPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = LeadSource(serializer.validated_data["source"])
        band = QualificationBand(serializer.validated_data["qualification_band"])
        candidate = LeadCandidate(
            org_id=request.org.id,
            source=source,
            source_record_id=f"preview-{uuid4()}",
            identity=LeadIdentity(),
            company=CompanySnapshot(country=serializer.validated_data.get("country")),
        )
        decision = RuleBasedSalesRouter().preview(
            candidate,
            QualificationResult(score=50, band=band),
        )
        return Response(
            {
                "matched": decision.rule_id is not None,
                "routing_rule_id": decision.rule_id,
                "assigned_profile_id": decision.profile_id,
                "reason": decision.reason,
            }
        )


class SDRIntelligenceSettingsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _get_configuration(request):
        default_model = (
            "gpt-5.6-luna"
            if "gpt-5.6-luna" in settings.OPENAI_ALLOWED_MODELS
            else settings.OPENAI_ALLOWED_MODELS[0]
        )
        default_effort = (
            "low"
            if "low" in settings.OPENAI_ALLOWED_REASONING_EFFORTS
            else settings.OPENAI_ALLOWED_REASONING_EFFORTS[0]
        )
        configuration, _ = SDRIntelligenceSettings.objects.get_or_create(
            org=request.org,
            defaults={
                "model": default_model,
                "reasoning_effort": default_effort,
            },
        )
        return configuration

    @staticmethod
    def _response(configuration):
        return SDRIntelligenceSettingsSerializer(configuration).data

    @extend_schema(
        tags=["SDR Intelligence"],
        responses={200: SDRIntelligenceSettingsSerializer},
    )
    def get(self, request):
        return Response(self._response(self._get_configuration(request)))

    @extend_schema(
        tags=["SDR Intelligence"],
        request=SDRIntelligenceSettingsSerializer,
        responses={200: SDRIntelligenceSettingsSerializer},
    )
    def put(self, request):
        return self._update(request, partial=False)

    def patch(self, request):
        return self._update(request, partial=True)

    def _update(self, request, *, partial):
        configuration = self._get_configuration(request)
        serializer = SDRIntelligenceSettingsSerializer(
            configuration,
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        configuration = serializer.save()
        return Response(self._response(configuration))


class LeadInspectionListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Intelligence"],
        responses={200: LeadInspectionSerializer(many=True)},
    )
    def get(self, request):
        queryset = LeadInspection.objects.filter(org=request.org).select_related(
            "intake__crm_lead"
        )
        status_filter = request.query_params.get("status", "").strip()
        if status_filter in LeadInspectionStatus.values:
            queryset = queryset.filter(status=status_filter)
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 100))
        count = queryset.count()
        inspections = queryset.order_by("-created_at")[:limit]
        return Response(
            {
                "count": count,
                "results": LeadInspectionSerializer(inspections, many=True).data,
            }
        )


class LeadInspectionDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Intelligence"], responses={200: LeadInspectionSerializer})
    def get(self, request, inspection_id):
        inspection = (
            LeadInspection.objects.filter(id=inspection_id, org=request.org)
            .select_related("intake__crm_lead")
            .first()
        )
        if inspection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(LeadInspectionSerializer(inspection).data)
