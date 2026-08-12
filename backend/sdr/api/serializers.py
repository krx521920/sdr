from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from common.models import Profile
from common.utils import COUNTRIES
from sdr.domain import LeadSource, QualificationBand
from sdr.intelligence.registry import provider_catalog, provider_registry
from sdr.models import (
    EmailSuppressionReason,
    LeadDelivery,
    LeadDeliveryKind,
    LeadDeliveryStatus,
    LeadInspection,
    LeadIntake,
    LeadIntakeSource,
    LeadLifecycleEvent,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    NurtureDeliveryStatus,
    NurtureEnrollmentStatus,
    NurtureReplySentiment,
    OutboundCampaignStatus,
    OutboundProspectStatus,
    SDREmailSuppression,
    SDRIntelligenceSettings,
    SDRModelCredential,
    SDRModelProvider,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRResponseSettings,
    SDRRoutingRule,
    SDRRoutingRuleMember,
)
from sdr.response import validate_feishu_webhook_url, validate_message_template
from sdr.routing.service import normalize_country

VALID_COUNTRY_CODES = {code for code, _ in COUNTRIES}


class SDRAnalyticsQuerySerializer(serializers.Serializer):
    days = serializers.ChoiceField(choices=(7, 30, 90), default=30)


class SDROutboundCampaignSerializer(serializers.ModelSerializer):
    owner_id = serializers.UUIDField(required=False, allow_null=True)
    owner_name = serializers.SerializerMethodField()
    sequence_id = serializers.UUIDField(required=False, allow_null=True)
    sequence_name = serializers.SerializerMethodField()
    sequence_ready = serializers.SerializerMethodField()
    metrics = serializers.SerializerMethodField()

    class Meta:
        model = SDROutboundCampaign
        fields = (
            "id",
            "name",
            "description",
            "icp_description",
            "channels",
            "status",
            "owner_id",
            "owner_name",
            "sequence_id",
            "sequence_name",
            "sequence_ready",
            "daily_send_limit",
            "metrics",
            "launched_at",
            "completed_at",
            "run_count",
            "last_refilled_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "owner_name",
            "sequence_name",
            "sequence_ready",
            "metrics",
            "launched_at",
            "completed_at",
            "run_count",
            "last_refilled_at",
            "created_at",
            "updated_at",
        )

    def validate_channels(self, values):
        allowed = {"email", "linkedin", "phone", "whatsapp"}
        cleaned = list(dict.fromkeys(str(value).strip().lower() for value in values))
        if any(value not in allowed for value in cleaned):
            raise serializers.ValidationError("Select only supported outbound channels.")
        return cleaned

    def validate_name(self, value):
        org = self.context["org"]
        queryset = SDROutboundCampaign.objects.filter(
            org=org,
            name__iexact=value.strip(),
        )
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        if queryset.exists():
            raise serializers.ValidationError(
                "An outbound campaign with this name already exists."
            )
        return value.strip()

    def validate_owner_id(self, value):
        if value is None:
            return None
        org = self.context["org"]
        if not Profile.objects.filter(id=value, org=org, is_active=True).exists():
            raise serializers.ValidationError("Select an active member of this organization.")
        return value

    def validate_sequence_id(self, value):
        if value is None:
            return None
        org = self.context["org"]
        if not SDRNurtureSequence.objects.filter(id=value, org=org).exists():
            raise serializers.ValidationError(
                "Select a nurture sequence from this organization."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        next_status = attrs.get(
            "status",
            getattr(self.instance, "status", OutboundCampaignStatus.DRAFT),
        )
        if self.instance and next_status != self.instance.status:
            raise serializers.ValidationError(
                {"status": "Use a campaign action to change execution status."}
            )
        if self.instance is None and next_status != OutboundCampaignStatus.DRAFT:
            raise serializers.ValidationError(
                {"status": "New outbound campaigns must start as drafts."}
            )
        if self.instance and self.instance.status == OutboundCampaignStatus.ACTIVE:
            protected_changes = {
                "channels": attrs.get("channels", self.instance.channels)
                != self.instance.channels,
                "sequence_id": attrs.get("sequence_id", self.instance.sequence_id)
                != self.instance.sequence_id,
                "daily_send_limit": attrs.get(
                    "daily_send_limit", self.instance.daily_send_limit
                )
                != self.instance.daily_send_limit,
            }
            changed = [field for field, differs in protected_changes.items() if differs]
            if changed:
                raise serializers.ValidationError(
                    {
                        field: "Pause the campaign before changing execution settings."
                        for field in changed
                    }
                )
        return attrs

    def create(self, validated_data):
        owner_id = validated_data.pop("owner_id", None)
        sequence_id = validated_data.pop("sequence_id", None)
        return SDROutboundCampaign.objects.create(
            owner_id=owner_id,
            sequence_id=sequence_id,
            **validated_data,
        )

    def update(self, instance, validated_data):
        owner_id = validated_data.pop("owner_id", serializers.empty)
        sequence_id = validated_data.pop("sequence_id", serializers.empty)
        if owner_id is not serializers.empty:
            instance.owner_id = owner_id
        if sequence_id is not serializers.empty:
            instance.sequence_id = sequence_id
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance

    def get_owner_name(self, obj):
        if not obj.owner_id:
            return ""
        return obj.owner.user.name or obj.owner.user.email

    def get_sequence_name(self, obj):
        return obj.sequence.name if obj.sequence_id else ""

    def get_sequence_ready(self, obj):
        if not obj.sequence_id:
            return False
        return bool(
            obj.sequence.is_active
            and LeadIntakeSource.OUTBOUND in obj.sequence.sources
            and obj.sequence.from_email
            and obj.sequence.steps.exists()
        )

    def get_metrics(self, obj):
        keys = {
            "total": "prospect_total",
            "ready": "prospect_ready",
            "queued": "prospect_queued",
            "processing": "prospect_processing",
            "promoted": "prospect_promoted",
            "failed": "prospect_failed",
            "disqualified": "prospect_disqualified",
        }
        if all(hasattr(obj, attribute) for attribute in keys.values()):
            return {key: getattr(obj, attribute) for key, attribute in keys.items()}
        queryset = obj.prospects.all()
        return {
            "total": queryset.count(),
            **{
                value: queryset.filter(status=value).count()
                for value in OutboundProspectStatus.values
            },
        }


class SDROutboundProspectSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    intake_id = serializers.UUIDField(read_only=True, allow_null=True)
    lead_id = serializers.UUIDField(
        source="intake.crm_lead_id",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = SDROutboundProspect
        fields = (
            "id",
            "campaign_id",
            "campaign_name",
            "first_name",
            "last_name",
            "email",
            "phone",
            "job_title",
            "linkedin_url",
            "company_name",
            "website",
            "industry",
            "country",
            "source_url",
            "notes",
            "status",
            "attempt_count",
            "last_error_code",
            "last_error_message",
            "intake_id",
            "lead_id",
            "promoted_at",
            "queued_at",
            "queued_run",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class SDROutboundImportSerializer(serializers.Serializer):
    csv_text = serializers.CharField(max_length=1_000_000, trim_whitespace=False)
    promote_ready = serializers.BooleanField(default=False)


class SDROutboundProspectActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=("promote", "disqualify", "restore")
    )


class SDROutboundCampaignActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=("launch", "pause", "retry_failed", "complete", "archive")
    )


class SDRRoutingRuleMemberSerializer(serializers.ModelSerializer):
    profile_id = serializers.UUIDField(source="profile.id", read_only=True)
    email = serializers.EmailField(source="profile.user.email", read_only=True)
    name = serializers.CharField(source="profile.user.name", read_only=True)

    class Meta:
        model = SDRRoutingRuleMember
        fields = ("profile_id", "email", "name", "position")


class SDRRoutingRuleSerializer(serializers.ModelSerializer):
    members = SDRRoutingRuleMemberSerializer(many=True, read_only=True)
    profile_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        allow_empty=False,
    )
    countries = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
    )
    sources = serializers.ListField(
        child=serializers.ChoiceField(choices=LeadIntakeSource.choices),
        required=False,
        allow_empty=True,
    )
    qualification_bands = serializers.ListField(
        child=serializers.ChoiceField(
            choices=[(band.value, band.value.title()) for band in QualificationBand]
        ),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = SDRRoutingRule
        fields = (
            "id",
            "name",
            "priority",
            "is_active",
            "strategy",
            "countries",
            "sources",
            "qualification_bands",
            "members",
            "profile_ids",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "members", "created_at", "updated_at")

    def validate_countries(self, values):
        normalized = []
        for value in values:
            country = normalize_country(value)
            if country not in VALID_COUNTRY_CODES:
                raise serializers.ValidationError(f'Unknown country: "{value}"')
            if country not in normalized:
                normalized.append(country)
        return normalized

    def validate_profile_ids(self, values):
        profile_ids = list(dict.fromkeys(values))
        org = self.context["org"]
        profiles = Profile.objects.filter(
            id__in=profile_ids,
            org=org,
            is_active=True,
            has_sales_access=True,
        )
        if profiles.count() != len(profile_ids):
            raise serializers.ValidationError(
                "Every assignee must be an active sales user in this organization."
            )
        return profile_ids

    @transaction.atomic
    def create(self, validated_data):
        profile_ids = validated_data.pop("profile_ids")
        rule = SDRRoutingRule.objects.create(**validated_data)
        self._replace_members(rule, profile_ids)
        return rule

    @transaction.atomic
    def update(self, instance, validated_data):
        profile_ids = validated_data.pop("profile_ids", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if profile_ids is not None:
            self._replace_members(instance, profile_ids)
        return instance

    @staticmethod
    def _replace_members(rule, profile_ids):
        rule.members.all().delete()
        SDRRoutingRuleMember.objects.bulk_create(
            [
                SDRRoutingRuleMember(
                    org_id=rule.org_id,
                    rule=rule,
                    profile_id=profile_id,
                    position=position,
                )
                for position, profile_id in enumerate(profile_ids)
            ]
        )


class SDRRoutingPreviewSerializer(serializers.Serializer):
    country = serializers.CharField(max_length=100, required=False, allow_blank=True)
    source = serializers.ChoiceField(
        choices=[(source.value, source.value) for source in LeadSource],
        default=LeadSource.API.value,
    )
    qualification_band = serializers.ChoiceField(
        choices=[(band.value, band.value) for band in QualificationBand],
        default=QualificationBand.MEDIUM.value,
    )


class SDRIntelligenceSettingsSerializer(serializers.ModelSerializer):
    openai_configured = serializers.SerializerMethodField()
    allowed_models = serializers.SerializerMethodField()
    allowed_reasoning_efforts = serializers.SerializerMethodField()
    provider_catalog = serializers.SerializerMethodField()
    tenant_keys_allowed = serializers.SerializerMethodField()
    openai_api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=8192
    )
    doubao_api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=8192
    )
    deepseek_api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=8192
    )
    clear_openai_api_key = serializers.BooleanField(
        write_only=True, required=False, default=False
    )
    clear_doubao_api_key = serializers.BooleanField(
        write_only=True, required=False, default=False
    )
    clear_deepseek_api_key = serializers.BooleanField(
        write_only=True, required=False, default=False
    )

    class Meta:
        model = SDRIntelligenceSettings
        fields = (
            "is_enabled",
            "research_enabled",
            "ai_scoring_enabled",
            "provider",
            "model",
            "reasoning_effort",
            "fallback_provider",
            "fallback_model",
            "fallback_reasoning_effort",
            "icp_description",
            "positive_signals",
            "negative_signals",
            "max_research_pages",
            "website_timeout_seconds",
            "openai_configured",
            "allowed_models",
            "allowed_reasoning_efforts",
            "provider_catalog",
            "tenant_keys_allowed",
            "openai_api_key",
            "doubao_api_key",
            "deepseek_api_key",
            "clear_openai_api_key",
            "clear_doubao_api_key",
            "clear_deepseek_api_key",
            "updated_at",
        )
        read_only_fields = (
            "openai_configured",
            "allowed_models",
            "allowed_reasoning_efforts",
            "provider_catalog",
            "tenant_keys_allowed",
            "updated_at",
        )

    def get_openai_configured(self, obj):
        return self.get_provider_catalog(obj)["openai"]["configured"]

    def get_allowed_models(self, obj):
        definition = provider_registry().get(obj.provider)
        return list(definition.models) if definition else []

    def get_allowed_reasoning_efforts(self, obj):
        return settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS

    def get_provider_catalog(self, obj):
        catalog = provider_catalog()
        credentials = {}
        if settings.AI_GATEWAY_ALLOW_TENANT_KEYS:
            credentials = {
                credential.provider: credential
                for credential in SDRModelCredential.objects.filter(
                    org_id=obj.org_id,
                    is_active=True,
                )
            }
        for provider, item in catalog.items():
            credential = credentials.get(provider)
            if credential:
                item["configured"] = True
                item["credential_source"] = "tenant"
                item["key_hint"] = credential.api_key_hint
            elif item["platform_configured"]:
                item["configured"] = True
                item["credential_source"] = "platform"
                item["key_hint"] = ""
            else:
                item["configured"] = False
                item["credential_source"] = "none"
                item["key_hint"] = ""
        return catalog

    def get_tenant_keys_allowed(self, obj):
        return settings.AI_GATEWAY_ALLOW_TENANT_KEYS

    def validate_reasoning_effort(self, value):
        if value not in settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS:
            raise serializers.ValidationError(
                "This reasoning effort is not allowed by the platform deployment."
            )
        return value

    def validate_fallback_reasoning_effort(self, value):
        if value not in settings.AI_GATEWAY_ALLOWED_REASONING_EFFORTS:
            raise serializers.ValidationError(
                "This fallback reasoning effort is not allowed by the deployment."
            )
        return value

    def validate(self, attrs):
        registry = provider_registry()
        current = self.instance
        provider = attrs.get(
            "provider", getattr(current, "provider", SDRModelProvider.OPENAI)
        )
        model = attrs.get("model", getattr(current, "model", ""))
        errors = {}
        if provider not in registry or model not in registry[provider].models:
            errors["model"] = "This model is not allowed for the selected provider."

        fallback_provider = attrs.get(
            "fallback_provider", getattr(current, "fallback_provider", "")
        )
        fallback_model = attrs.get(
            "fallback_model", getattr(current, "fallback_model", "")
        )
        if fallback_provider:
            if (
                fallback_provider not in registry
                or fallback_model not in registry[fallback_provider].models
            ):
                errors["fallback_model"] = (
                    "Select an allowed model for the fallback provider."
                )
        else:
            attrs["fallback_model"] = ""

        credential_fields = [
            f"{provider_name}_api_key" for provider_name in SDRModelProvider.values
        ] + [
            f"clear_{provider_name}_api_key"
            for provider_name in SDRModelProvider.values
        ]
        if not settings.AI_GATEWAY_ALLOW_TENANT_KEYS and any(
            attrs.get(field) for field in credential_fields
        ):
            errors["api_keys"] = "Tenant-owned model API keys are disabled."

        for field in ("icp_description", "positive_signals", "negative_signals"):
            if len(attrs.get(field, getattr(self.instance, field, ""))) > 5000:
                errors[field] = "Keep this guidance under 5,000 characters."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        key_updates = {}
        clear_updates = {}
        for provider in SDRModelProvider.values:
            key_updates[provider] = validated_data.pop(f"{provider}_api_key", "")
            clear_updates[provider] = validated_data.pop(
                f"clear_{provider}_api_key", False
            )
        instance = super().update(instance, validated_data)
        if settings.AI_GATEWAY_ALLOW_TENANT_KEYS:
            for provider in SDRModelProvider.values:
                api_key = key_updates[provider].strip()
                if clear_updates[provider] and not api_key:
                    SDRModelCredential.objects.filter(
                        org_id=instance.org_id,
                        provider=provider,
                    ).delete()
                elif api_key:
                    credential, _ = SDRModelCredential.objects.get_or_create(
                        org_id=instance.org_id,
                        provider=provider,
                        defaults={"api_key_ciphertext": "pending"},
                    )
                    credential.set_api_key(api_key)
                    credential.is_active = True
                    credential.save()
        return instance


class LeadInspectionSerializer(serializers.ModelSerializer):
    intake_id = serializers.UUIDField(source="intake.id", read_only=True)
    lead_id = serializers.UUIDField(source="intake.crm_lead_id", read_only=True)
    source = serializers.CharField(source="intake.source", read_only=True)
    source_record_id = serializers.CharField(
        source="intake.source_record_id", read_only=True
    )
    company_name = serializers.SerializerMethodField()

    def get_company_name(self, obj):
        if obj.intake.crm_lead:
            return obj.intake.crm_lead.company_name or obj.intake.crm_lead.title or ""
        return obj.intake.normalized_payload.get("company", {}).get("name", "")

    class Meta:
        model = LeadInspection
        fields = (
            "id",
            "intake_id",
            "lead_id",
            "source",
            "source_record_id",
            "company_name",
            "status",
            "website_url",
            "source_urls",
            "research_summary",
            "research_facts",
            "content_sha256",
            "provider",
            "model",
            "prompt_version",
            "configuration_sha256",
            "qualification_score",
            "qualification_band",
            "qualification_reasons",
            "used_fallback",
            "fallback_kind",
            "provider_attempts",
            "error_code",
            "error_message",
            "input_tokens",
            "output_tokens",
            "started_at",
            "completed_at",
            "created_at",
        )
        read_only_fields = fields


