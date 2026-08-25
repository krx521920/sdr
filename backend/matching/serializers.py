"""API contracts for the unified matching bounded context."""

import re

from django.utils import timezone
from rest_framework import serializers

from common.models import Profile
from matching.models import (
    Evidence,
    Match,
    MatchDecisionEvent,
    MatchEvidence,
    MatchOpportunity,
    MatchOpportunityStatus,
    MatchRevision,
    MatchRun,
    MatchStatus,
    Person,
    PersonIdentity,
    PersonIdentityKind,
)
from matching.services import (
    MAX_ASYNC_RECOMPUTE_PEOPLE,
    MAX_SYNC_RECOMPUTE_PEOPLE,
    SUPPORTED_DIMENSIONS,
)


def _validate_string_list(value, field_name):
    if not isinstance(value, list):
        raise serializers.ValidationError(f"{field_name} must be a list of strings.")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise serializers.ValidationError(
                f"{field_name} must contain only non-empty strings."
            )
        cleaned.append(item.strip())
    return list(dict.fromkeys(cleaned))


def validate_criteria(value):
    if not isinstance(value, dict):
        raise serializers.ValidationError("Criteria must be an object.")
    unknown = set(value) - set(SUPPORTED_DIMENSIONS)
    if unknown:
        raise serializers.ValidationError(
            f"Unsupported criteria: {', '.join(sorted(unknown))}."
        )
    return {key: _validate_string_list(items, key) for key, items in value.items()}


class OrgRelatedSerializerMixin:
    def org(self):
        return self.context["org"]


