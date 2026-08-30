import logging
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import models, transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import HasOrgContext, IsOrgAdmin
from integrations.api.serializers import (
    ApolloConnectionSerializer,
    ApolloConnectionWriteSerializer,
    ChannelSafetyApprovalWriteSerializer,
    ChannelSafetyChannelWriteSerializer,
    ChannelSafetyOrganizationWriteSerializer,
    ChannelSafetyTargetWriteSerializer,
    ChannelSafetyUnknownResolveSerializer,
    FacebookConversionSettingsSerializer,
    FacebookMessengerManualReplySerializer,
    FacebookMessengerSettingsSerializer,
    FacebookOAuthPageSelectionSerializer,
    FacebookOAuthSessionSerializer,
    FacebookOAuthStartSerializer,
    FacebookPageConnectionCreateSerializer,
    FacebookPageConnectionSerializer,
    FeishuBaseConnectionSerializer,
    FeishuBaseConnectionWriteSerializer,
    FeishuBaseExecutionWriteSerializer,
    FeishuBasePersonImportWriteSerializer,
    LinkedInConnectionSerializer,
    LinkedInConnectionWriteSerializer,
    WhatsAppBusinessConnectionSerializer,
    WhatsAppBusinessConnectionWriteSerializer,
    WhatsAppMessageExecutionSerializer,
    WhatsAppMessageListQuerySerializer,
)
from integrations.execution_safety import (
    ExecutionSafetyError,
    add_test_target,
    configure_channel,
    configure_organization_execution,
    disable_test_target,
    issue_execution_approval,
    resolve_unknown_execution,
)
from integrations.models import (
    ApolloConnection,
    ChannelExecutionApproval,
    ChannelExecutionControl,
    ChannelTestTarget,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    FacebookConversionSettings,
    FacebookMessengerMessage,
    FacebookMessengerReply,
    FacebookOAuthSession,
    FacebookPageConnection,
    FeishuBaseConnection,
    FeishuBasePersonImport,
    FeishuBaseSync,
    FeishuBaseSyncStatus,
    LinkedInConnection,
    OrganizationExecutionControl,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)
from integrations.providers.facebook.adapter import (
    FacebookLeadAdsAdapter,
    FacebookWebhookError,
)
from integrations.providers.facebook.client import FacebookGraphAPIError
from integrations.providers.facebook.conversions import (
    reconcile_recent_conversion_events,
)
from integrations.providers.facebook.jobs import enqueue_facebook_lead_event
from integrations.providers.facebook.messenger import (
    FacebookMessengerReplyUnavailable,
    FacebookMessengerUnavailable,
    enqueue_facebook_message_event,
    enqueue_manual_facebook_reply,
)
from integrations.providers.facebook.oauth import (
    FacebookOAuthConfigurationError,
    FacebookOAuthSelectionError,
    FacebookOAuthStateError,
    finish_facebook_oauth,
    select_facebook_pages,
    start_facebook_oauth,
)
from integrations.providers.facebook.service import (
    FacebookConnectionUnavailable,
    FacebookPageAlreadyConnected,
    FacebookPageIdentityMismatch,
    connect_facebook_page,
    set_facebook_messenger_enabled,
)
from integrations.providers.feishu_base.person_import import (
    enqueue_feishu_person_import,
    feishu_person_import_execution_intent,
)
from integrations.providers.feishu_base.sync import (
    FeishuBaseSyncUnavailable,
    enqueue_feishu_base_sync,
    enqueue_feishu_remote_delete,
    enqueue_feishu_schema_validation,
    feishu_delete_execution_intent,
    feishu_research_sync_execution_intent,
    feishu_schema_execution_intent,
)
from integrations.providers.whatsapp.outbound import (
    WHATSAPP_SEND_ACTION,
    WhatsAppCampaignUnavailable,
    reserve_and_enqueue_whatsapp_message,
    whatsapp_message_execution_intent,
)
from integrations.providers.whatsapp.webhooks import (
    process_whatsapp_status_webhook,
    verify_whatsapp_signature,
)
from integrations.secrets import SecretDecryptionError
from leads.models import Lead
from sdr.compliance import intake_data_restriction
from sdr.models import LeadIntake

logger = logging.getLogger(__name__)


def _channel_safety_error(exc):
    return Response(exc.as_dict(), status=exc.status_code)


def _uuid_header(request):
    from uuid import UUID

    try:
        return UUID(request.headers.get("Idempotency-Key", "").strip())
    except (TypeError, ValueError):
        return None