class SDRResponseSettingsSerializer(serializers.ModelSerializer):
    feishu_webhook_url = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=2000,
    )
    clear_feishu_webhook = serializers.BooleanField(
        write_only=True,
        required=False,
        default=False,
    )
    feishu_configured = serializers.SerializerMethodField()

    class Meta:
        model = SDRResponseSettings
        fields = (
            "acknowledgement_email_enabled",
            "acknowledgement_subject",
            "acknowledgement_body",
            "acknowledgement_from_email",
            "sales_in_app_enabled",
            "feishu_enabled",
            "feishu_webhook_url",
            "clear_feishu_webhook",
            "feishu_configured",
            "feishu_webhook_hint",
            "response_sla_seconds",
            "updated_at",
        )
        read_only_fields = (
            "feishu_configured",
            "feishu_webhook_hint",
            "updated_at",
        )

    def get_feishu_configured(self, obj):
        return bool(obj.feishu_webhook_ciphertext)

    def validate_acknowledgement_subject(self, value):
        try:
            return validate_message_template(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_acknowledgement_body(self, value):
        try:
            return validate_message_template(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_feishu_webhook_url(self, value):
        if not value.strip():
            return ""
        try:
            return validate_feishu_webhook_url(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        attrs = super().validate(attrs)
        enabled = attrs.get(
            "feishu_enabled",
            getattr(self.instance, "feishu_enabled", False),
        )
        supplied = bool(attrs.get("feishu_webhook_url", "").strip())
        clearing = attrs.get("clear_feishu_webhook", False)
        configured = bool(
            self.instance and self.instance.feishu_webhook_ciphertext and not clearing
        )
        if enabled and not (supplied or configured):
            raise serializers.ValidationError(
                {"feishu_webhook_url": "Configure a webhook before enabling Feishu."}
            )
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        webhook_url = validated_data.pop("feishu_webhook_url", "").strip()
        clear_webhook = validated_data.pop("clear_feishu_webhook", False)
        instance = super().update(instance, validated_data)
        if webhook_url:
            instance.set_feishu_webhook(webhook_url)
            instance.save(
                update_fields=[
                    "feishu_webhook_ciphertext",
                    "feishu_webhook_hint",
                    "updated_at",
                ]
            )
        elif clear_webhook:
            instance.clear_feishu_webhook()
            instance.feishu_enabled = False
            instance.save(
                update_fields=[
                    "feishu_webhook_ciphertext",
                    "feishu_webhook_hint",
                    "feishu_enabled",
                    "updated_at",
                ]
            )
        return instance


class SDRNurtureStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = SDRNurtureStep
        fields = (
            "id",
            "position",
            "delay_minutes",
            "subject_a",
            "body_a",
            "subject_b",
            "body_b",
            "variant_b_percent",
        )
        read_only_fields = ("id",)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        for field in ("subject_a", "body_a"):
            try:
                validate_message_template(attrs.get(field, ""))
            except ValueError as exc:
                raise serializers.ValidationError({field: str(exc)}) from exc
        percent = attrs.get("variant_b_percent", 0)
        subject_b = attrs.get("subject_b", "").strip()
        body_b = attrs.get("body_b", "").strip()
        if percent and (not subject_b or not body_b):
            raise serializers.ValidationError(
                "Variant B subject and body are required when B traffic is enabled."
            )
        for field, value in (("subject_b", subject_b), ("body_b", body_b)):
            if not value:
                continue
            try:
                validate_message_template(value)
            except ValueError as exc:
                raise serializers.ValidationError({field: str(exc)}) from exc
        return attrs


class SDRNurtureSequenceSerializer(serializers.ModelSerializer):
    steps = SDRNurtureStepSerializer(many=True)
    metrics = serializers.SerializerMethodField()

    class Meta:
        model = SDRNurtureSequence
        fields = (
            "id",
            "name",
            "description",
            "priority",
            "is_active",
            "auto_enroll",
            "sources",
            "qualification_bands",
            "from_email",
            "steps",
            "metrics",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "metrics", "created_at", "updated_at")

    def validate_sources(self, values):
        valid = set(LeadIntakeSource.values)
        if any(value not in valid for value in values):
            raise serializers.ValidationError("Select only supported intake sources.")
        return list(dict.fromkeys(values))

    def validate_qualification_bands(self, values):
        valid = {band.value for band in QualificationBand}
        if any(value not in valid for value in values):
            raise serializers.ValidationError("Select only supported score bands.")
        return list(dict.fromkeys(values))

    def validate_steps(self, steps):
        if not steps:
            raise serializers.ValidationError("Add at least one nurture step.")
        positions = [step["position"] for step in steps]
        if sorted(positions) != list(range(1, len(steps) + 1)):
            raise serializers.ValidationError(
                "Step positions must be consecutive, starting at 1."
            )
        return steps

    def validate(self, attrs):
        attrs = super().validate(attrs)
        active = attrs.get("is_active", getattr(self.instance, "is_active", False))
        automatic = attrs.get(
            "auto_enroll", getattr(self.instance, "auto_enroll", False)
        )
        if automatic and not active:
            raise serializers.ValidationError(
                {"auto_enroll": "Enable the sequence before automatic enrollment."}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        steps = validated_data.pop("steps")
        sequence = SDRNurtureSequence.objects.create(**validated_data)
        self._replace_steps(sequence, steps)
        return sequence

    @transaction.atomic
    def update(self, instance, validated_data):
        steps = validated_data.pop("steps", None)
        instance = super().update(instance, validated_data)
        if steps is not None:
            self._replace_steps(instance, steps)
        return instance

    @staticmethod
    def _replace_steps(sequence, steps):
        sequence.steps.all().delete()
        SDRNurtureStep.objects.bulk_create(
            [
                SDRNurtureStep(
                    org_id=sequence.org_id,
                    sequence=sequence,
                    **step,
                )
                for step in steps
            ]
        )

    def get_metrics(self, obj):
        enrollments = obj.enrollments.all()
        deliveries = LeadNurtureDelivery.objects.filter(
            org_id=obj.org_id,
            enrollment__sequence=obj,
        )
        sent = deliveries.filter(status=NurtureDeliveryStatus.SENT).count()
        delivered = deliveries.filter(delivered_at__isnull=False).count()
        bounced = deliveries.filter(bounced_at__isnull=False).count()
        complained = deliveries.filter(complained_at__isnull=False).count()
        opened = deliveries.filter(opened_at__isnull=False).count()
        clicked = deliveries.filter(clicked_at__isnull=False).count()
        replied = deliveries.filter(replied_at__isnull=False).count()
        positive = deliveries.filter(
            reply_sentiment=NurtureReplySentiment.POSITIVE
        ).count()
        variants = {}
        for variant in ("A", "B"):
            variant_deliveries = deliveries.filter(variant=variant)
            variant_sent = variant_deliveries.filter(
                status=NurtureDeliveryStatus.SENT
            ).count()
            variant_replied = variant_deliveries.filter(
                replied_at__isnull=False
            ).count()
            variant_opened = variant_deliveries.filter(
                opened_at__isnull=False
            ).count()
            variant_clicked = variant_deliveries.filter(
                clicked_at__isnull=False
            ).count()
            variant_bounced = variant_deliveries.filter(
                bounced_at__isnull=False
            ).count()
            variant_complained = variant_deliveries.filter(
                complained_at__isnull=False
            ).count()
            variants[variant] = {
                "sent": variant_sent,
                "opened": variant_opened,
                "clicked": variant_clicked,
                "bounced": variant_bounced,
                "complained": variant_complained,
                "replied": variant_replied,
                "open_rate": (
                    round(variant_opened * 100 / variant_sent, 1)
                    if variant_sent
                    else 0
                ),
                "click_rate": (
                    round(variant_clicked * 100 / variant_sent, 1)
                    if variant_sent
                    else 0
                ),
                "bounce_rate": (
                    round(variant_bounced * 100 / variant_sent, 1)
                    if variant_sent
                    else 0
                ),
                "reply_rate": (
                    round(variant_replied * 100 / variant_sent, 1)
                    if variant_sent
                    else 0
                ),
            }
        return {
            "enrollments": enrollments.count(),
            "active": enrollments.filter(
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
            "open_rate": round(opened * 100 / sent, 1) if sent else 0,
            "click_rate": round(clicked * 100 / sent, 1) if sent else 0,
            "delivery_rate": round(delivered * 100 / sent, 1) if sent else 0,
            "bounce_rate": round(bounced * 100 / sent, 1) if sent else 0,
            "complaint_rate": round(complained * 100 / sent, 1) if sent else 0,
            "reply_rate": round(replied * 100 / sent, 1) if sent else 0,
            "positive_reply_rate": round(positive * 100 / sent, 1) if sent else 0,
            "variants": variants,
        }


class LeadNurtureDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadNurtureDelivery
        fields = (
            "id",
            "step_position",
            "variant",
            "recipient",
            "status",
            "scheduled_for",
            "attempt_count",
            "last_error_code",
            "last_error_message",
            "sent_at",
            "provider_message_id",
            "delivered_at",
            "bounced_at",
            "complained_at",
            "bounce_type",
            "bounce_subtype",
            "opened_at",
            "clicked_at",
            "open_count",
            "click_count",
            "last_clicked_url",
            "replied_at",
            "reply_message_id",
            "reply_sentiment",
        )
        read_only_fields = fields


class SDREmailSuppressionSerializer(serializers.ModelSerializer):
    source_delivery_id = serializers.UUIDField(
        source="source_delivery.id",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = SDREmailSuppression
        fields = (
            "id",
            "email",
            "reason",
            "source",
            "is_active",
            "suppressed_at",
            "released_at",
            "source_delivery_id",
            "details",
        )
        read_only_fields = fields


class SDREmailSuppressionCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    reason = serializers.ChoiceField(
        choices=EmailSuppressionReason.choices,
        default=EmailSuppressionReason.ADMIN,
    )


class LeadNurtureEnrollmentSerializer(serializers.ModelSerializer):
    sequence_name = serializers.CharField(source="sequence.name", read_only=True)
    intake_id = serializers.UUIDField(source="intake.id", read_only=True)
    lead_id = serializers.UUIDField(source="lead.id", read_only=True, allow_null=True)
    company_name = serializers.SerializerMethodField()
    contact_name = serializers.SerializerMethodField()
    contact_email = serializers.SerializerMethodField()
    deliveries = LeadNurtureDeliverySerializer(many=True, read_only=True)

    class Meta:
        model = LeadNurtureEnrollment
        fields = (
            "id",
            "sequence_id",
            "sequence_name",
            "intake_id",
            "lead_id",
            "company_name",
            "contact_name",
            "contact_email",
            "status",
            "current_step_position",
            "next_run_at",
            "enrolled_at",
            "completed_at",
            "stop_reason",
            "deliveries",
        )
        read_only_fields = fields

    def get_company_name(self, obj):
        lead = obj.lead or obj.intake.crm_lead
        return getattr(lead, "company_name", "") or obj.intake.normalized_payload.get(
            "company", {}
        ).get("name", "")

    def get_contact_name(self, obj):
        lead = obj.lead or obj.intake.crm_lead
        if lead:
            return " ".join(filter(None, (lead.first_name, lead.last_name)))
        identity = obj.intake.normalized_payload.get("identity", {})
        return " ".join(
            filter(None, (identity.get("first_name"), identity.get("last_name")))
        )

    def get_contact_email(self, obj):
        lead = obj.lead or obj.intake.crm_lead
        if lead and lead.email:
            return lead.email
        return obj.intake.normalized_payload.get("identity", {}).get("email", "")


class LeadNurtureEnrollmentActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=(
            "pause",
            "resume",
            "cancel",
            "mark_replied",
            "mark_converted",
        )
    )
    reply_sentiment = serializers.ChoiceField(
        choices=NurtureReplySentiment.choices,
        required=False,
        default=NurtureReplySentiment.NEUTRAL,
    )


class LeadLifecycleEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadLifecycleEvent
        fields = ("id", "event_type", "event_key", "data", "occurred_at")
        read_only_fields = fields


class LeadDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadDelivery
        fields = (
            "id",
            "kind",
            "recipient",
            "status",
            "attempt_count",
            "last_error_code",
            "last_error_message",
            "sent_at",
            "created_at",
        )
        read_only_fields = fields


class LeadIntakeOperationsSerializer(serializers.ModelSerializer):
    lead_id = serializers.UUIDField(source="crm_lead_id", read_only=True)
    assigned_profile_id = serializers.UUIDField(read_only=True, allow_null=True)
    routing_rule_id = serializers.UUIDField(read_only=True, allow_null=True)
    assigned_sales = serializers.SerializerMethodField()
    company_name = serializers.SerializerMethodField()
    contact_email = serializers.SerializerMethodField()
    deliveries = LeadDeliverySerializer(many=True, read_only=True)
    lifecycle_events = LeadLifecycleEventSerializer(many=True, read_only=True)
    response_seconds = serializers.SerializerMethodField()
    sla_breached = serializers.SerializerMethodField()

    class Meta:
        model = LeadIntake
        fields = (
            "id",
            "source",
            "source_record_id",
            "status",
            "attempt_count",
            "lead_id",
            "company_name",
            "contact_email",
            "assigned_profile_id",
            "assigned_sales",
            "qualification_score",
            "qualification_band",
            "routing_rule_id",
            "routing_reason",
            "error_message",
            "response_seconds",
            "sla_breached",
            "deliveries",
            "lifecycle_events",
            "processed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_assigned_sales(self, obj):
        profile = obj.assigned_profile
        if profile is None:
            return None
        return {
            "id": str(profile.id),
            "name": profile.user.name,
            "email": profile.user.email,
        }

    def get_company_name(self, obj):
        if obj.crm_lead:
            return obj.crm_lead.company_name or obj.crm_lead.title or ""
        return obj.normalized_payload.get("company", {}).get("name", "")

    def get_contact_email(self, obj):
        if obj.crm_lead and obj.crm_lead.email:
            return obj.crm_lead.email
        return (
            obj.normalized_payload.get("identity", {}).get("email")
            or obj.raw_payload.get("email")
            or ""
        )

    def get_response_seconds(self, obj):
        delivery = self._ack_delivery(obj)
        if not delivery or not delivery.sent_at:
            return None
        return max(0, int((delivery.sent_at - obj.created_at).total_seconds()))

    def get_sla_breached(self, obj):
        configuration = self.context.get("response_settings")
        if not configuration or not configuration.acknowledgement_email_enabled:
            return False
        if not self.get_contact_email(obj):
            return False
        response_seconds = self.get_response_seconds(obj)
        if response_seconds is not None:
            return response_seconds > configuration.response_sla_seconds
        return (
            timezone.now() - obj.created_at
        ).total_seconds() > configuration.response_sla_seconds

    @staticmethod
    def _ack_delivery(obj):
        return next(
            (
                delivery
                for delivery in obj.deliveries.all()
                if delivery.kind == LeadDeliveryKind.ACKNOWLEDGEMENT_EMAIL
                and delivery.status == LeadDeliveryStatus.SENT
            ),
            None,
        )
