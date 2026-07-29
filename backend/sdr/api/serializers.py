from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from common.models import Profile
from common.utils import COUNTRIES
from sdr.domain import LeadSource, QualificationBand
from sdr.intelligence.registry import provider_catalog, provider_registry
from sdr.models import (
    LeadInspection,
    LeadIntakeSource,
    SDRIntelligenceSettings,
    SDRModelCredential,
    SDRModelProvider,
    SDRRoutingRule,
    SDRRoutingRuleMember,
)
from sdr.routing.service import normalize_country

VALID_COUNTRY_CODES = {code for code, _ in COUNTRIES}


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