class ChannelSafetySummaryView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def get(self, request):
        org_control = OrganizationExecutionControl.objects.filter(org=request.org).first()
        controls = {
            item.channel: item
            for item in ChannelExecutionControl.objects.filter(org=request.org)
        }
        targets = list(ChannelTestTarget.objects.filter(org=request.org).order_by("channel", "safe_label"))
        target_labels = {(item.channel, item.identifier_hash): item.safe_label for item in targets}
        now = timezone.now()
        approvals = ChannelExecutionApproval.objects.filter(
            org=request.org, consumed_at__isnull=True, expires_at__gt=now
        ).order_by("expires_at")
        unknown = ExternalExecutionRequest.objects.filter(
            org=request.org, status=ExternalRequestStatus.UNKNOWN
        ).order_by("unknown_at")
        return Response(
            {
                "environment_enabled": bool(
                    getattr(settings, "REAL_CHANNEL_EXECUTION_ENABLED", False)
                ),
                "organization": {
                    "enabled": org_control.enabled if org_control else False,
                    "daily_limit": org_control.daily_limit if org_control else 0,
                    "reserved_units": org_control.reserved_units if org_control else 0,
                    "consumed_units": org_control.consumed_units if org_control else 0,
                    "revision": org_control.revision if org_control else 0,
                },
                "channels": [
                    {
                        "channel": channel,
                        "implemented": channel not in {ExecutionChannel.WECHAT, ExecutionChannel.WECOM},
                        "enabled": bool(controls.get(channel) and controls[channel].enabled)
                        if channel not in {ExecutionChannel.WECHAT, ExecutionChannel.WECOM}
                        else False,
                        "test_mode": controls[channel].test_mode if channel in controls else True,
                        "daily_limit": controls[channel].daily_limit if channel in controls else 0,
                        "per_execution_limit": controls[channel].per_execution_limit if channel in controls else 0,
                        "reserved_units": controls[channel].reserved_units if channel in controls else 0,
                        "consumed_units": controls[channel].consumed_units if channel in controls else 0,
                        "revision": controls[channel].revision if channel in controls else 0,
                    }
                    for channel in ExecutionChannel.values
                ],
                "test_targets": [
                    {"id": item.id, "channel": item.channel, "safe_label": item.safe_label, "active": item.is_active}
                    for item in targets
                ],
                "approvals": [
                    {
                        "id": item.id,
                        "channel": item.channel,
                        "action": item.action,
                        "safe_label": target_labels.get((item.channel, item.target_hash), "Approved test target"),
                        "units": item.units,
                        "expires_at": item.expires_at,
                    }
                    for item in approvals
                ],
                "unknown_requests": [
                    {
                        "id": item.id,
                        "channel": item.channel,
                        "action": item.action,
                        "units": item.units,
                        "status": item.status,
                        "sending_at": item.sending_at,
                        "unknown_at": item.unknown_at,
                    }
                    for item in unknown
                ],
            }
        )


class ChannelSafetyOrganizationView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def put(self, request):
        serializer = ChannelSafetyOrganizationWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            control = configure_organization_execution(
                org=request.org, actor=request.profile, **serializer.validated_data
            )
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        return Response({"enabled": control.enabled, "daily_limit": control.daily_limit, "revision": control.revision})


class ChannelSafetyChannelView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def put(self, request, channel):
        serializer = ChannelSafetyChannelWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            control = configure_channel(
                org=request.org, actor=request.profile, channel=channel, **serializer.validated_data
            )
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        return Response({"channel": control.channel, "enabled": control.enabled, "test_mode": control.test_mode, "daily_limit": control.daily_limit, "per_execution_limit": control.per_execution_limit, "revision": control.revision})


class ChannelSafetyTargetListCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def post(self, request):
        serializer = ChannelSafetyTargetWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            target = add_test_target(org=request.org, actor=request.profile, **serializer.validated_data)
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        return Response({"id": target.id, "channel": target.channel, "safe_label": target.safe_label, "active": target.is_active}, status=201)


class ChannelSafetyTargetDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def delete(self, request, target_id):
        try:
            disable_test_target(org=request.org, actor=request.profile, target_id=target_id)
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        return Response(status=204)


class ChannelSafetyApprovalCreateView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def post(self, request):
        key = _uuid_header(request)
        if key is None:
            return Response({"idempotency_key": ["A valid UUID Idempotency-Key is required."]}, status=400)
        serializer = ChannelSafetyApprovalWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        target = ChannelTestTarget.objects.filter(
            org=request.org, id=data["target_id"], is_active=True
        ).first()
        if target is None:
            return Response(status=404)
        try:
            result = issue_execution_approval(
                org=request.org,
                approved_by=request.profile,
                channel=target.channel,
                action=data["action"],
                target_hash=target.identifier_hash,
                payload_hash=data["payload_sha256"],
                units=data["units"],
                expires_in=timedelta(seconds=data["expires_in_seconds"]),
                idempotency_key=key,
            )
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        approval = result.approval
        return Response(
            {"id": approval.id, "channel": approval.channel, "action": approval.action, "safe_label": target.safe_label, "units": approval.units, "expires_at": approval.expires_at, "replayed": result.replayed},
            status=200 if result.replayed else 201,
        )


class ChannelSafetyUnknownResolveView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    def post(self, request, request_id):
        serializer = ChannelSafetyUnknownResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = resolve_unknown_execution(
                org=request.org, actor=request.profile, request_id=request_id, **serializer.validated_data
            )
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        return Response({"id": item.id, "status": item.status, "error_code": item.error_code})


def _local_connection_test_payload(code: str, *, ok: bool = False) -> dict:
    """Return a deliberately small, provider-data-free local test result."""

    return {"code": code, "ok": ok, "local_only": True}