class PersonIdentitySerializer(OrgRelatedSerializerMixin, serializers.ModelSerializer):
    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.none())

    class Meta:
        model = PersonIdentity
        fields = (
            "id",
            "person",
            "kind",
            "normalized_value",
            "display_value",
            "source",
            "is_primary",
            "verified_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        org = self.context.get("org")
        if org:
            self.fields["person"].queryset = Person.objects.filter(org=org)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        person = attrs.get("person", getattr(self.instance, "person", None))
        kind = attrs.get("kind", getattr(self.instance, "kind", ""))
        value = attrs.get(
            "normalized_value",
            getattr(self.instance, "normalized_value", ""),
        ).strip()
        if person and person.org_id != self.org().id:
            raise serializers.ValidationError("Person belongs to another org.")
        if kind == PersonIdentityKind.EMAIL:
            value = value.lower()
            if "@" not in value:
                raise serializers.ValidationError(
                    {"normalized_value": "Enter a valid email address."}
                )
        elif kind in {PersonIdentityKind.PHONE, PersonIdentityKind.WHATSAPP}:
            value = re.sub(r"[\s()\-]", "", value)
            if not re.fullmatch(r"\+?[0-9]{6,20}", value):
                raise serializers.ValidationError(
                    {"normalized_value": "Enter a valid phone number."}
                )
        else:
            value = value.lower()
        attrs["normalized_value"] = value
        duplicate = PersonIdentity.objects.filter(
            org=self.org(),
            kind=kind,
            normalized_value=value,
        )
        if self.instance:
            duplicate = duplicate.exclude(id=self.instance.id)
        if duplicate.exists():
            raise serializers.ValidationError(
                {"normalized_value": "This identity already exists in the org."}
            )
        return attrs


class PersonSerializer(serializers.ModelSerializer):
    identities = PersonIdentitySerializer(many=True, read_only=True)
    evidence_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Person
        fields = (
            "id",
            "display_name",
            "first_name",
            "last_name",
            "headline",
            "summary",
            "current_title",
            "current_company",
            "location",
            "timezone",
            "skills",
            "roles",
            "attributes",
            "availability",
            "status",
            "identities",
            "evidence_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "evidence_count", "created_at", "updated_at")

    def validate_display_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Display name is required.")
        return value

    def validate_skills(self, value):
        return _validate_string_list(value, "skills")

    def validate_roles(self, value):
        return _validate_string_list(value, "roles")

    def validate_attributes(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("attributes must be an object.")
        return value


class EvidenceSerializer(OrgRelatedSerializerMixin, serializers.ModelSerializer):
    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.none())

    class Meta:
        model = Evidence
        fields = (
            "id",
            "person",
            "kind",
            "source",
            "summary",
            "facts",
            "source_uri",
            "source_record_id",
            "observed_at",
            "valid_until",
            "confidence",
            "content_hash",
            "created_at",
        )
        read_only_fields = ("id", "content_hash", "created_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        org = self.context.get("org")
        if org:
            self.fields["person"].queryset = Person.objects.filter(org=org)

    def validate_facts(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("facts must be an object.")
        for key in set(value).intersection(SUPPORTED_DIMENSIONS):
            value[key] = _validate_string_list(value[key], f"facts.{key}")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        person = attrs.get("person", getattr(self.instance, "person", None))
        if person and person.org_id != self.org().id:
            raise serializers.ValidationError("Person belongs to another org.")
        observed_at = attrs.get(
            "observed_at", getattr(self.instance, "observed_at", timezone.now())
        )
        valid_until = attrs.get(
            "valid_until", getattr(self.instance, "valid_until", None)
        )
        if valid_until and valid_until < observed_at:
            raise serializers.ValidationError(
                {"valid_until": "Cannot precede observed_at."}
            )
        source = attrs.get("source", getattr(self.instance, "source", ""))
        source_record_id = attrs.get(
            "source_record_id",
            getattr(self.instance, "source_record_id", ""),
        )
        if person and source_record_id:
            duplicate = Evidence.objects.filter(
                org=self.org(),
                person=person,
                source=source,
                source_record_id=source_record_id,
            )
            if self.instance:
                duplicate = duplicate.exclude(id=self.instance.id)
            if duplicate.exists():
                raise serializers.ValidationError(
                    {"source_record_id": "This source record already exists."}
                )
        return attrs


class MatchOpportunitySerializer(
    OrgRelatedSerializerMixin, serializers.ModelSerializer
):
    owner = serializers.PrimaryKeyRelatedField(
        queryset=Profile.objects.none(),
        allow_null=True,
        required=False,
    )
    match_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = MatchOpportunity
        fields = (
            "id",
            "opportunity_type",
            "status",
            "title",
            "description",
            "organization_name",
            "location",
            "remote_mode",
            "required_criteria",
            "preferred_criteria",
            "exclusion_criteria",
            "scoring_weights",
            "owner",
            "opened_at",
            "closes_at",
            "match_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "match_count", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        org = self.context.get("org")
        if org:
            self.fields["owner"].queryset = Profile.objects.filter(
                org=org,
                is_active=True,
            )

    def validate_required_criteria(self, value):
        return validate_criteria(value)

    def validate_preferred_criteria(self, value):
        return validate_criteria(value)

    def validate_exclusion_criteria(self, value):
        return validate_criteria(value)

    def validate_scoring_weights(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("scoring_weights must be an object.")
        unknown = set(value) - set(SUPPORTED_DIMENSIONS)
        if unknown:
            raise serializers.ValidationError(
                f"Unsupported weights: {', '.join(sorted(unknown))}."
            )
        cleaned = {}
        for key, raw_weight in value.items():
            if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
                raise serializers.ValidationError(f"Weight {key} must be numeric.")
            if raw_weight < 0 or raw_weight > 100:
                raise serializers.ValidationError(
                    f"Weight {key} must be between 0 and 100."
                )
            cleaned[key] = raw_weight
        if cleaned and not any(cleaned.values()):
            raise serializers.ValidationError("At least one weight must be positive.")
        return cleaned

    def validate(self, attrs):
        attrs = super().validate(attrs)
        owner = attrs.get("owner", getattr(self.instance, "owner", None))
        if owner and owner.org_id != self.org().id:
            raise serializers.ValidationError("Owner belongs to another org.")
        opened_at = attrs.get("opened_at", getattr(self.instance, "opened_at", None))
        closes_at = attrs.get("closes_at", getattr(self.instance, "closes_at", None))
        if closes_at and opened_at and closes_at < opened_at:
            raise serializers.ValidationError(
                {"closes_at": "Cannot precede opened_at."}
            )
        if attrs.get("status") == MatchOpportunityStatus.OPEN and not opened_at:
            attrs["opened_at"] = timezone.now()
        return attrs


class MatchPersonSummarySerializer(serializers.ModelSerializer):
    """Minimal person projection safe to embed in ranked match responses."""

    class Meta:
        model = Person
        fields = (
            "id",
            "display_name",
            "current_title",
            "current_company",
            "location",
            "availability",
        )
        read_only_fields = fields


class MatchEvidenceSummarySerializer(serializers.ModelSerializer):
    """Citation metadata without raw facts or provider record locators."""

    class Meta:
        model = Evidence
        fields = (
            "id",
            "kind",
            "source",
            "summary",
            "observed_at",
            "valid_until",
            "confidence",
            "content_hash",
        )
        read_only_fields = fields


class MatchEvidenceSerializer(serializers.ModelSerializer):
    evidence = MatchEvidenceSummarySerializer(read_only=True)

    class Meta:
        model = MatchEvidence
        fields = (
            "id",
            "evidence",
            "direction",
            "relevance",
            "contribution",
            "explanation",
        )


class MatchSerializer(serializers.ModelSerializer):
    person_name = serializers.CharField(source="person.display_name", read_only=True)
    person_summary = MatchPersonSummarySerializer(source="person", read_only=True)
    opportunity_title = serializers.CharField(
        source="opportunity.title", read_only=True
    )
    evidence_links = MatchEvidenceSerializer(many=True, read_only=True)

    class Meta:
        model = Match
        fields = (
            "id",
            "person",
            "person_name",
            "person_summary",
            "opportunity",
            "opportunity_title",
            "status",
            "overall_score",
            "eligibility_score",
            "fit_score",
            "trust_score",
            "relationship_score",
            "availability_score",
            "confidence",
            "rank",
            "reasons",
            "gaps",
            "score_breakdown",
            "engine_version",
            "model_provider",
            "model_name",
            "evaluated_at",
            "ranking_revision",
            "decision_revision",
            "decision_reason",
            "decided_at",
            "evidence_links",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class MatchStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=MatchStatus.choices)
    expected_revision = serializers.IntegerField(min_value=0)
    expected_ranking_revision = serializers.IntegerField(min_value=0)
    reason_code = serializers.CharField(max_length=64, trim_whitespace=True)
    reason = serializers.CharField(
        max_length=1000,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default="",
    )
    idempotency_key = serializers.CharField(
        max_length=128,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )


class MatchRevisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchRevision
        fields = (
            "id",
            "match",
            "run",
            "revision",
            "revision_kind",
            "snapshot",
            "evidence_snapshot",
            "engine_version",
            "evaluated_at",
            "created_at",
        )
        read_only_fields = fields


class MatchDecisionEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchDecisionEvent
        fields = (
            "id",
            "match",
            "from_status",
            "to_status",
            "reason_code",
            "reason",
            "expected_decision_revision",
            "resulting_decision_revision",
            "based_on_ranking_revision",
            "created_at",
        )
        read_only_fields = fields


class RecomputeMatchesSerializer(OrgRelatedSerializerMixin, serializers.Serializer):
    person_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=False,
        max_length=MAX_SYNC_RECOMPUTE_PEOPLE,
    )

    def validate_person_ids(self, values):
        values = list(dict.fromkeys(values))
        found = set(
            Person.objects.filter(org=self.org(), id__in=values).values_list(
                "id", flat=True
            )
        )
        missing = [str(value) for value in values if value not in found]
        if missing:
            raise serializers.ValidationError(
                f"Unknown people for this org: {', '.join(missing)}"
            )
        return values


class AsyncRecomputeMatchesSerializer(
    OrgRelatedSerializerMixin, serializers.Serializer
):
    person_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=False,
        max_length=MAX_ASYNC_RECOMPUTE_PEOPLE,
    )

    def validate_person_ids(self, values):
        values = list(dict.fromkeys(values))
        found = set(
            Person.objects.filter(
                org=self.org(),
                status="active",
                id__in=values,
            ).values_list("id", flat=True)
        )
        missing = [str(value) for value in values if value not in found]
        if missing:
            raise serializers.ValidationError(
                "Every requested person must be active and belong to this org."
            )
        return values


class MatchRunSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(source="automation_job.id", read_only=True)
    status = serializers.CharField(source="automation_job.status", read_only=True)
    error_code = serializers.CharField(
        source="automation_job.last_error_code",
        read_only=True,
    )
    status_url = serializers.SerializerMethodField()

    @staticmethod
    def get_status_url(obj):
        return f"/api/matching/match-runs/{obj.id}/"

    class Meta:
        model = MatchRun
        fields = (
            "id",
            "opportunity",
            "job_id",
            "status",
            "error_code",
            "status_url",
            "total_count",
            "processed_count",
            "result_count",
            "ranking_revision",
            "engine_version",
            "started_at",
            "completed_at",
            "outcome",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields
