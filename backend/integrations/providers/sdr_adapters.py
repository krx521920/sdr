"""Concrete provider adapters registered behind SDR-owned runtime ports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import Count, Q

from integrations.models import (
    ApolloConnection,
    FeishuBaseConnection,
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
    ProviderAdapterError,
    register_outbound_channel_adapter,
    register_prospect_source_adapter,
    register_provider_data_governance_adapter,
    register_research_result_sink_adapter,
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
    register_research_result_sink_adapter(
        "feishu_base",
        FeishuBaseResearchResultSinkAdapter(),
    )


def _apollo_error(exc: ApolloAPIError) -> ProviderAdapterError:
    return ProviderAdapterError(
        str(exc),
        error_code=exc.error_code,
        retryable=exc.retryable,
    )


def _rate(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 1)