class BaseLocalConnectionTestView(APIView):
    """Validate stored connection state without provider calls or data writes."""

    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)
    connection_model = None
    credential_ciphertext_field = ""
    credential_getter_name = ""
    required_identifier_fields: tuple[str, ...] = ()
    select_related_fields: tuple[str, ...] = ()
    requires_partner_access = False

    def permission_denied(self, request, message=None, code=None):
        payload = _local_connection_test_payload("permission_denied")
        if request.authenticators and not request.successful_authenticator:
            raise NotAuthenticated(payload)
        raise PermissionDenied(payload)

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(exc, (NotAuthenticated, PermissionDenied)):
            # DRF converts primitive values inside exception ``detail`` mappings
            # to ErrorDetail strings. Restore the endpoint's boolean-only contract.
            response.data = _local_connection_test_payload("permission_denied")
        return response

    def _connection(self, request):
        queryset = self.connection_model.objects.filter(org=request.org)
        if self.select_related_fields:
            queryset = queryset.select_related(*self.select_related_fields)
        return queryset.first()

    @staticmethod
    def _required_value_present(value) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        return value is not None

    def post(self, request):
        connection = self._connection(request)
        if connection is None:
            return Response(
                _local_connection_test_payload("connection_missing"),
                status=status.HTTP_404_NOT_FOUND,
            )
        if not connection.is_active:
            return Response(
                _local_connection_test_payload("connection_inactive"),
                status=status.HTTP_409_CONFLICT,
            )
        if any(
            not self._required_value_present(getattr(connection, field_name, None))
            for field_name in self.required_identifier_fields
        ):
            return Response(
                _local_connection_test_payload("required_identifier_missing"),
                status=status.HTTP_400_BAD_REQUEST,
            )

        ciphertext = getattr(connection, self.credential_ciphertext_field, "")
        if not self._required_value_present(ciphertext):
            return Response(
                _local_connection_test_payload("credential_missing"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            credential = getattr(connection, self.credential_getter_name)()
        except SecretDecryptionError:
            return Response(
                _local_connection_test_payload("credential_decryption_failed"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._required_value_present(credential):
            return Response(
                _local_connection_test_payload("credential_decryption_failed"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        if self.requires_partner_access and not connection.partner_access_confirmed:
            return Response(
                _local_connection_test_payload("partner_access_not_confirmed"),
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            _local_connection_test_payload("connection_ready", ok=True),
            status=status.HTTP_200_OK,
        )


class ApolloConnectionTestView(BaseLocalConnectionTestView):
    connection_model = ApolloConnection
    credential_ciphertext_field = "api_key_ciphertext"
    credential_getter_name = "get_api_key"


class WhatsAppBusinessConnectionTestView(BaseLocalConnectionTestView):
    connection_model = WhatsAppBusinessConnection
    credential_ciphertext_field = "access_token_ciphertext"
    credential_getter_name = "get_access_token"
    required_identifier_fields = ("phone_number_id",)
    select_related_fields = ("route",)


class LinkedInConnectionTestView(BaseLocalConnectionTestView):
    connection_model = LinkedInConnection
    credential_ciphertext_field = "access_token_ciphertext"
    credential_getter_name = "get_access_token"
    requires_partner_access = True


class ApolloConnectionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Apollo"],
        responses={200: ApolloConnectionSerializer},
    )
    def get(self, request):
        connection = ApolloConnection.objects.filter(org=request.org).first()
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(ApolloConnectionSerializer(connection).data)

    @extend_schema(
        tags=["Integrations - Apollo"],
        request=ApolloConnectionWriteSerializer,
        responses={200: ApolloConnectionSerializer},
    )
    def put(self, request):
        connection = ApolloConnection.objects.filter(org=request.org).first()
        serializer = ApolloConnectionWriteSerializer(
            data=request.data,
            context={"connection": connection},
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if connection is None:
            connection = ApolloConnection(
                org=request.org,
                api_key_ciphertext="",
            )
        if values.get("api_key"):
            connection.set_api_key(values["api_key"])
        if "is_active" in values:
            connection.is_active = values["is_active"]
        connection.full_clean()
        connection.save()
        return Response(ApolloConnectionSerializer(connection).data)


class LinkedInConnectionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - LinkedIn"],
        responses={200: LinkedInConnectionSerializer},
    )
    def get(self, request):
        connection = LinkedInConnection.objects.filter(org=request.org).first()
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(LinkedInConnectionSerializer(connection).data)

    @extend_schema(
        tags=["Integrations - LinkedIn"],
        request=LinkedInConnectionWriteSerializer,
        responses={200: LinkedInConnectionSerializer},
    )
    def put(self, request):
        connection = LinkedInConnection.objects.filter(org=request.org).first()
        serializer = LinkedInConnectionWriteSerializer(
            data=request.data,
            context={"connection": connection},
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if connection is None:
            connection = LinkedInConnection(
                org=request.org,
                access_token_ciphertext="",
            )
        if values.get("access_token"):
            connection.set_access_token(values["access_token"])
        for field_name in ("partner_access_confirmed", "is_active"):
            if field_name in values:
                setattr(connection, field_name, values[field_name])
        connection.full_clean()
        connection.save()
        return Response(LinkedInConnectionSerializer(connection).data)


def _feishu_protected_destination_change(connection, values) -> bool:
    if "app_id" in values and values["app_id"] != connection.app_id:
        return True
    return any(
        values.get(field_name)
        and values[field_name] != getattr(connection, field_name)
        for field_name in ("app_token", "table_id")
    )


class FeishuBaseConnectionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Feishu Base"],
        responses={200: FeishuBaseConnectionSerializer},
    )
    def get(self, request):
        connection = FeishuBaseConnection.objects.filter(org=request.org).first()
        if connection is None:
            return Response(
                {
                    "id": None,
                    "app_id": "",
                    "app_id_configured": False,
                    "app_secret_configured": False,
                    "app_token_configured": False,
                    "table_id_configured": False,
                    "field_mapping": {},
                    "is_active": False,
                    "last_validated_at": None,
                    "last_sync_at": None,
                    "sync_summary": {
                        "total": 0,
                        "pending": 0,
                        "queued": 0,
                        "syncing": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "skipped": 0,
                        "unknown": 0,
                        "external_erasure_pending": 0,
                        "external_erasure_completed": 0,
                    },
                    "created_at": None,
                    "updated_at": None,
                }
            )
        return Response(FeishuBaseConnectionSerializer(connection).data)

    @extend_schema(
        tags=["Integrations - Feishu Base"],
        request=FeishuBaseConnectionWriteSerializer,
        responses={200: FeishuBaseConnectionSerializer},
    )
    def put(self, request):
        with transaction.atomic():
            connection = FeishuBaseConnection.objects.select_for_update().filter(
                org=request.org
            ).first()
            serializer = FeishuBaseConnectionWriteSerializer(
                data=request.data,
                context={"connection": connection},
            )
            serializer.is_valid(raise_exception=True)
            values = serializer.validated_data
            if connection is None:
                connection = FeishuBaseConnection(org=request.org)
            elif _feishu_protected_destination_change(connection, values):
                protected_sync_exists = FeishuBaseSync.objects.filter(
                    org=request.org
                ).filter(
                    models.Q(record_id_ciphertext__gt="")
                    | models.Q(status=FeishuBaseSyncStatus.UNKNOWN)
                ).exists()
                if protected_sync_exists:
                    return Response(
                        {
                            "code": "feishu_destination_locked",
                            "detail": (
                                "Delete or reconcile existing Feishu Base records "
                                "before changing connection identifiers."
                            ),
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
            if values.get("app_secret"):
                connection.set_app_secret(values["app_secret"])
            for field_name in ("app_id", "field_mapping", "is_active"):
                if field_name in values:
                    setattr(connection, field_name, values[field_name])
            # Empty identifier values mean "keep the stored value". This lets
            # the UI replace a destination without reading current tokens.
            for field_name in ("app_token", "table_id"):
                if values.get(field_name):
                    setattr(connection, field_name, values[field_name])
            connection.full_clean()
            connection.save()
        return Response(FeishuBaseConnectionSerializer(connection).data)


class FeishuBaseConnectionTestView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Feishu Base"],
        request=FeishuBaseExecutionWriteSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request):
        connection = FeishuBaseConnection.objects.filter(org=request.org).first()
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = FeishuBaseExecutionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not serializer.validated_data:
                intent = feishu_schema_execution_intent(connection)
                return Response(
                    {"approval_required": True, "intent": intent.as_dict()}
                )
            job = enqueue_feishu_schema_validation(
                connection=connection,
                **serializer.validated_data,
            )
        except FeishuBaseSyncUnavailable as exc:
            return _feishu_base_error(exc)
        return _feishu_job_response(job)


class FeishuBaseResearchSyncView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Feishu Base"],
        request=FeishuBaseExecutionWriteSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request, intake_id):
        intake = LeadIntake.objects.filter(org=request.org, id=intake_id).first()
        if intake is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = FeishuBaseExecutionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not serializer.validated_data:
                intent = feishu_research_sync_execution_intent(intake=intake)
                return Response(
                    {"approval_required": True, "intent": intent.as_dict()}
                )
            job = enqueue_feishu_base_sync(
                intake=intake,
                **serializer.validated_data,
            )
        except FeishuBaseSyncUnavailable as exc:
            return _feishu_base_error(exc)
        return _feishu_job_response(job)


class FeishuBaseSyncListView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["Integrations - Feishu Base"], responses={200: dict})
    def get(self, request):
        rows = (
            FeishuBaseSync.objects.filter(org=request.org)
            .select_related("execution_request")
            .order_by("-updated_at", "-id")[:100]
        )
        return Response(
            {
                "results": [
                    {
                        "id": row.id,
                        "intake_id": row.intake_id,
                        "safe_label": row.record_safe_label
                        or f"Research sync {str(row.id)[:8]}",
                        "status": row.status,
                        "external_erasure_status": _feishu_erasure_status(row),
                        "can_delete": row.has_remote_record
                        and row.status
                        in {
                            FeishuBaseSyncStatus.SUCCEEDED,
                            FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING,
                        },
                        "updated_at": row.updated_at,
                    }
                    for row in rows
                ]
            }
        )


class FeishuBaseRemoteDeleteView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Feishu Base"],
        request=FeishuBaseExecutionWriteSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request, sync_id):
        sync = (
            FeishuBaseSync.objects.filter(org=request.org, id=sync_id)
            .select_related("connection", "intake")
            .first()
        )
        if sync is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = FeishuBaseExecutionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not serializer.validated_data:
                intent = feishu_delete_execution_intent(sync)
                return Response(
                    {"approval_required": True, "intent": intent.as_dict()}
                )
            job = enqueue_feishu_remote_delete(
                sync=sync,
                **serializer.validated_data,
            )
        except FeishuBaseSyncUnavailable as exc:
            return _feishu_base_error(exc)
        return _feishu_job_response(job)


class FeishuBasePersonImportPreviewView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - Feishu Base"],
        request=FeishuBasePersonImportWriteSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request):
        connection = FeishuBaseConnection.objects.filter(org=request.org).first()
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = FeishuBasePersonImportWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        mapping = data.pop("mapping")
        row_limit = data.pop("limit")
        try:
            if not data:
                intent = feishu_person_import_execution_intent(
                    connection=connection,
                    mapping=mapping,
                    row_limit=row_limit,
                )
                return Response(
                    {"approval_required": True, "intent": intent.as_dict()}
                )
            result = enqueue_feishu_person_import(
                connection=connection,
                requested_by=request.profile,
                mapping=mapping,
                row_limit=row_limit,
                **data,
            )
        except FeishuBaseSyncUnavailable as exc:
            return _feishu_base_error(exc)
        return Response(
            _safe_feishu_person_import(result.person_import, replayed=result.replayed),
            status=status.HTTP_202_ACCEPTED,
        )


class FeishuBasePersonImportDetailView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["Integrations - Feishu Base"], responses={200: dict})
    def get(self, request, import_id):
        person_import = (
            FeishuBasePersonImport.objects.filter(org=request.org, id=import_id)
            .select_related("automation_job", "import_batch")
            .first()
        )
        if person_import is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(_safe_feishu_person_import(person_import))


def _feishu_base_error(exc):
    forbidden = {
        "environment_execution_disabled",
        "organization_execution_disabled",
        "channel_disabled",
        "target_not_allowlisted",
    }
    response_status = (
        status.HTTP_403_FORBIDDEN
        if exc.code in forbidden
        else status.HTTP_409_CONFLICT
    )
    return Response(
        {"code": exc.code, "detail": str(exc)},
        status=response_status,
    )


def _safe_feishu_person_import(person_import, *, replayed=False):
    job = person_import.automation_job
    return {
        "id": person_import.id,
        "status": person_import.status,
        "job_id": job.id if job else None,
        "job_status": job.status if job else None,
        "status_url": (
            f"/api/integrations/feishu-base/person-imports/{person_import.id}/"
        ),
        "batch_id": person_import.import_batch_id,
        "error_code": person_import.error_code,
        "total_count": person_import.total_count,
        "ready_count": person_import.ready_count,
        "invalid_count": person_import.invalid_count,
        "created_at": person_import.created_at,
        "completed_at": person_import.completed_at,
        "replayed": replayed,
    }


def _feishu_job_response(job):
    return Response(
        {
            "job_id": job.id,
            "execution_request_id": job.payload["execution_request_id"],
            "status": job.status,
        },
        status=status.HTTP_202_ACCEPTED,
    )


def _feishu_erasure_status(sync):
    if sync.status == FeishuBaseSyncStatus.EXTERNAL_ERASURE_COMPLETED:
        return "completed"
    if sync.status == FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING:
        return "pending"
    if (
        sync.status == FeishuBaseSyncStatus.UNKNOWN
        and sync.execution_request_id
        and sync.execution_request.action == "delete_research_record"
    ):
        return "unknown"
    return "available" if sync.has_remote_record else "none"


WHATSAPP_APPROVABLE_MESSAGE_STATUSES = frozenset(
    {
        WhatsAppMessageStatus.PENDING,
        WhatsAppMessageStatus.QUEUED,
    }
)


def _safe_whatsapp_message(message):
    execution_request = message.execution_request
    return {
        "id": message.id,
        "campaign_id": message.campaign_id,
        "prospect_id": message.prospect_id,
        "status": message.status,
        "execution_request_id": message.execution_request_id,
        "execution_status": (
            execution_request.status if execution_request is not None else None
        ),
        "created_at": message.created_at,
    }


def _whatsapp_execution_intent_payload(*, message, intent):
    return {
        "channel": ExecutionChannel.WHATSAPP,
        "action": WHATSAPP_SEND_ACTION,
        "message_id": str(message.id),
        "target_sha256": intent.target_hash,
        "payload_sha256": intent.payload_hash,
        "units": intent.units,
    }


def _whatsapp_execution_unavailable():
    return Response(
        {
            "code": "whatsapp_execution_unavailable",
            "detail": "The WhatsApp message cannot be approved in its current state.",
        },
        status=status.HTTP_409_CONFLICT,
    )


def _whatsapp_non_reserved_replay_response(execution_request):
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
            "code": "whatsapp_execution_not_replayable",
            "detail": (
                "This WhatsApp execution has already left RESERVED state; "
                "no provider job was queued."
            ),
            "execution_request_id": str(execution_request.id),
            "execution_status": execution_request.status,
            "replayed": True,
        },
        status=response_status,
    )


