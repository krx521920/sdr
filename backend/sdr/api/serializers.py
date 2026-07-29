from django.db import transaction
from rest_framework import serializers

from common.models import Profile
from common.utils import COUNTRIES
from sdr.domain import LeadSource, QualificationBand
from sdr.models import (
    LeadIntakeSource,
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
