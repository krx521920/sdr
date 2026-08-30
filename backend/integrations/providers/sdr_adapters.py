"""Concrete provider adapters registered behind SDR-owned runtime ports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import Count, Q

from integrations import execution_safety as integration_execution_safety
from integrations.models import (
    ApolloConnection,
    ExternalExecutionRequest,
    FacebookMessengerMessage,
    FacebookMessengerMessageStatus,
    FacebookMessengerReply,
    FacebookMessengerReplyStatus,
    FeishuBaseConnection,
    FeishuBaseSync,
    FeishuBaseSyncStatus,
    LinkedInConnection,
    LinkedInInvitation,
    LinkedInInvitationStatus,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
)
from integrations.providers.apollo import ApolloAPIError, ApolloClient
from integrations.providers.feishu_base import sync as feishu_base_sync
from integrations.providers.linkedin import outbound as linkedin_outbound
from integrations.providers.whatsapp import outbound as whatsapp_outbound
from sdr.provider_ports import (
    ExecutionReservation,
    ExecutionSafetyError,
    ProviderAdapterError,
    register_execution_safety_adapter,
    register_outbound_channel_adapter,
    register_prospect_source_adapter,
    register_provider_data_governance_adapter,
    register_research_result_sink_adapter,
)


def _execution_safety_call(operation, /, **kwargs):
    try:
        return operation(**kwargs)
    except integration_execution_safety.ExecutionSafetyError as exc:
        raise ExecutionSafetyError(
            code=exc.code,
            detail=exc.detail,
            status_code=exc.status_code,
        ) from exc


class IntegrationExecutionSafetyAdapter:
    """Expose integration-owned execution controls through an SDR-owned port."""

    def assert_provider_io_authorized(
        self,
        *,
        org,
        channel: str,
        action: str,
        execution_request_id,
    ) -> None:
        _execution_safety_call(
            integration_execution_safety.assert_provider_io_authorized,
            org=org,
            channel=channel,
            action=action,
            execution_request_id=execution_request_id,
        )

    def hash_target_identifier(
        self,
        *,
        org,
        channel: str,
        identifier: str,
    ) -> str:
        return _execution_safety_call(
            integration_execution_safety.hash_target_identifier,
            org=org,
            channel=channel,
            identifier=identifier,
        )

    def reserve_execution(self, **kwargs) -> ExecutionReservation:
        reservation = _execution_safety_call(
            integration_execution_safety.reserve_execution,
            **kwargs,
        )
        return ExecutionReservation(
            request=reservation.request,
            replayed=reservation.replayed,
        )

    def get_request(
        self,
        *,
        request_id,
        org=None,
        org_id=None,
        status=None,
        for_update: bool = False,
        include_org: bool = False,
    ):
        if (org is None) == (org_id is None):
            raise ValueError("Exactly one of org or org_id is required.")
        query = ExternalExecutionRequest.objects.filter(id=request_id)
        query = query.filter(org=org) if org is not None else query.filter(org_id=org_id)
        if status is not None:
            query = query.filter(status=status)
        if for_update:
            query = query.select_for_update()
        if include_org:
            query = query.select_related("org")
        return query.first()

    def mark_sending(self, **kwargs):
        return _execution_safety_call(
            integration_execution_safety.mark_execution_sending,
            **kwargs,
        )

    def mark_provider_accepted(self, **kwargs):
        return _execution_safety_call(
            integration_execution_safety.mark_provider_accepted,
            **kwargs,
        )

    def release(self, **kwargs):
        return _execution_safety_call(
            integration_execution_safety.release_execution,
            **kwargs,
        )

    def mark_delivered(self, **kwargs):
        return _execution_safety_call(
            integration_execution_safety.mark_execution_delivered,
            **kwargs,
        )

    def reconcile_stale_reserved(self, **kwargs):
        return _execution_safety_call(
            integration_execution_safety.reconcile_stale_reserved,
            **kwargs,
        )

    def reconcile_stale_sending(self, **kwargs):
        return _execution_safety_call(
            integration_execution_safety.reconcile_stale_sending,
            **kwargs,
        )


class ApolloClientAdapter:
    def __init__(self, client: ApolloClient):
        self.client = client

    def search_people(self, **kwargs) -> Mapping[str, Any]:
        try:
            return self.client.search_people(**kwargs)
        except ApolloAPIError as exc:
            raise _apollo_error(exc) from exc

    def enrich_person(self, **kwargs) -> Mapping[str, Any] | None:
        try:
            return self.client.enrich_person(**kwargs)
        except ApolloAPIError as exc:
            raise _apollo_error(exc) from exc

    def for_execution(self, *, org, action: str, execution_request_id):
        return ApolloClientAdapter(
            self.client.for_execution(
                org=org,
                action=action,
                execution_request_id=execution_request_id,
            )
        )


class ApolloProspectSourceAdapter:
    def is_ready(self, *, org_id: UUID) -> bool:
        return ApolloConnection.objects.filter(org_id=org_id, is_active=True).exists()

    def client_for(self, *, org_id: UUID):
        connection = ApolloConnection.objects.filter(
            org_id=org_id,
            is_active=True,
        ).first()
        if connection is None:
            return None
        return ApolloClientAdapter(
            ApolloClient(
                api_key=connection.get_api_key(),
                base_url=settings.APOLLO_API_BASE_URL,
                timeout=settings.APOLLO_API_TIMEOUT,
            )
        )

    def mark_synced(self, *, org_id: UUID, synced_at: datetime) -> None:
        ApolloConnection.objects.filter(org_id=org_id).update(last_sync_at=synced_at)


class WhatsAppOutboundChannelAdapter:
    def is_ready(self, *, org_id: UUID) -> bool:
        return (
            WhatsAppBusinessConnection.objects.filter(
                org_id=org_id,
                is_active=True,
            )
            .exclude(access_token_ciphertext="")
            .exists()
        )

    def enqueue(self, *, prospect, campaign, campaign_run: int):
        try:
            return whatsapp_outbound.enqueue_whatsapp_campaign_message(
                prospect=prospect,
                campaign=campaign,
                campaign_run=campaign_run,
            )
        except whatsapp_outbound.WhatsAppCampaignUnavailable as exc:
            raise ProviderAdapterError(
                str(exc),
                error_code="whatsapp_campaign_unavailable",
            ) from exc

    def retry_failed(self, *, campaign) -> int:
        try:
            return whatsapp_outbound.retry_failed_whatsapp_messages(campaign)
        except whatsapp_outbound.WhatsAppCampaignUnavailable as exc:
            raise ProviderAdapterError(
                str(exc),
                error_code="whatsapp_campaign_unavailable",
            ) from exc

    def campaign_metrics(self, *, org_id: UUID, campaign_id: UUID):
        messages = WhatsAppMessage.objects.filter(
            org_id=org_id,
            campaign_id=campaign_id,
        )
        summary = messages.aggregate(
            queued=Count(
                "id",
                filter=Q(
                    status__in=(
                        WhatsAppMessageStatus.PENDING,
                        WhatsAppMessageStatus.QUEUED,
                        WhatsAppMessageStatus.SENDING,
                    )
                ),
            ),
            sent=Count("id", filter=Q(sent_at__isnull=False)),
            delivered=Count("id", filter=Q(delivered_at__isnull=False)),
            read=Count("id", filter=Q(read_at__isnull=False)),
            failed=Count("id", filter=Q(status=WhatsAppMessageStatus.FAILED)),
        )
        values = {key: int(value or 0) for key, value in summary.items()}
        values.update(
            {
                "delivery_rate": _rate(values["delivered"], values["sent"]),
                "read_rate": _rate(values["read"], values["sent"]),
            }
        )
        return values


class LinkedInOutboundChannelAdapter:
    def is_ready(self, *, org_id: UUID) -> bool:
        return (
            LinkedInConnection.objects.filter(
                org_id=org_id,
                is_active=True,
                partner_access_confirmed=True,
            )
            .exclude(access_token_ciphertext="")
            .exists()
        )

    def enqueue(self, *, prospect, campaign, campaign_run: int):
        try:
            return linkedin_outbound.enqueue_linkedin_campaign_invitation(
                prospect=prospect,
                campaign=campaign,
                campaign_run=campaign_run,
            )
        except linkedin_outbound.LinkedInCampaignUnavailable as exc:
            raise ProviderAdapterError(
                str(exc),
                error_code="linkedin_campaign_unavailable",
            ) from exc

    def retry_failed(self, *, campaign) -> int:
        try:
            return linkedin_outbound.retry_failed_linkedin_invitations(campaign)
        except linkedin_outbound.LinkedInCampaignUnavailable as exc:
            raise ProviderAdapterError(
                str(exc),
                error_code="linkedin_campaign_unavailable",
            ) from exc

    def campaign_metrics(self, *, org_id: UUID, campaign_id: UUID):
        invitations = LinkedInInvitation.objects.filter(
            org_id=org_id,
            campaign_id=campaign_id,
        )
        summary = invitations.aggregate(
            queued=Count(
                "id",
                filter=Q(
                    status__in=(
                        LinkedInInvitationStatus.PENDING,
                        LinkedInInvitationStatus.QUEUED,
                        LinkedInInvitationStatus.SENDING,
                    )
                ),
            ),
            sent=Count("id", filter=Q(status=LinkedInInvitationStatus.SENT)),
            failed=Count("id", filter=Q(status=LinkedInInvitationStatus.FAILED)),
            skipped=Count("id", filter=Q(status=LinkedInInvitationStatus.SKIPPED)),
        )
        return {key: int(value or 0) for key, value in summary.items()}


class WhatsAppDataGovernanceAdapter:
    def anonymize_intake_data(
        self,
        *,
        org_id: UUID,
        intake_id: UUID,
        marker: str,
    ) -> Mapping[str, int]:
        messages = WhatsAppMessage.objects.filter(
            org_id=org_id,
            prospect__intake_id=intake_id,
        )
        redacted = messages.update(
            recipient=f"redacted:{marker}",
            provider_message_id="",
            provider_status_snapshot={},
            error_message="",
        )
        skipped = messages.filter(
            status__in=(
                WhatsAppMessageStatus.PENDING,
                WhatsAppMessageStatus.QUEUED,
                WhatsAppMessageStatus.SENDING,
                WhatsAppMessageStatus.FAILED,
            )
        ).update(
            status=WhatsAppMessageStatus.SKIPPED,
            error_code="data_anonymized",
            error_message="SDR-owned personal data was anonymized.",
        )
        return {"redacted": redacted, "skipped": skipped}


class LinkedInDataGovernanceAdapter:
    def anonymize_intake_data(
        self,
        *,
        org_id: UUID,
        intake_id: UUID,
        marker: str,
    ) -> Mapping[str, int]:
        invitations = LinkedInInvitation.objects.filter(
            org_id=org_id,
            prospect__intake_id=intake_id,
        )
        redacted = invitations.update(
            recipient=f"redacted+{marker}@invalid.local",
            message_body="",
            provider_invitation_id="",
            provider_status_snapshot={},
            error_message="",
        )
        skipped = invitations.filter(
            status__in=(
                LinkedInInvitationStatus.PENDING,
                LinkedInInvitationStatus.QUEUED,
                LinkedInInvitationStatus.SENDING,
                LinkedInInvitationStatus.FAILED,
            )
        ).update(
            status=LinkedInInvitationStatus.SKIPPED,
            error_code="data_anonymized",
            error_message="SDR-owned personal data was anonymized.",
        )
        return {"redacted": redacted, "skipped": skipped}


class FacebookMessengerDataGovernanceAdapter:
    def anonymize_intake_data(
        self,
        *,
        org_id: UUID,
        intake_id: UUID,
        marker: str,
    ) -> Mapping[str, int]:
        messages = list(
            FacebookMessengerMessage.objects.filter(
                org_id=org_id,
                intake_id=intake_id,
            ).only("id", "status")
        )
        for message in messages:
            values: dict[str, Any] = {
                "page_id": "",
                "sender_psid": f"redacted:{marker}",
                "message_id": f"redacted:{message.id.hex}",
                "body": "",
                "attachment_types": [],
                "error_code": "",
                "error_message": "",
            }
            if message.status in {
                FacebookMessengerMessageStatus.RECEIVED,
                FacebookMessengerMessageStatus.QUEUED,
                FacebookMessengerMessageStatus.FAILED,
            }:
                values.update(
                    status=FacebookMessengerMessageStatus.IGNORED,
                    error_code="data_anonymized",
                )
            FacebookMessengerMessage.objects.filter(
                id=message.id,
                org_id=org_id,
            ).update(**values)

        replies = list(
            FacebookMessengerReply.objects.filter(
                org_id=org_id,
                trigger_message__intake_id=intake_id,
            ).only("id", "status")
        )
        skipped = 0
        for reply in replies:
            values = {
                "page_id": "",
                "recipient_psid": f"redacted:{reply.id.hex}",
                "body": "",
                "provider_message_id": "",
                "error_code": "",
                "error_message": "",
            }
            if reply.status in {
                FacebookMessengerReplyStatus.PENDING,
                FacebookMessengerReplyStatus.QUEUED,
                FacebookMessengerReplyStatus.SENDING,
                FacebookMessengerReplyStatus.FAILED,
            }:
                values.update(
                    status=FacebookMessengerReplyStatus.SKIPPED,
                    error_code="data_anonymized",
                )
                skipped += 1
            FacebookMessengerReply.objects.filter(
                id=reply.id,
                org_id=org_id,
            ).update(**values)
        return {
            "facebook_messenger_redacted": len(messages) + len(replies),
            "facebook_messenger_skipped": skipped,
        }


class FeishuBaseDataGovernanceAdapter:
    """Prevent resync and record that a remote Base row still needs erasure."""

    def anonymize_intake_data(
        self,
        *,
        org_id: UUID,
        intake_id: UUID,
        marker: str,
    ) -> Mapping[str, int]:
        del marker
        syncs = FeishuBaseSync.objects.filter(org_id=org_id, intake_id=intake_id)
        # UNKNOWN may represent a successful remote create whose response was
        # lost before the record id could be encrypted locally. Never relabel
        # that case as local-only or erase its manual reconciliation signal.
        manual_reconciliation = syncs.filter(
            status=FeishuBaseSyncStatus.UNKNOWN
        ).count()
        decided = syncs.exclude(status=FeishuBaseSyncStatus.UNKNOWN)
        pending = decided.exclude(record_id_ciphertext="").update(
            status=FeishuBaseSyncStatus.EXTERNAL_ERASURE_PENDING,
            synced_field_names=[],
            error_code="pending_external_erasure",
            error_message="Remote erasure must be completed and verified.",
            failed_at=None,
        )
        local_only = decided.filter(record_id_ciphertext="").update(
            status=FeishuBaseSyncStatus.SKIPPED,
            synced_field_names=[],
            error_code="data_anonymized",
            error_message="",
            failed_at=None,
        )
        return {
            "feishu_pending_external_erasure": pending,
            "feishu_local_sync_skipped": local_only,
            "feishu_manual_reconciliation_required": manual_reconciliation,
        }


class FeishuBaseResearchResultSinkAdapter:
    def is_ready(self, *, org_id: UUID) -> bool:
        return (
            FeishuBaseConnection.objects.filter(org_id=org_id, is_active=True)
            .exclude(app_secret_ciphertext="")
            .exists()
        )

    def enqueue(self, *, intake):
        try:
            return feishu_base_sync.enqueue_feishu_base_sync(intake=intake)
        except feishu_base_sync.FeishuBaseSyncUnavailable as exc:
            raise ProviderAdapterError(
                str(exc),
                error_code="feishu_base_sync_unavailable",
            ) from exc


def register_sdr_provider_adapters() -> None:
    register_execution_safety_adapter(IntegrationExecutionSafetyAdapter())
    register_prospect_source_adapter("apollo", ApolloProspectSourceAdapter())
    register_outbound_channel_adapter("whatsapp", WhatsAppOutboundChannelAdapter())
    register_outbound_channel_adapter("linkedin", LinkedInOutboundChannelAdapter())
    register_provider_data_governance_adapter(
        "whatsapp",
        WhatsAppDataGovernanceAdapter(),
    )
    register_provider_data_governance_adapter(
        "linkedin",
        LinkedInDataGovernanceAdapter(),
    )
    register_provider_data_governance_adapter(
        "facebook_messenger",
        FacebookMessengerDataGovernanceAdapter(),
    )
    register_provider_data_governance_adapter(
        "feishu_base",
        FeishuBaseDataGovernanceAdapter(),
    )
    register_research_result_sink_adapter(
        "feishu_base",
        FeishuBaseResearchResultSinkAdapter(),
    )


def _apollo_error(exc: ApolloAPIError) -> ProviderAdapterError:
    # Apollo error bodies may echo sensitive request data.  The stable code is
    # sufficient for diagnosis and the message is persisted by AutomationJob.
    return ProviderAdapterError(
        "Apollo request did not complete successfully.",
        error_code=exc.error_code,
        retryable=exc.retryable,
    )


def _rate(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 1)
