from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, HasSalesAccess, IsOrgAdmin
from common.utils import COUNTRIES
from sdr.analytics import build_sdr_analytics
from sdr.api.serializers import (
    LeadInspectionSerializer,
    LeadIntakeOperationsSerializer,
    LeadNurtureEnrollmentActionSerializer,
    LeadNurtureEnrollmentSerializer,
    SDRAICallAuditSerializer,
    SDRAnalyticsQuerySerializer,
    SDRApolloCandidateSerializer,
    SDRChannelComplianceRuleSerializer,
    SDRComplianceDeletionActionSerializer,
    SDRComplianceEventSerializer,
    SDRComplianceRetentionScanSerializer,
    SDRComplianceSettingsSerializer,
    SDRDataProvenanceSerializer,
    SDRDoNotContactCreateSerializer,
    SDRDoNotContactSerializer,
    SDREmailExecutionApprovalSerializer,
    SDREmailSuppressionCreateSerializer,
    SDREmailSuppressionSerializer,
    SDRIntelligenceSettingsSerializer,
    SDRNurtureSequenceSerializer,
    SDROutboundCampaignActionSerializer,
    SDROutboundCampaignSerializer,
    SDROutboundCopyDraftActionSerializer,
    SDROutboundCopyDraftEditSerializer,
    SDROutboundCopyDraftSerializer,
    SDROutboundCopyGenerateSerializer,
    SDROutboundImportSerializer,
    SDROutboundProspectActionSerializer,
    SDROutboundProspectSerializer,
    SDROutboundSourceSerializer,
    SDROutboundSourceSyncRequestSerializer,
    SDRResponseSettingsSerializer,
    SDRRoutingPreviewSerializer,
    SDRRoutingRuleSerializer,
    SDRSalesFeedbackSerializer,
)
from sdr.compliance import (
    anonymize_intake,
    block_contact,
    cancel_intake_deletion,
    release_contact_block,
    request_intake_deletion,
    scan_retention,
)
from sdr.domain import (
    CompanySnapshot,
    LeadCandidate,
    LeadIdentity,
    LeadSource,
    QualificationBand,
    QualificationResult,
)
from sdr.email_execution import EMAIL_SEND_ACTION, reserve_email_send
from sdr.email_safety import clear_campaign_safety_hold
from sdr.models import (
    ApolloCandidateStatus,
    EmailSuppressionReason,
    EmailSuppressionSource,
    LeadDelivery,
    LeadDeliveryKind,
    LeadDeliveryStatus,
    LeadInspection,
    LeadInspectionStatus,
    LeadIntake,
    LeadIntakeSource,
    LeadIntakeStatus,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    NurtureReplySentiment,
    OutboundCampaignStatus,
    OutboundProspectStatus,
    SDRAICallAudit,
    SDRApolloCandidate,
    SDRChannelComplianceRule,
    SDRComplianceChannel,
    SDRComplianceEvent,
    SDRComplianceSettings,
    SDRDataProvenance,
    SDRDoNotContactEntry,
    SDRDoNotContactReason,
    SDRDoNotContactSource,
    SDREmailSuppression,
    SDRIntelligenceSettings,
    SDRNurtureSequence,
    SDROutboundCampaign,
    SDROutboundCopyDraft,
    SDROutboundProspect,
    SDROutboundSource,
    SDRResponseSettings,
    SDRRoutingRule,
    SDRRoutingStrategy,
    SDRSalesFeedback,
)
from sdr.nurture import (
    enqueue_approved_nurture_delivery,
    nurture_email_execution_intent,
    pause_enrollment,
    resume_enrollment,
    stop_enrollment,
)
from sdr.outbound import (
    OutboundCampaignExecutionError,
    OutboundImportError,
    OutboundProspectUnavailable,
    enqueue_outbound_prospect,
    finish_outbound_campaign,
    import_prospect_csv,
    launch_outbound_campaign,
    pause_outbound_campaign,
    restore_outbound_prospect,
    retry_failed_outbound_work,
)
from sdr.outbound_analytics import build_outbound_campaign_analytics
from sdr.outbound_copy import (
    OutboundCopyUnavailable,
    apply_outbound_copy_draft,
    enqueue_outbound_copy_generation,
)
from sdr.provider_ports import (
    ExecutionChannel,
    ExecutionSafetyError,
    ExternalRequestStatus,
)
from sdr.response import (
    acknowledgement_email_execution_intent,
    enqueue_approved_acknowledgement_delivery,
)
from sdr.routing import RuleBasedSalesRouter
from sdr.sales_feedback import feedback_choices
from sdr.sources import (
    OutboundSourceUnavailable,
    apollo_enrichment_execution_intent,
    apollo_search_execution_intent,
    enqueue_apollo_candidate_enrichment,
    enqueue_outbound_source_sync,
)
from sdr.suppression import release_suppression, suppress_email


class SDRAnalyticsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Analytics"],
        parameters=[SDRAnalyticsQuerySerializer],
    )
    def get(self, request):
        serializer = SDRAnalyticsQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(
            build_sdr_analytics(
                org=request.org,
                days=serializer.validated_data["days"],
            )
        )


class SDRComplianceOverviewView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Compliance"])
    def get(self, request):
        configuration, _ = SDRComplianceSettings.objects.get_or_create(org=request.org)
        provenance = SDRDataProvenance.objects.filter(org=request.org)
        dnc = SDRDoNotContactEntry.objects.filter(org=request.org)
        events = SDRComplianceEvent.objects.filter(org=request.org)[:20]
        return Response(
            {
                "settings": SDRComplianceSettingsSerializer(configuration).data,
                "summary": {
                    "provenance_records": provenance.count(),
                    "unassessed": provenance.filter(lawful_basis="unassessed").count(),
                    "retention_due": provenance.filter(status="retention_due").count(),
                    "deletion_requested": provenance.filter(
                        status="deletion_requested"
                    ).count(),
                    "active_dnc": dnc.filter(is_active=True).count(),
                    "blocked_decisions": SDRComplianceEvent.objects.filter(
                        org=request.org,
                        event_type="contact_blocked",
                    ).count(),
                },
                "choices": {
                    "channels": [
                        {"value": value, "label": label}
                        for value, label in SDRComplianceChannel.choices
                    ],
                    "dnc_reasons": [
                        {"value": value, "label": label}
                        for value, label in SDRDoNotContactReason.choices
                    ],
                    "dnc_sources": [
                        {"value": value, "label": label}
                        for value, label in SDRDoNotContactSource.choices
                    ],
                },
                "recent_events": SDRComplianceEventSerializer(events, many=True).data,
            }
        )


class SDRComplianceSettingsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _get(request):
        configuration, _ = SDRComplianceSettings.objects.get_or_create(org=request.org)
        return configuration

    def get(self, request):
        return Response(SDRComplianceSettingsSerializer(self._get(request)).data)

    def put(self, request):
        return self._update(request, partial=False)

    def patch(self, request):
        return self._update(request, partial=True)

    def _update(self, request, *, partial):
        serializer = SDRComplianceSettingsSerializer(
            self._get(request), data=request.data, partial=partial
        )
        serializer.is_valid(raise_exception=True)
        return Response(SDRComplianceSettingsSerializer(serializer.save()).data)


class SDRComplianceRuleListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def get(self, request):
        rules = SDRChannelComplianceRule.objects.filter(org=request.org)
        return Response(
            {
                "count": rules.count(),
                "results": SDRChannelComplianceRuleSerializer(rules, many=True).data,
            }
        )

    def post(self, request):
        serializer = SDRChannelComplianceRuleSerializer(
            data=request.data, context={"org": request.org}
        )
        serializer.is_valid(raise_exception=True)
        rule = serializer.save(org=request.org)
        return Response(
            SDRChannelComplianceRuleSerializer(rule).data,
            status=status.HTTP_201_CREATED,
        )


class SDRComplianceRuleDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _get(request, rule_id):
        return SDRChannelComplianceRule.objects.filter(
            org=request.org, id=rule_id
        ).first()

    def put(self, request, rule_id):
        return self._update(request, rule_id, partial=False)

    def patch(self, request, rule_id):
        return self._update(request, rule_id, partial=True)

    def _update(self, request, rule_id, *, partial):
        rule = self._get(request, rule_id)
        if rule is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDRChannelComplianceRuleSerializer(
            rule,
            data=request.data,
            partial=partial,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        return Response(SDRChannelComplianceRuleSerializer(serializer.save()).data)

    def delete(self, request, rule_id):
        rule = self._get(request, rule_id)
        if rule is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SDRDoNotContactListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def get(self, request):
        entries = SDRDoNotContactEntry.objects.filter(org=request.org)
        if request.query_params.get("include_released", "false").lower() != "true":
            entries = entries.filter(is_active=True)
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            limit = 100
        entries = list(entries[:limit])
        return Response(
            {
                "count": len(entries),
                "results": SDRDoNotContactSerializer(entries, many=True).data,
            }
        )

    def post(self, request):
        serializer = SDRDoNotContactCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            entry, created = block_contact(
                org_id=request.org.id,
                created_by=request.user,
                source=SDRDoNotContactSource.ADMIN,
                **serializer.validated_data,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            SDRDoNotContactSerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SDRDoNotContactDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def delete(self, request, entry_id):
        entry = SDRDoNotContactEntry.objects.filter(
            org=request.org, id=entry_id
        ).first()
        if entry is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        entry = release_contact_block(entry, updated_by=request.user)
        return Response(SDRDoNotContactSerializer(entry).data)


class SDRDataProvenanceListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def get(self, request):
        queryset = SDRDataProvenance.objects.filter(org=request.org).select_related(
            "intake"
        )
        status_filter = request.query_params.get("status", "").strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            limit = 100
        count = queryset.count()
        return Response(
            {
                "count": count,
                "results": SDRDataProvenanceSerializer(
                    queryset[:limit], many=True
                ).data,
            }
        )


class SDRDataProvenanceDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def patch(self, request, intake_id):
        provenance = (
            SDRDataProvenance.objects.filter(org=request.org, intake_id=intake_id)
            .select_related("intake")
            .first()
        )
        if provenance is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDRDataProvenanceSerializer(
            provenance, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        return Response(SDRDataProvenanceSerializer(serializer.save()).data)


class SDRComplianceDeletionActionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def post(self, request, intake_id):
        intake = LeadIntake.objects.filter(org=request.org, id=intake_id).first()
        if intake is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDRComplianceDeletionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        if action == "request":
            provenance = request_intake_deletion(intake, requested_by=request.user)
        elif action == "cancel":
            provenance = cancel_intake_deletion(intake, updated_by=request.user)
        else:
            if serializer.validated_data["confirm_intake_id"] != intake.id:
                return Response(
                    {"confirm_intake_id": ["The confirmation does not match."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            provenance = anonymize_intake(intake, performed_by=request.user)
        provenance = SDRDataProvenance.objects.select_related("intake").get(
            id=provenance.id, org=request.org
        )
        return Response(SDRDataProvenanceSerializer(provenance).data)


class SDRComplianceRetentionScanView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def post(self, request):
        serializer = SDRComplianceRetentionScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            scan_retention(
                org_id=request.org.id,
                execute=serializer.validated_data["execute"],
                limit=serializer.validated_data["limit"],
            )
        )


class SDRSalesFeedbackLeadView(APIView):
    """Read or update the sales verdict for the latest SDR handoff of a CRM lead."""

    permission_classes = (IsAuthenticated, HasOrgContext)

    def get_permissions(self):
        permission_classes = list(self.permission_classes)
        if self.request.method == "PUT":
            permission_classes.append(HasSalesAccess)
        return [permission() for permission in permission_classes]

    def _get_intake(self, request, lead_id, *, for_update=False):
        queryset = LeadIntake.objects.filter(
            org=request.org,
            crm_lead_id=lead_id,
        ).select_related("assigned_profile__user", "crm_lead")
        if for_update:
            queryset = queryset.select_for_update()
        return queryset.order_by("-created_at", "-id").first()

    def _can_access(self, request, intake):
        profile = request.profile
        if profile.role == "ADMIN" or profile.is_organization_admin:
            return True
        if intake.assigned_profile_id == profile.id:
            return True
        return intake.crm_lead.assigned_to.filter(id=profile.id).exists()

    def _response_payload(self, request, intake):
        if not self._can_access(request, intake):
            return None
        inspection = LeadInspection.objects.filter(
            org=request.org,
            intake=intake,
        ).first()
        feedback = (
            SDRSalesFeedback.objects.filter(org=request.org, intake=intake)
            .select_related("feedback_by__user")
            .first()
        )
        return {
            "available": True,
            "can_submit": True,
            "intake": {
                "id": str(intake.id),
                "source": intake.source,
                "qualification_score": (
                    inspection.qualification_score
                    if inspection and inspection.qualification_score is not None
                    else intake.qualification_score
                ),
                "qualification_band": (
                    inspection.qualification_band
                    if inspection and inspection.qualification_band
                    else intake.qualification_band
                ),
                "provider": inspection.provider if inspection else "rules",
                "model": inspection.model if inspection else "rules-v1",
                "prompt_version": inspection.prompt_version if inspection else "",
            },
            "choices": feedback_choices(),
            "feedback": (
                SDRSalesFeedbackSerializer(feedback).data if feedback else None
            ),
        }

    @extend_schema(tags=["SDR Sales Feedback"])
    def get(self, request, lead_id):
        if not HasSalesAccess().has_permission(request, self):
            return Response({"available": False})
        intake = self._get_intake(request, lead_id)
        if intake is None:
            return Response({"available": False})
        payload = self._response_payload(request, intake)
        if payload is None:
            return Response(
                {"detail": "This SDR handoff is not assigned to you."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(payload)

    @extend_schema(
        tags=["SDR Sales Feedback"],
        request=SDRSalesFeedbackSerializer,
    )
    def put(self, request, lead_id):
        with transaction.atomic():
            intake = self._get_intake(request, lead_id, for_update=True)
            if intake is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if not self._can_access(request, intake):
                return Response(
                    {"detail": "This SDR handoff is not assigned to you."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            feedback = SDRSalesFeedback.objects.filter(
                org=request.org,
                intake=intake,
            ).first()
            serializer = SDRSalesFeedbackSerializer(
                feedback,
                data=request.data,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            save_values = {
                "feedback_by": request.profile,
                "submitted_at": timezone.now(),
            }
            if feedback is None:
                inspection = LeadInspection.objects.filter(
                    org=request.org,
                    intake=intake,
                ).first()
                save_values.update(
                    {
                        "org": request.org,
                        "intake": intake,
                        "qualification_score_snapshot": (
                            inspection.qualification_score
                            if inspection and inspection.qualification_score is not None
                            else intake.qualification_score
                        ),
                        "qualification_band_snapshot": (
                            inspection.qualification_band
                            if inspection and inspection.qualification_band
                            else intake.qualification_band
                        ),
                        "provider_snapshot": (
                            inspection.provider if inspection else "rules"
                        ),
                        "model_snapshot": (
                            inspection.model if inspection else "rules-v1"
                        ),
                        "prompt_version_snapshot": (
                            inspection.prompt_version if inspection else ""
                        ),
                    }
                )
            serializer.save(**save_values)
            payload = self._response_payload(request, intake)
        return Response(payload)


def _outbound_campaign_queryset(request):
    return (
        SDROutboundCampaign.objects.filter(org=request.org)
        .select_related("owner__user", "sequence")
        .prefetch_related("sequence__steps")
        .annotate(
            prospect_total=Count("prospects"),
            prospect_ready=Count(
                "prospects",
                filter=Q(prospects__status=OutboundProspectStatus.READY),
            ),
            prospect_queued=Count(
                "prospects",
                filter=Q(prospects__status=OutboundProspectStatus.QUEUED),
            ),
            prospect_processing=Count(
                "prospects",
                filter=Q(prospects__status=OutboundProspectStatus.PROCESSING),
            ),
            prospect_promoted=Count(
                "prospects",
                filter=Q(prospects__status=OutboundProspectStatus.PROMOTED),
            ),
            prospect_failed=Count(
                "prospects",
                filter=Q(prospects__status=OutboundProspectStatus.FAILED),
            ),
            prospect_disqualified=Count(
                "prospects",
                filter=Q(prospects__status=OutboundProspectStatus.DISQUALIFIED),
            ),
        )
    )


class SDROutboundCampaignListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Outbound"],
        responses={200: SDROutboundCampaignSerializer(many=True)},
    )
    def get(self, request):
        campaigns = list(_outbound_campaign_queryset(request))
        outbound_sequences = [
            sequence
            for sequence in SDRNurtureSequence.objects.filter(org=request.org)
            .prefetch_related("steps")
            .order_by("priority", "created_at")
            if LeadIntakeSource.OUTBOUND in sequence.sources
        ]
        return Response(
            {
                "summary": {
                    "campaigns": len(campaigns),
                    "active_campaigns": sum(
                        item.status == OutboundCampaignStatus.ACTIVE
                        for item in campaigns
                    ),
                    "prospects": sum(item.prospect_total for item in campaigns),
                    "ready": sum(item.prospect_ready for item in campaigns),
                    "promoted": sum(item.prospect_promoted for item in campaigns),
                    "failed": sum(item.prospect_failed for item in campaigns),
                },
                "channels": ["email", "linkedin", "phone", "whatsapp"],
                "statuses": [
                    {"value": value, "label": label}
                    for value, label in OutboundCampaignStatus.choices
                ],
                "outbound_sequences": [
                    {
                        "id": str(sequence.id),
                        "name": sequence.name,
                        "is_active": sequence.is_active,
                        "from_email": sequence.from_email,
                        "step_count": len(sequence.steps.all()),
                        "ready": bool(
                            sequence.is_active
                            and sequence.from_email
                            and sequence.steps.all()
                        ),
                    }
                    for sequence in outbound_sequences
                ],
                "results": SDROutboundCampaignSerializer(
                    campaigns,
                    many=True,
                    context={"org": request.org},
                ).data,
            }
        )

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundCampaignSerializer,
        responses={201: SDROutboundCampaignSerializer},
    )
    def post(self, request):
        serializer = SDROutboundCampaignSerializer(
            data=request.data,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        campaign = serializer.save(org=request.org)
        campaign = _outbound_campaign_queryset(request).get(id=campaign.id)
        return Response(
            SDROutboundCampaignSerializer(
                campaign,
                context={"org": request.org},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class SDROutboundCampaignDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def _get(self, request, campaign_id):
        return _outbound_campaign_queryset(request).filter(id=campaign_id).first()

    def get(self, request, campaign_id):
        campaign = self._get(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(
            SDROutboundCampaignSerializer(
                campaign,
                context={"org": request.org},
            ).data
        )

    def patch(self, request, campaign_id):
        campaign = self._get(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundCampaignSerializer(
            campaign,
            data=request.data,
            partial=True,
            context={"org": request.org},
        )
        serializer.is_valid(raise_exception=True)
        campaign = serializer.save()
        campaign = self._get(request, campaign.id)
        return Response(
            SDROutboundCampaignSerializer(
                campaign,
                context={"org": request.org},
            ).data
        )

    def delete(self, request, campaign_id):
        campaign = self._get(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if campaign.prospect_total:
            return Response(
                {"detail": "Archive campaigns that already contain prospects."},
                status=status.HTTP_409_CONFLICT,
            )
        campaign.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SDROutboundCampaignAnalyticsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Outbound Analytics"])
    def get(self, request, campaign_id):
        campaign = (
            SDROutboundCampaign.objects.filter(
                id=campaign_id,
                org=request.org,
            )
            .select_related("sequence")
            .prefetch_related("sequence__steps")
            .first()
        )
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(
            build_outbound_campaign_analytics(
                org=request.org,
                campaign=campaign,
            )
        )


class SDROutboundCampaignActionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundCampaignActionSerializer,
    )
    def post(self, request, campaign_id):
        campaign = _outbound_campaign_queryset(request).filter(id=campaign_id).first()
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundCampaignActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        try:
            if action == "launch":
                execution = launch_outbound_campaign(campaign)
            elif action == "pause":
                execution = pause_outbound_campaign(campaign)
            elif action == "retry_failed":
                execution = {
                    "action": "retry_failed",
                    **retry_failed_outbound_work(campaign),
                }
            elif action == "clear_safety_hold":
                execution = clear_campaign_safety_hold(campaign)
            else:
                execution = finish_outbound_campaign(
                    campaign,
                    archive=action == "archive",
                )
        except OutboundCampaignExecutionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        campaign = _outbound_campaign_queryset(request).get(id=campaign_id)
        return Response(
            {
                "campaign": SDROutboundCampaignSerializer(
                    campaign,
                    context={"org": request.org},
                ).data,
                "execution": execution,
            }
        )


class SDROutboundProspectListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Outbound"],
        responses={200: SDROutboundProspectSerializer(many=True)},
    )
    def get(self, request, campaign_id):
        if not SDROutboundCampaign.objects.filter(
            id=campaign_id,
            org=request.org,
        ).exists():
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = SDROutboundProspect.objects.filter(
            org=request.org,
            campaign_id=campaign_id,
        )
        status_filter = request.query_params.get("status", "").strip()
        if status_filter in OutboundProspectStatus.values:
            queryset = queryset.filter(status=status_filter)
        search = request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(company_name__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )
        try:
            limit = max(1, min(int(request.query_params.get("limit", 100)), 500))
        except (TypeError, ValueError):
            limit = 100
        summary = {
            row["status"]: row["count"]
            for row in SDROutboundProspect.objects.filter(
                org=request.org,
                campaign_id=campaign_id,
            )
            .values("status")
            .annotate(count=Count("id"))
        }
        prospects = list(
            queryset.select_related("campaign", "intake__crm_lead")[:limit]
        )
        return Response(
            {
                "count": queryset.count(),
                "summary": summary,
                "results": SDROutboundProspectSerializer(prospects, many=True).data,
            }
        )


class SDROutboundProspectImportView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundImportSerializer,
    )
    def post(self, request, campaign_id):
        campaign = SDROutboundCampaign.objects.filter(
            id=campaign_id,
            org=request.org,
        ).first()
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if campaign.status in {
            OutboundCampaignStatus.COMPLETED,
            OutboundCampaignStatus.ARCHIVED,
        }:
            return Response(
                {"detail": "Reopen this campaign before importing prospects."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = SDROutboundImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = import_prospect_csv(
                campaign=campaign,
                csv_text=serializer.validated_data["csv_text"],
                promote_ready=serializer.validated_data["promote_ready"],
                created_by_id=request.user.id,
            )
        except OutboundImportError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(result, status=status.HTTP_201_CREATED)


class SDROutboundProspectActionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundProspectActionSerializer,
        responses={200: SDROutboundProspectSerializer},
    )
    def post(self, request, prospect_id):
        prospect = (
            SDROutboundProspect.objects.filter(id=prospect_id, org=request.org)
            .select_related("campaign", "intake__crm_lead")
            .first()
        )
        if prospect is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundProspectActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        job_id = None
        try:
            if action == "promote":
                if prospect.campaign.status == OutboundCampaignStatus.ARCHIVED:
                    raise OutboundProspectUnavailable(
                        "Reopen the campaign before promoting prospects."
                    )
                job_id = enqueue_outbound_prospect(prospect).id
            elif action == "disqualify":
                if prospect.status not in {
                    OutboundProspectStatus.READY,
                    OutboundProspectStatus.FAILED,
                }:
                    raise OutboundProspectUnavailable(
                        "Only ready or failed prospects can be disqualified."
                    )
                prospect.status = OutboundProspectStatus.DISQUALIFIED
                prospect.save(update_fields=["status", "updated_at"])
            else:
                restore_outbound_prospect(prospect)
        except OutboundProspectUnavailable as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        prospect.refresh_from_db()
        response = SDROutboundProspectSerializer(prospect).data
        response["job_id"] = str(job_id) if job_id else None
        return Response(response)


class SDROutboundSourceListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _campaign(request, campaign_id):
        return SDROutboundCampaign.objects.filter(
            id=campaign_id,
            org=request.org,
        ).first()

    @extend_schema(
        tags=["SDR Outbound"],
        responses={200: SDROutboundSourceSerializer(many=True)},
    )
    def get(self, request, campaign_id):
        campaign = self._campaign(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        sources = SDROutboundSource.objects.filter(
            org=request.org,
            campaign=campaign,
        )
        return Response(
            SDROutboundSourceSerializer(
                sources,
                many=True,
                context={"org": request.org, "campaign": campaign},
            ).data
        )

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundSourceSerializer,
        responses={201: SDROutboundSourceSerializer},
    )
    def post(self, request, campaign_id):
        campaign = self._campaign(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundSourceSerializer(
            data=request.data,
            context={"org": request.org, "campaign": campaign},
        )
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        return Response(
            SDROutboundSourceSerializer(
                source,
                context={"org": request.org, "campaign": campaign},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class SDROutboundSourceDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _source(request, source_id):
        return (
            SDROutboundSource.objects.filter(id=source_id, org=request.org)
            .select_related("campaign")
            .first()
        )

    def get(self, request, source_id):
        source = self._source(request, source_id)
        if source is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(
            SDROutboundSourceSerializer(
                source,
                context={"org": request.org, "campaign": source.campaign},
            ).data
        )

    def patch(self, request, source_id):
        source = self._source(request, source_id)
        if source is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundSourceSerializer(
            source,
            data=request.data,
            partial=True,
            context={"org": request.org, "campaign": source.campaign},
        )
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        return Response(serializer.to_representation(source))

    def delete(self, request, source_id):
        source = self._source(request, source_id)
        if source is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        source.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SDROutboundSourceSyncView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Outbound"])
    def post(self, request, source_id):
        source = (
            SDROutboundSource.objects.filter(id=source_id, org=request.org)
            .select_related("campaign")
            .first()
        )
        if source is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if source.campaign.status == OutboundCampaignStatus.ARCHIVED:
            return Response(
                {"detail": "Reopen the campaign before syncing its sources."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = SDROutboundSourceSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approval_id = serializer.validated_data.get("approval_id")
        idempotency_key = serializer.validated_data.get("idempotency_key")
        if approval_id is None:
            return Response(
                {
                    "status": "approval_required",
                    "intent": apollo_search_execution_intent(source).as_dict(),
                    "detail": (
                        "Approve this exact one-unit Apollo search, then POST "
                        "approval_id with a new UUID idempotency_key."
                    ),
                },
                status=status.HTTP_200_OK,
            )
        try:
            job = enqueue_outbound_source_sync(
                source,
                manual=True,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )
        except OutboundSourceUnavailable as exc:
            return Response(
                {"code": exc.code, "detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {"job_id": str(job.id), "status": job.status},
            status=status.HTTP_202_ACCEPTED,
        )


class SDROutboundSourceApolloCandidateListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Outbound"])
    def get(self, request, source_id):
        source = SDROutboundSource.objects.filter(
            id=source_id,
            org=request.org,
        ).first()
        if source is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        candidates = list(
            SDRApolloCandidate.objects.filter(
                org=request.org,
                source=source,
            ).order_by("created_at", "id")[:500]
        )
        data = SDRApolloCandidateSerializer(candidates, many=True).data
        for item, candidate in zip(data, candidates, strict=True):
            item["enrichment_intent"] = (
                apollo_enrichment_execution_intent(candidate).as_dict()
                if candidate.status == ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL
                else None
            )
        return Response({"results": data, "count": len(data)})


class SDRApolloCandidateEnrichView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["SDR Outbound"])
    def post(self, request, candidate_id):
        candidate = (
            SDRApolloCandidate.objects.filter(
                id=candidate_id,
                org=request.org,
            )
            .select_related("source", "source__campaign")
            .first()
        )
        if candidate is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundSourceSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approval_id = serializer.validated_data.get("approval_id")
        idempotency_key = serializer.validated_data.get("idempotency_key")
        if approval_id is None:
            if candidate.status != ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL:
                return Response(
                    {
                        "code": "apollo_candidate_not_pending",
                        "detail": "This candidate is not awaiting enrichment approval.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {
                    "status": "approval_required",
                    "candidate_id": str(candidate.id),
                    "intent": apollo_enrichment_execution_intent(candidate).as_dict(),
                    "detail": (
                        "Approve this exact one-unit Apollo enrichment, then POST "
                        "approval_id with a new UUID idempotency_key."
                    ),
                },
                status=status.HTTP_200_OK,
            )
        try:
            job = enqueue_apollo_candidate_enrichment(
                candidate,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )
        except OutboundSourceUnavailable as exc:
            return Response(
                {"code": exc.code, "detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        candidate.refresh_from_db()
        return Response(
            {
                "job_id": str(job.id),
                "status": job.status,
                "candidate_id": str(candidate.id),
                "execution_request_id": str(candidate.enrichment_request_id),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class SDROutboundCopyDraftListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _campaign(request, campaign_id):
        return SDROutboundCampaign.objects.filter(
            id=campaign_id,
            org=request.org,
        ).first()

    @extend_schema(
        tags=["SDR Outbound"],
        responses={200: SDROutboundCopyDraftSerializer(many=True)},
    )
    def get(self, request, campaign_id):
        campaign = self._campaign(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        drafts = (
            SDROutboundCopyDraft.objects.filter(org=request.org, campaign=campaign)
            .select_related("reviewed_by__user")
            .order_by("-created_at")[:20]
        )
        return Response(SDROutboundCopyDraftSerializer(drafts, many=True).data)

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundCopyGenerateSerializer,
        responses={202: SDROutboundCopyDraftSerializer},
    )
    def post(self, request, campaign_id):
        campaign = self._campaign(request, campaign_id)
        if campaign is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if campaign.status in {
            OutboundCampaignStatus.COMPLETED,
            OutboundCampaignStatus.ARCHIVED,
        }:
            return Response(
                {"detail": "Reopen the campaign before generating outbound copy."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = SDROutboundCopyGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            draft = SDROutboundCopyDraft.objects.create(
                org=request.org,
                campaign=campaign,
                created_by=request.user,
                **serializer.validated_data,
            )
            job = enqueue_outbound_copy_generation(draft)
        draft.refresh_from_db()
        response = SDROutboundCopyDraftSerializer(draft).data
        response["job_id"] = str(job.id)
        return Response(response, status=status.HTTP_202_ACCEPTED)


class SDROutboundCopyDraftDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _draft(request, draft_id):
        return (
            SDROutboundCopyDraft.objects.filter(id=draft_id, org=request.org)
            .select_related("campaign", "reviewed_by__user")
            .first()
        )

    def get(self, request, draft_id):
        draft = self._draft(request, draft_id)
        if draft is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(SDROutboundCopyDraftSerializer(draft).data)

    def patch(self, request, draft_id):
        draft = self._draft(request, draft_id)
        if draft is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if draft.status != "ready":
            return Response(
                {"detail": "Only ready copy drafts can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = SDROutboundCopyDraftEditSerializer(
            data=request.data,
            context={"draft": draft},
        )
        serializer.is_valid(raise_exception=True)
        draft.generated_steps = serializer.validated_data["generated_steps"]
        draft.reviewed_by = request.profile
        draft.reviewed_at = timezone.now()
        draft.save(
            update_fields=[
                "generated_steps",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
        draft = self._draft(request, draft.id)
        return Response(SDROutboundCopyDraftSerializer(draft).data)


class SDROutboundCopyDraftActionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Outbound"],
        request=SDROutboundCopyDraftActionSerializer,
    )
    def post(self, request, draft_id):
        draft = (
            SDROutboundCopyDraft.objects.filter(id=draft_id, org=request.org)
            .select_related("campaign")
            .first()
        )
        if draft is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDROutboundCopyDraftActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sequence = apply_outbound_copy_draft(
                draft,
                reviewer=request.profile,
            )
        except OutboundCopyUnavailable as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        draft = SDROutboundCopyDraft.objects.select_related("reviewed_by__user").get(
            id=draft.id, org=request.org
        )
        return Response(
            {
                "draft": SDROutboundCopyDraftSerializer(draft).data,
                "sequence_id": str(sequence.id),
                "sequence_name": sequence.name,
                "sequence_active": sequence.is_active,
            }
        )


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
            if "low" in settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS
            else settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS[0]
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


class SDRAICallAuditListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Intelligence"],
        responses={200: SDRAICallAuditSerializer(many=True)},
    )
    def get(self, request):
        queryset = SDRAICallAudit.objects.filter(org=request.org)
        purpose = request.query_params.get("purpose", "").strip()
        status_filter = request.query_params.get("status", "").strip()
        if purpose:
            queryset = queryset.filter(purpose=purpose)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        try:
            limit = int(request.query_params.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        records = queryset.order_by("-created_at", "id")[:limit]
        return Response(SDRAICallAuditSerializer(records, many=True).data)


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


EMAIL_APPROVABLE_DELIVERY_STATUSES = frozenset(
    {
        LeadDeliveryStatus.PENDING,
        LeadDeliveryStatus.FAILED,
    }
)
NURTURE_EMAIL_APPROVABLE_DELIVERY_STATUSES = frozenset(
    {
        NurtureDeliveryStatus.PENDING,
        NurtureDeliveryStatus.FAILED,
    }
)


def _email_execution_intent_payload(*, delivery_id, intent):
    """Project an exact approval scope without recipient or message content."""

    return {
        "channel": ExecutionChannel.EMAIL,
        "action": EMAIL_SEND_ACTION,
        "delivery_id": str(delivery_id),
        "target_sha256": intent.target_hash,
        "payload_sha256": intent.payload_hash,
        "units": intent.units,
    }


def _email_execution_error(exc: ExecutionSafetyError):
    response_status = exc.status_code
    if not isinstance(response_status, int) or not 400 <= response_status < 600:
        response_status = status.HTTP_409_CONFLICT
    return Response(
        {"code": exc.code, "detail": exc.detail},
        status=response_status,
    )


def _email_intent_unavailable():
    return Response(
        {
            "code": "email_execution_unavailable",
            "detail": "The email delivery cannot be approved in its current state.",
        },
        status=status.HTTP_409_CONFLICT,
    )


def _email_job_response(*, job, execution_request, replayed):
    return Response(
        {
            "job_id": str(job.id),
            "status": job.status,
            "execution_request_id": str(execution_request.id),
            "execution_status": execution_request.status,
            "replayed": replayed,
        },
        status=status.HTTP_202_ACCEPTED,
    )


def _email_non_reserved_replay_response(execution_request):
    response_status = (
        status.HTTP_200_OK
        if execution_request.status
        in {
            ExternalRequestStatus.ACCEPTED,
            ExternalRequestStatus.DELIVERED,
        }
        else status.HTTP_409_CONFLICT
    )
    return Response(
        {
            "code": "email_execution_not_replayable",
            "detail": (
                "This email execution has already left RESERVED state; "
                "no provider job was queued."
            ),
            "execution_request_id": str(execution_request.id),
            "execution_status": execution_request.status,
            "replayed": True,
        },
        status=response_status,
    )


class SDRAcknowledgementEmailExecutionView(APIView):
    """Preview or reserve one exact acknowledgement-email provider attempt."""

    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Email Execution"],
        request=SDREmailExecutionApprovalSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request, delivery_id):
        delivery = (
            LeadDelivery.objects.filter(
                id=delivery_id,
                org=request.org,
                intake__org=request.org,
            )
            .select_related("intake__org")
            .first()
        )
        if delivery is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if delivery.kind != LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL:
            return Response(
                {
                    "code": "email_delivery_kind_mismatch",
                    "detail": "This delivery is not an acknowledgement email.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        if delivery.status not in EMAIL_APPROVABLE_DELIVERY_STATUSES:
            return _email_intent_unavailable()

        serializer = SDREmailExecutionApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            intent = acknowledgement_email_execution_intent(delivery)
        except ExecutionSafetyError as exc:
            return _email_execution_error(exc)
        except (DjangoValidationError, ValueError):
            return _email_intent_unavailable()

        approval_id = serializer.validated_data.get("approval_id")
        if approval_id is None:
            return Response(
                {
                    "approval_required": True,
                    "intent": _email_execution_intent_payload(
                        delivery_id=delivery.id,
                        intent=intent,
                    ),
                }
            )
        try:
            reservation = reserve_email_send(
                org=request.org,
                delivery_id=delivery.id,
                approval_id=approval_id,
                intent=intent,
            )
            if reservation.request.status != ExternalRequestStatus.RESERVED:
                return _email_non_reserved_replay_response(reservation.request)
            job = enqueue_approved_acknowledgement_delivery(
                delivery,
                execution_request_id=reservation.request.id,
            )
        except ExecutionSafetyError as exc:
            return _email_execution_error(exc)
        return _email_job_response(
            job=job,
            execution_request=reservation.request,
            replayed=reservation.replayed,
        )


class SDRNurtureEmailExecutionView(APIView):
    """Preview or reserve one exact nurture-email provider attempt."""

    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Email Execution"],
        request=SDREmailExecutionApprovalSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request, delivery_id):
        delivery = (
            LeadNurtureDelivery.objects.filter(
                id=delivery_id,
                org=request.org,
                enrollment__org=request.org,
                enrollment__intake__org=request.org,
            )
            .select_related(
                "enrollment__intake__org",
                "enrollment__sequence",
                "step",
            )
            .first()
        )
        if delivery is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if delivery.status not in NURTURE_EMAIL_APPROVABLE_DELIVERY_STATUSES:
            return _email_intent_unavailable()

        serializer = SDREmailExecutionApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            intent = nurture_email_execution_intent(delivery)
        except ExecutionSafetyError as exc:
            return _email_execution_error(exc)
        except (DjangoValidationError, ValueError):
            return _email_intent_unavailable()

        approval_id = serializer.validated_data.get("approval_id")
        if approval_id is None:
            return Response(
                {
                    "approval_required": True,
                    "intent": _email_execution_intent_payload(
                        delivery_id=delivery.id,
                        intent=intent,
                    ),
                }
            )
        try:
            reservation = reserve_email_send(
                org=request.org,
                delivery_id=delivery.id,
                approval_id=approval_id,
                intent=intent,
            )
            if reservation.request.status != ExternalRequestStatus.RESERVED:
                return _email_non_reserved_replay_response(reservation.request)
            job = enqueue_approved_nurture_delivery(
                delivery,
                execution_request_id=reservation.request.id,
            )
        except ExecutionSafetyError as exc:
            return _email_execution_error(exc)
        return _email_job_response(
            job=job,
            execution_request=reservation.request,
            replayed=reservation.replayed,
        )


class SDRResponseSettingsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _get_configuration(request):
        configuration, _ = SDRResponseSettings.objects.get_or_create(org=request.org)
        return configuration

    @extend_schema(
        tags=["SDR Response"],
        responses={200: SDRResponseSettingsSerializer},
    )
    def get(self, request):
        return Response(
            SDRResponseSettingsSerializer(self._get_configuration(request)).data
        )

    @extend_schema(
        tags=["SDR Response"],
        request=SDRResponseSettingsSerializer,
        responses={200: SDRResponseSettingsSerializer},
    )
    def put(self, request):
        return self._update(request, partial=False)

    def patch(self, request):
        return self._update(request, partial=True)

    def _update(self, request, *, partial):
        configuration = self._get_configuration(request)
        serializer = SDRResponseSettingsSerializer(
            configuration,
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        return Response(SDRResponseSettingsSerializer(serializer.save()).data)


class LeadIntakeListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Response"],
        responses={200: LeadIntakeOperationsSerializer(many=True)},
    )
    def get(self, request):
        base = LeadIntake.objects.filter(org=request.org)
        summary = {
            row["status"]: row["count"]
            for row in base.values("status").annotate(count=Count("id"))
        }
        delivery_summary = {}
        for row in (
            base.values("deliveries__kind", "deliveries__status")
            .exclude(deliveries__kind__isnull=True)
            .annotate(count=Count("deliveries__id"))
        ):
            kind = row["deliveries__kind"]
            delivery_summary.setdefault(kind, {})[row["deliveries__status"]] = row[
                "count"
            ]

        queryset = base
        status_filter = request.query_params.get("status", "").strip()
        if status_filter in LeadIntakeStatus.values:
            queryset = queryset.filter(status=status_filter)
        source_filter = request.query_params.get("source", "").strip()
        if source_filter in LeadIntakeSource.values:
            queryset = queryset.filter(source=source_filter)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 50)), 100))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        count = queryset.count()
        intakes = list(
            queryset.select_related(
                "crm_lead",
                "assigned_profile__user",
                "routing_rule",
            )
            .prefetch_related("deliveries", "lifecycle_events")
            .order_by("-created_at")[offset : offset + limit]
        )
        response_configuration, _ = SDRResponseSettings.objects.get_or_create(
            org=request.org
        )
        serialized = LeadIntakeOperationsSerializer(
            intakes,
            many=True,
            context={"response_settings": response_configuration},
        ).data
        response_values = [
            item["response_seconds"]
            for item in serialized
            if item["response_seconds"] is not None
        ]
        return Response(
            {
                "count": count,
                "summary": summary,
                "delivery_summary": delivery_summary,
                "response_metrics": {
                    "sample_size": len(serialized),
                    "responded": len(response_values),
                    "average_response_seconds": (
                        round(sum(response_values) / len(response_values))
                        if response_values
                        else None
                    ),
                    "sla_breached": sum(
                        1 for item in serialized if item["sla_breached"]
                    ),
                    "sla_seconds": response_configuration.response_sla_seconds,
                },
                "results": serialized,
            }
        )


class LeadIntakeDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    @extend_schema(
        tags=["SDR Response"],
        responses={200: LeadIntakeOperationsSerializer},
    )
    def get(self, request, intake_id):
        intake = (
            LeadIntake.objects.filter(id=intake_id, org=request.org)
            .select_related(
                "crm_lead",
                "assigned_profile__user",
                "routing_rule",
            )
            .prefetch_related("deliveries", "lifecycle_events")
            .first()
        )
        if intake is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        response_configuration, _ = SDRResponseSettings.objects.get_or_create(
            org=request.org
        )
        return Response(
            LeadIntakeOperationsSerializer(
                intake,
                context={"response_settings": response_configuration},
            ).data
        )


class SDRNurtureSequenceListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Nurture"],
        responses={200: SDRNurtureSequenceSerializer(many=True)},
    )
    def get(self, request):
        sequences = list(
            SDRNurtureSequence.objects.filter(org=request.org)
            .prefetch_related("steps", "enrollments")
            .order_by("priority", "created_at", "id")
        )
        enrollment_base = LeadNurtureEnrollment.objects.filter(org=request.org)
        delivery_base = LeadNurtureDelivery.objects.filter(org=request.org)
        active_suppressions = SDREmailSuppression.objects.filter(
            org=request.org,
            is_active=True,
        ).count()
        sent = delivery_base.filter(status=NurtureDeliveryStatus.SENT).count()
        delivered = delivery_base.filter(delivered_at__isnull=False).count()
        bounced = delivery_base.filter(bounced_at__isnull=False).count()
        complained = delivery_base.filter(complained_at__isnull=False).count()
        opened = delivery_base.filter(opened_at__isnull=False).count()
        clicked = delivery_base.filter(clicked_at__isnull=False).count()
        replied = delivery_base.filter(replied_at__isnull=False).count()
        positive = delivery_base.filter(
            reply_sentiment=NurtureReplySentiment.POSITIVE
        ).count()
        return Response(
            {
                "summary": {
                    "sequences": len(sequences),
                    "active_sequences": sum(1 for item in sequences if item.is_active),
                    "enrollments": enrollment_base.count(),
                    "active_enrollments": enrollment_base.filter(
                        status=NurtureEnrollmentStatus.ACTIVE
                    ).count(),
                    "sent": sent,
                    "delivered": delivered,
                    "bounced": bounced,
                    "complained": complained,
                    "opened": opened,
                    "clicked": clicked,
                    "replied": replied,
                    "positive_replies": positive,
                    "active_suppressions": active_suppressions,
                    "open_rate": round(opened * 100 / sent, 1) if sent else 0,
                    "click_rate": round(clicked * 100 / sent, 1) if sent else 0,
                    "delivery_rate": (round(delivered * 100 / sent, 1) if sent else 0),
                    "bounce_rate": round(bounced * 100 / sent, 1) if sent else 0,
                    "complaint_rate": (
                        round(complained * 100 / sent, 1) if sent else 0
                    ),
                    "reply_rate": round(replied * 100 / sent, 1) if sent else 0,
                    "positive_reply_rate": (
                        round(positive * 100 / sent, 1) if sent else 0
                    ),
                },
                "sources": [
                    {"value": value, "label": label}
                    for value, label in LeadIntakeSource.choices
                ],
                "qualification_bands": [
                    {"value": band.value, "label": band.value.title()}
                    for band in QualificationBand
                ],
                "results": SDRNurtureSequenceSerializer(
                    sequences,
                    many=True,
                ).data,
            }
        )

    @extend_schema(
        tags=["SDR Nurture"],
        request=SDRNurtureSequenceSerializer,
        responses={201: SDRNurtureSequenceSerializer},
    )
    def post(self, request):
        serializer = SDRNurtureSequenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sequence = serializer.save(org=request.org)
        sequence = SDRNurtureSequence.objects.prefetch_related(
            "steps", "enrollments"
        ).get(id=sequence.id, org=request.org)
        return Response(
            SDRNurtureSequenceSerializer(sequence).data,
            status=status.HTTP_201_CREATED,
        )


class SDREmailSuppressionListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Nurture"],
        responses={200: SDREmailSuppressionSerializer(many=True)},
    )
    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            limit = 100
        suppressions = SDREmailSuppression.objects.filter(org=request.org)
        if request.query_params.get("include_released", "false").lower() != "true":
            suppressions = suppressions.filter(is_active=True)
        suppressions = suppressions.select_related("source_delivery")[:limit]
        return Response(
            {
                "count": len(suppressions),
                "results": SDREmailSuppressionSerializer(
                    suppressions,
                    many=True,
                ).data,
                "reasons": [
                    {"value": value, "label": label}
                    for value, label in EmailSuppressionReason.choices
                ],
            }
        )

    @extend_schema(
        tags=["SDR Nurture"],
        request=SDREmailSuppressionCreateSerializer,
        responses={201: SDREmailSuppressionSerializer},
    )
    def post(self, request):
        serializer = SDREmailSuppressionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        suppression, created = suppress_email(
            org_id=request.org.id,
            email=serializer.validated_data["email"],
            reason=serializer.validated_data["reason"],
            source=EmailSuppressionSource.ADMIN,
            details={"profile_id": str(request.profile.id)},
        )
        return Response(
            SDREmailSuppressionSerializer(suppression).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SDREmailSuppressionDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Nurture"],
        responses={200: SDREmailSuppressionSerializer},
    )
    def delete(self, request, suppression_id):
        suppression = SDREmailSuppression.objects.filter(
            id=suppression_id,
            org=request.org,
        ).first()
        if suppression is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        release_suppression(suppression, updated_by=request.user)
        suppression.refresh_from_db()
        return Response(SDREmailSuppressionSerializer(suppression).data)


class SDRNurtureSequenceDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _get_sequence(request, sequence_id):
        return (
            SDRNurtureSequence.objects.filter(id=sequence_id, org=request.org)
            .prefetch_related("steps", "enrollments")
            .first()
        )

    def get(self, request, sequence_id):
        sequence = self._get_sequence(request, sequence_id)
        if sequence is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(SDRNurtureSequenceSerializer(sequence).data)

    def put(self, request, sequence_id):
        return self._update(request, sequence_id, partial=False)

    def patch(self, request, sequence_id):
        return self._update(request, sequence_id, partial=True)

    def _update(self, request, sequence_id, *, partial):
        sequence = self._get_sequence(request, sequence_id)
        if sequence is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = SDRNurtureSequenceSerializer(
            sequence,
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        sequence = serializer.save()
        sequence = self._get_sequence(request, sequence.id)
        return Response(SDRNurtureSequenceSerializer(sequence).data)

    def delete(self, request, sequence_id):
        sequence = self._get_sequence(request, sequence_id)
        if sequence is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            sequence.delete()
        except ProtectedError:
            return Response(
                {"detail": "Disable sequences that already have enrollment history."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeadNurtureEnrollmentListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Nurture"],
        responses={200: LeadNurtureEnrollmentSerializer(many=True)},
    )
    def get(self, request):
        queryset = LeadNurtureEnrollment.objects.filter(org=request.org)
        status_filter = request.query_params.get("status", "").strip()
        if status_filter in NurtureEnrollmentStatus.values:
            queryset = queryset.filter(status=status_filter)
        sequence_id = request.query_params.get("sequence_id", "").strip()
        if sequence_id:
            queryset = queryset.filter(sequence_id=sequence_id)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 50)), 100))
        except (TypeError, ValueError):
            limit = 50
        count = queryset.count()
        enrollments = list(
            queryset.select_related(
                "sequence",
                "intake__crm_lead",
                "lead",
            )
            .prefetch_related("deliveries")
            .order_by("-enrolled_at")[:limit]
        )
        summary = {
            row["status"]: row["count"]
            for row in LeadNurtureEnrollment.objects.filter(org=request.org)
            .values("status")
            .annotate(count=Count("id"))
        }
        return Response(
            {
                "count": count,
                "summary": summary,
                "results": LeadNurtureEnrollmentSerializer(
                    enrollments,
                    many=True,
                ).data,
            }
        )


class LeadNurtureEnrollmentActionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["SDR Nurture"],
        request=LeadNurtureEnrollmentActionSerializer,
        responses={200: LeadNurtureEnrollmentSerializer},
    )
    def post(self, request, enrollment_id):
        serializer = LeadNurtureEnrollmentActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enrollment = (
            LeadNurtureEnrollment.objects.filter(
                id=enrollment_id,
                org=request.org,
            )
            .select_related("sequence", "intake__crm_lead", "lead")
            .prefetch_related("deliveries")
            .first()
        )
        if enrollment is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        action = serializer.validated_data["action"]
        if action == "pause":
            enrollment = pause_enrollment(enrollment)
        elif action == "resume":
            enrollment = resume_enrollment(enrollment)
        elif action == "cancel":
            enrollment = stop_enrollment(
                enrollment,
                status=NurtureEnrollmentStatus.CANCELLED,
                reason="Cancelled by a user.",
            )
        elif action == "mark_replied":
            enrollment = stop_enrollment(
                enrollment,
                status=NurtureEnrollmentStatus.REPLIED,
                reason="A reply was recorded by a user.",
                reply_sentiment=serializer.validated_data["reply_sentiment"],
            )
        else:
            enrollment = stop_enrollment(
                enrollment,
                status=NurtureEnrollmentStatus.CONVERTED,
                reason="A conversion was recorded by a user.",
            )
        enrollment.refresh_from_db()
        return Response(
            LeadNurtureEnrollmentSerializer(
                enrollment,
                context={"request": request},
            ).data
        )