class WhatsAppMessageListView(APIView):
    """Reviewable, strictly non-PII WhatsApp execution queue."""

    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(tags=["Integrations - WhatsApp"], responses={200: dict})
    def get(self, request):
        serializer = WhatsAppMessageListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = serializer.validated_data
        messages = WhatsAppMessage.objects.filter(
            org=request.org,
            campaign__org=request.org,
            prospect__org=request.org,
            connection__org=request.org,
        ).select_related("execution_request")
        if filters.get("campaign_id"):
            messages = messages.filter(campaign_id=filters["campaign_id"])
        if filters.get("status"):
            messages = messages.filter(status=filters["status"])
        count = messages.count()
        messages = messages.order_by("-created_at", "-id")[: filters["limit"]]
        return Response(
            {
                "count": count,
                "results": [_safe_whatsapp_message(message) for message in messages],
            }
        )


class WhatsAppMessageExecutionView(APIView):
    """Preview or reserve one exact WhatsApp provider attempt."""

    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - WhatsApp"],
        request=WhatsAppMessageExecutionSerializer,
        responses={200: dict, 202: dict},
    )
    def post(self, request, message_id):
        message = (
            WhatsAppMessage.objects.filter(
                id=message_id,
                org=request.org,
                campaign__org=request.org,
                prospect__org=request.org,
                connection__org=request.org,
            )
            .select_related(
                "org",
                "connection__route",
                "campaign",
                "prospect",
                "execution_request",
            )
            .first()
        )
        if message is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer = WhatsAppMessageExecutionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if (
            message.execution_request_id
            and message.execution_request.status != ExternalRequestStatus.RESERVED
        ):
            return _whatsapp_non_reserved_replay_response(message.execution_request)
        if message.status not in WHATSAPP_APPROVABLE_MESSAGE_STATUSES:
            return _whatsapp_execution_unavailable()

        try:
            intent = whatsapp_message_execution_intent(message)
        except WhatsAppCampaignUnavailable:
            return _whatsapp_execution_unavailable()

        approval_id = serializer.validated_data.get("approval_id")
        if approval_id is None:
            return Response(
                {
                    "approval_required": True,
                    "intent": _whatsapp_execution_intent_payload(
                        message=message,
                        intent=intent,
                    ),
                }
            )

        try:
            submission = reserve_and_enqueue_whatsapp_message(
                message,
                approval_id=approval_id,
            )
        except ExecutionSafetyError as exc:
            return _channel_safety_error(exc)
        except WhatsAppCampaignUnavailable:
            return _whatsapp_execution_unavailable()
        if (
            submission.request.status != ExternalRequestStatus.RESERVED
            or submission.job is None
        ):
            return _whatsapp_non_reserved_replay_response(submission.request)
        return Response(
            {
                "job_id": str(submission.job.id),
                "status": submission.job.status,
                "execution_request_id": str(submission.request.id),
                "execution_status": submission.request.status,
                "replayed": submission.replayed,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class WhatsAppBusinessConnectionView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @extend_schema(
        tags=["Integrations - WhatsApp"],
        responses={200: WhatsAppBusinessConnectionSerializer},
    )
    def get(self, request):
        connection = (
            WhatsAppBusinessConnection.objects.filter(org=request.org)
            .select_related("route")
            .first()
        )
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(WhatsAppBusinessConnectionSerializer(connection).data)

    @extend_schema(
        tags=["Integrations - WhatsApp"],
        request=WhatsAppBusinessConnectionWriteSerializer,
        responses={200: WhatsAppBusinessConnectionSerializer},
    )
    def put(self, request):
        connection = (
            WhatsAppBusinessConnection.objects.filter(org=request.org)
            .select_related("route")
            .first()
        )
        serializer = WhatsAppBusinessConnectionWriteSerializer(
            data=request.data,
            context={"connection": connection},
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        phone_number_id = values["phone_number_id"]
        conflicting = WhatsAppPhoneRoute.objects.filter(
            phone_number_id=phone_number_id
        ).exclude(org=request.org)
        if conflicting.exists():
            return Response(
                {"detail": "This WhatsApp phone number is already connected."},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            route, _ = WhatsAppPhoneRoute.objects.get_or_create(
                phone_number_id=phone_number_id,
                defaults={"org": request.org},
            )
            if connection is None:
                connection = WhatsAppBusinessConnection(
                    org=request.org,
                    route=route,
                    access_token_ciphertext="",
                )
            else:
                previous_route = connection.route
                connection.route = route
            for field_name in (
                "business_account_id",
                "display_phone_number",
                "is_active",
            ):
                if field_name in values:
                    setattr(connection, field_name, values[field_name])
            if values.get("access_token"):
                connection.set_access_token(values["access_token"])
            connection.full_clean()
            connection.save()
            if "previous_route" in locals() and previous_route.id != route.id:
                previous_route.delete()
        connection = WhatsAppBusinessConnection.objects.select_related("route").get(
            id=connection.id,
            org=request.org,
        )
        return Response(WhatsAppBusinessConnectionSerializer(connection).data)


class WhatsAppWebhookView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    @extend_schema(tags=["Integrations - WhatsApp"], responses={200: str})
    def get(self, request):
        if (
            request.query_params.get("hub.mode") == "subscribe"
            and request.query_params.get("hub.verify_token")
            == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
            and settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
        ):
            return HttpResponse(request.query_params.get("hub.challenge", ""))
        return Response(status=status.HTTP_403_FORBIDDEN)

    @extend_schema(
        tags=["Integrations - WhatsApp"], request=dict, responses={200: dict}
    )
    def post(self, request):
        if not verify_whatsapp_signature(
            body=request.body,
            signature=request.headers.get("X-Hub-Signature-256", ""),
            app_secret=settings.META_APP_SECRET,
        ):
            return Response(status=status.HTTP_403_FORBIDDEN)
        result = process_whatsapp_status_webhook(request.data)
        return Response(result)


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

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookMessengerSettingsSerializer,
        responses={200: FacebookPageConnectionSerializer},
    )
    def patch(self, request, connection_id):
        serializer = FacebookMessengerSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = (
            FacebookPageConnection.objects.filter(
                id=connection_id,
                org=request.org,
            )
            .select_related("route")
            .first()
        )
        if connection is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        requested = serializer.validated_data
        messenger_enabled = requested.get(
            "messenger_enabled",
            connection.messenger_enabled,
        )
        auto_reply_enabled = requested.get(
            "messenger_auto_reply_enabled",
            connection.messenger_auto_reply_enabled,
        )
        if (
            requested.get("messenger_auto_reply_enabled") is True
            and not messenger_enabled
        ):
            return Response(
                {
                    "detail": (
                        "Messenger intake must be enabled before automatic replies."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not messenger_enabled:
            auto_reply_enabled = False
        try:
            if (
                "messenger_enabled" in requested
                and messenger_enabled != connection.messenger_enabled
            ):
                set_facebook_messenger_enabled(
                    connection,
                    enabled=messenger_enabled,
                )
        except FacebookGraphAPIError as exc:
            response_status = (
                status.HTTP_502_BAD_GATEWAY
                if exc.retryable
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(
                {
                    "detail": (
                        "Meta could not enable Messenger for this Page. "
                        "Reconnect with the pages_messaging permission."
                    )
                },
                status=response_status,
            )
        update_fields = []
        if "messenger_auto_reply_enabled" in requested:
            connection.messenger_auto_reply_enabled = auto_reply_enabled
            update_fields.append("messenger_auto_reply_enabled")
        if "messenger_auto_reply_template" in requested:
            connection.messenger_auto_reply_template = requested[
                "messenger_auto_reply_template"
            ]
            update_fields.append("messenger_auto_reply_template")
        if update_fields:
            connection.save(update_fields=[*update_fields, "updated_at"])
        return Response(FacebookPageConnectionSerializer(connection).data)


class FacebookMessengerConversationView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext)

    @staticmethod
    def _lead_and_intake(request, lead_id):
        lead = Lead.objects.filter(id=lead_id, org=request.org).first()
        if lead is None:
            raise NotFound("Lead not found.")
        intake = (
            LeadIntake.objects.filter(
                org=request.org,
                crm_lead=lead,
                source="facebook_messenger",
            )
            .select_related("assigned_profile")
            .order_by("-created_at")
            .first()
        )
        is_admin = (
            request.user.is_superuser
            or request.profile.role == "ADMIN"
            or request.profile.is_organization_admin
        )
        is_assigned = lead.assigned_to.filter(id=request.profile.id).exists() or (
            intake is not None and intake.assigned_profile_id == request.profile.id
        )
        if not is_admin and lead.created_by_id != request.user.id and not is_assigned:
            raise PermissionDenied("You do not have access to this lead conversation.")
        return lead, intake

    @staticmethod
    def _conversation_payload(lead, intake):
        if intake is None:
            return {
                "available": False,
                "lead_id": str(lead.id),
                "messages": [],
                "can_reply": False,
                "reply_unavailable_reason": "not_messenger_lead",
            }

        inbound_messages = list(
            FacebookMessengerMessage.objects.filter(
                org_id=intake.org_id,
                intake=intake,
            )
            .select_related("connection")
            .order_by("occurred_at", "created_at")
        )
        replies = list(
            FacebookMessengerReply.objects.filter(
                org_id=intake.org_id,
                trigger_message__intake=intake,
            )
            .select_related("created_by")
            .order_by("created_at")
        )
        timeline = []
        for message in inbound_messages:
            timeline.append(
                {
                    "id": str(message.id),
                    "direction": "inbound",
                    "body": message.body,
                    "attachment_types": list(message.attachment_types),
                    "status": message.status,
                    "kind": "customer",
                    "provider_message_id": message.message_id,
                    "sent_by": "Customer",
                    "timestamp": message.occurred_at,
                    "error_code": message.error_code,
                }
            )
        for reply in replies:
            timeline.append(
                {
                    "id": str(reply.id),
                    "direction": "outbound",
                    "body": reply.body,
                    "attachment_types": [],
                    "status": reply.status,
                    "kind": reply.kind,
                    "provider_message_id": reply.provider_message_id,
                    "sent_by": (
                        reply.created_by.email if reply.created_by else "Automation"
                    ),
                    "timestamp": reply.sent_at or reply.created_at,
                    "error_code": reply.error_code,
                }
            )
        timeline.sort(key=lambda item: item["timestamp"])
        for item in timeline:
            item["timestamp"] = item["timestamp"].isoformat()

        latest = inbound_messages[-1] if inbound_messages else None
        connection = latest.connection if latest else None
        window_expires_at = (
            latest.occurred_at + timedelta(hours=24) if latest is not None else None
        )
        restriction = intake_data_restriction(intake)
        can_reply = bool(
            latest
            and connection
            and connection.is_active
            and connection.messenger_enabled
            and not restriction
            and window_expires_at
            and window_expires_at > timezone.now()
        )
        if restriction:
            unavailable_reason = restriction.code
        elif latest is None:
            unavailable_reason = "no_inbound_message"
        elif connection is None or not connection.is_active:
            unavailable_reason = "page_disconnected"
        elif not connection.messenger_enabled:
            unavailable_reason = "messenger_disabled"
        elif not can_reply:
            unavailable_reason = "outside_messaging_window"
        else:
            unavailable_reason = ""
        return {
            "available": True,
            "intake_id": str(intake.id),
            "lead_id": str(lead.id),
            "page_id": latest.page_id if latest else "",
            "page_name": connection.page_name if connection else "Facebook Page",
            "messages": timeline,
            "can_reply": can_reply,
            "reply_window_expires_at": (
                window_expires_at.isoformat() if window_expires_at else None
            ),
            "reply_unavailable_reason": unavailable_reason,
        }

    @extend_schema(tags=["Integrations - Facebook"])
    def get(self, request, lead_id):
        lead, intake = self._lead_and_intake(request, lead_id)
        return Response(self._conversation_payload(lead, intake))

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookMessengerManualReplySerializer,
    )
    def post(self, request, lead_id):
        lead, intake = self._lead_and_intake(request, lead_id)
        if intake is None:
            raise NotFound("This lead does not have a Messenger conversation.")
        serializer = FacebookMessengerManualReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            accepted = enqueue_manual_facebook_reply(
                intake=intake,
                body=serializer.validated_data["body"],
                client_request_id=serializer.validated_data["client_request_id"],
                created_by_id=request.user.id,
            )
        except FacebookMessengerReplyUnavailable as exc:
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reply = FacebookMessengerReply.objects.get(
            id=accepted.reply_id,
            org=request.org,
        )
        return Response(
            {
                "reply_id": str(reply.id),
                "status": reply.status,
                "job_id": str(accepted.job_id),
                "replayed": accepted.replayed,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class FacebookConversionSettingsView(APIView):
    permission_classes = (IsAuthenticated, HasOrgContext, IsOrgAdmin)

    @staticmethod
    def _configuration(request):
        configuration, _ = FacebookConversionSettings.objects.get_or_create(
            org=request.org
        )
        return configuration

    @extend_schema(
        tags=["Integrations - Facebook"],
        responses={200: FacebookConversionSettingsSerializer},
    )
    def get(self, request):
        return Response(
            FacebookConversionSettingsSerializer(self._configuration(request)).data
        )

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookConversionSettingsSerializer,
        responses={200: FacebookConversionSettingsSerializer},
    )
    def put(self, request):
        return self._save(request, partial=False)

    @extend_schema(
        tags=["Integrations - Facebook"],
        request=FacebookConversionSettingsSerializer,
        responses={200: FacebookConversionSettingsSerializer},
    )
    def patch(self, request):
        return self._save(request, partial=True)

    def _save(self, request, *, partial):
        serializer = FacebookConversionSettingsSerializer(
            self._configuration(request),
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        configuration = serializer.save()
        backfilled = 0
        if configuration.is_enabled:
            backfilled = reconcile_recent_conversion_events(org_id=request.org.id)
        response_data = dict(FacebookConversionSettingsSerializer(configuration).data)
        response_data["backfilled_events"] = backfilled
        return Response(response_data)


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
            lead_events = adapter.parse_events(body=body)
            message_events = adapter.parse_message_events(body=body)
        except FacebookWebhookError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            for event in lead_events:
                try:
                    enqueue_facebook_lead_event(event.as_payload())
                except FacebookConnectionUnavailable:
                    logger.warning(
                        "Ignoring Facebook lead webhook for an unconnected Page: %s",
                        event.page_id,
                    )
            for event in message_events:
                try:
                    enqueue_facebook_message_event(event.as_payload())
                except FacebookMessengerUnavailable:
                    logger.info(
                        "Ignoring Messenger webhook for a disabled Page: %s",
                        event.page_id,
                    )
        except Exception:
            logger.exception("Could not persist or dispatch Facebook lead webhook")
            return Response(
                {"detail": "Lead processing queue is unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "status": "accepted",
                "events": len(lead_events) + len(message_events),
                "lead_events": len(lead_events),
                "messenger_events": len(message_events),
            }
        )
