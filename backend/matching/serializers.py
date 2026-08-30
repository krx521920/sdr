"""API contracts for the unified matching bounded context."""

import json
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit

from django.utils import timezone
from rest_framework import serializers

from common.models import Profile
from matching.models import (
    Evidence,
    EvidenceCollectionMethod,
    EvidenceKind,
    EvidenceLawfulBasis,
    EvidenceProcessingStatus,
    EvidenceSource,
    GovernanceContactChannel,
    Match,
    MatchDecisionEvent,
    MatchEvidence,
    MatchFeedbackAction,
    MatchFeedbackAssessment,
    MatchFeedbackAttribution,
    MatchFeedbackDimension,
    MatchFeedbackEvent,
    MatchFeedbackSource,
    MatchOpportunity,
    MatchOpportunityStatus,
    MatchOpportunityType,
    MatchOutcomeCode,
    MatchRecommendationVerdict,
    MatchRevision,
    MatchRun,
    MatchScoringPolicy,
    MatchScoringPolicyVersion,
    MatchStatus,
    MatchWeightSuggestion,
    MatchWeightSuggestionReviewAction,
    Person,
    PersonAvailability,
    PersonContactIntentPurpose,
    PersonContactIntentState,
    PersonIdentity,
    PersonIdentityKind,
    PersonImportBatch,
    PersonImportConflict,
    PersonImportDecision,
    PersonImportDecisionAction,
    PersonImportRecord,
)
from matching.services import (
    MAX_ASYNC_RECOMPUTE_PEOPLE,
    MAX_SYNC_RECOMPUTE_PEOPLE,
    SUPPORTED_DIMENSIONS,
)


class MatchingCapabilitiesSerializer(serializers.Serializer):
    read = serializers.BooleanField(read_only=True)
    manage = serializers.BooleanField(read_only=True)
    recompute = serializers.BooleanField(read_only=True)
    decide = serializers.BooleanField(read_only=True)
    feedback = serializers.BooleanField(read_only=True)
    calibrate = serializers.BooleanField(read_only=True)
    export = serializers.BooleanField(read_only=True)
    delete = serializers.BooleanField(read_only=True)
    retention = serializers.BooleanField(read_only=True)


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


class StrictSerializer(serializers.Serializer):
    """Fail closed when clients send fields outside the published contract."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {name: ["Unknown field."] for name in unknown}
                )
        return super().to_internal_value(data)


RAW_CONTENT_KEYS = {
    "raw_content",
    "message_body",
    "body_text",
    "body_html",
    "transcript",
    "chat_text",
    "conversation",
    "provider_payload",
}


def _reject_nested_raw_content(value, *, path="payload", depth=0):
    if depth > 20:
        raise serializers.ValidationError(f"{path} is nested too deeply.")
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in RAW_CONTENT_KEYS:
                raise serializers.ValidationError(
                    f"{path}.{key} cannot contain raw messages or provider payloads."
                )
            _reject_nested_raw_content(
                child,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_nested_raw_content(
                child,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )


def _validate_bounded_json_object(value, field_name, *, max_bytes, max_keys=100):
    if not isinstance(value, dict):
        raise serializers.ValidationError(f"{field_name} must be an object.")
    if len(value) > max_keys:
        raise serializers.ValidationError(
            f"{field_name} cannot contain more than {max_keys} keys."
        )
    _reject_nested_raw_content(value, path=field_name)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError(
            f"{field_name} must contain valid JSON values."
        ) from exc
    if len(encoded) > max_bytes:
        raise serializers.ValidationError(
            f"{field_name} cannot exceed {max_bytes} UTF-8 bytes."
        )
    return value


class PersonOnboardingPersonSerializer(StrictSerializer):
    display_name = serializers.CharField(max_length=255, trim_whitespace=True)
    first_name = serializers.CharField(
        max_length=120,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    last_name = serializers.CharField(
        max_length=120,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    headline = serializers.CharField(
        max_length=500,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    summary = serializers.CharField(
        max_length=5000,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    current_title = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    current_company = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    location = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    timezone = serializers.CharField(
        max_length=64,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    skills = serializers.ListField(
        child=serializers.CharField(max_length=120, trim_whitespace=True),
        required=False,
        allow_empty=True,
        max_length=50,
    )
    roles = serializers.ListField(
        child=serializers.CharField(max_length=120, trim_whitespace=True),
        required=False,
        allow_empty=True,
        max_length=50,
    )
    availability = serializers.ChoiceField(
        choices=PersonAvailability.choices,
        required=False,
    )

    def validate_display_name(self, value):
        if not value:
            raise serializers.ValidationError("Display name is required.")
        return value

    def validate_skills(self, value):
        return _validate_string_list(value, "skills")

    def validate_roles(self, value):
        return _validate_string_list(value, "roles")


class PersonOnboardingIdentitySerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=PersonIdentityKind.choices)
    normalized_value = serializers.CharField(max_length=500, trim_whitespace=True)
    display_value = serializers.CharField(
        max_length=500,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    source = serializers.ChoiceField(
        choices=((EvidenceSource.MANUAL, "Manual"),),
        required=False,
        default=EvidenceSource.MANUAL,
    )
    is_primary = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        kind = attrs["kind"]
        value = attrs["normalized_value"]
        if kind == PersonIdentityKind.EMAIL:
            value = serializers.EmailField(max_length=254).run_validation(value).lower()
        elif kind in {PersonIdentityKind.PHONE, PersonIdentityKind.WHATSAPP}:
            value = re.sub(r"[\s()\-]", "", value)
            if not re.fullmatch(r"\+?[0-9]{6,20}", value):
                raise serializers.ValidationError(
                    {"normalized_value": "Enter a valid phone number."}
                )
        else:
            value = value.lower()
        attrs["normalized_value"] = value
        attrs["source"] = EvidenceSource.MANUAL
        return attrs


SENSITIVE_SOURCE_URI_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "code",
    "credential",
    "jwt",
    "password",
    "passwd",
    "secret",
    "session",
    "signature",
    "sig",
    "token",
}


def _validate_supported_facts(value):
    value = _validate_bounded_json_object(
        value,
        "facts",
        max_bytes=32 * 1024,
    )
    unknown = set(value) - set(SUPPORTED_DIMENSIONS)
    if unknown:
        raise serializers.ValidationError(
            f"Unsupported facts: {', '.join(sorted(unknown))}."
        )
    for key in value:
        dimension_values = value[key]
        if isinstance(dimension_values, list) and len(dimension_values) > 100:
            raise serializers.ValidationError(
                f"facts.{key} cannot contain more than 100 values."
            )
        cleaned = _validate_string_list(dimension_values, f"facts.{key}")
        if any(len(item) > 120 for item in cleaned):
            raise serializers.ValidationError(
                f"facts.{key} values cannot exceed 120 characters."
            )
        value[key] = cleaned
    return value


def _validate_safe_source_uri(value):
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise serializers.ValidationError("Only HTTP(S) source URIs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise serializers.ValidationError(
            "Source URI must not contain embedded credentials."
        )
    if parsed.fragment:
        raise serializers.ValidationError("Source URI must not contain a fragment.")
    for raw_key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        key = raw_key.casefold().replace("-", "_")
        if key in SENSITIVE_SOURCE_URI_QUERY_KEYS or any(
            key.endswith(f"_{suffix}") for suffix in SENSITIVE_SOURCE_URI_QUERY_KEYS
        ):
            raise serializers.ValidationError(
                "Source URI must not contain sensitive parameters."
            )
    return value


class PersonOnboardingEvidenceSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=EvidenceKind.choices)
    source = serializers.ChoiceField(
        choices=((EvidenceSource.MANUAL, "Manual"),),
        required=False,
        default=EvidenceSource.MANUAL,
    )
    summary = serializers.CharField(max_length=5000, trim_whitespace=True)
    facts = serializers.JSONField(required=False, default=dict)
    source_uri = serializers.URLField(
        max_length=1000,
        required=False,
        allow_blank=True,
    )
    source_record_id = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )
    observed_at = serializers.DateTimeField(required=False)
    valid_until = serializers.DateTimeField(required=False, allow_null=True)
    confidence = serializers.DecimalField(
        max_digits=4,
        decimal_places=3,
        min_value=0,
        max_value=1,
        required=False,
    )

    def validate_facts(self, value):
        return _validate_supported_facts(value)

    def validate_source_uri(self, value):
        return _validate_safe_source_uri(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        observed_at = attrs.get("observed_at")
        valid_until = attrs.get("valid_until")
        if valid_until and observed_at is None:
            raise serializers.ValidationError(
                {"observed_at": "Required when valid_until is provided."}
            )
        if valid_until and valid_until < observed_at:
            raise serializers.ValidationError(
                {"valid_until": "Cannot precede observed_at."}
            )
        attrs["source"] = EvidenceSource.MANUAL
        return attrs


class PersonOnboardingRequestSerializer(StrictSerializer):
    person = PersonOnboardingPersonSerializer()
    identities = PersonOnboardingIdentitySerializer(
        many=True,
        required=False,
        allow_empty=True,
        max_length=20,
        default=list,
    )
    evidence = PersonOnboardingEvidenceSerializer(
        many=True,
        allow_empty=False,
        min_length=1,
        max_length=50,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        seen_identities = set()
        primary_kinds = set()
        for index, identity in enumerate(attrs.get("identities", [])):
            identity_key = (identity["kind"], identity["normalized_value"])
            if identity_key in seen_identities:
                raise serializers.ValidationError(
                    {"identities": {index: "Duplicate identity in request."}}
                )
            seen_identities.add(identity_key)
            if identity.get("is_primary"):
                if identity["kind"] in primary_kinds:
                    raise serializers.ValidationError(
                        {
                            "identities": {
                                index: "Only one primary identity is allowed per kind."
                            }
                        }
                    )
                primary_kinds.add(identity["kind"])

        seen_source_records = set()
        for index, evidence in enumerate(attrs["evidence"]):
            source_record_id = evidence.get("source_record_id", "")
            if source_record_id and source_record_id in seen_source_records:
                raise serializers.ValidationError(
                    {"evidence": {index: "Duplicate source_record_id in request."}}
                )
            if source_record_id:
                seen_source_records.add(source_record_id)

        encoded = json.dumps(
            attrs,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise serializers.ValidationError(
                "Onboarding request cannot exceed 262144 normalized UTF-8 bytes."
            )
        return attrs


class PersonOnboardingResponseSerializer(serializers.Serializer):
    person_id = serializers.UUIDField(read_only=True)
    identity_ids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
    )
    evidence_ids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
    )
    replayed = serializers.BooleanField(read_only=True)


class PersonIdentitySerializer(OrgRelatedSerializerMixin, serializers.ModelSerializer):
    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.none())
    source = serializers.ChoiceField(
        choices=((EvidenceSource.MANUAL, "Manual"),),
        required=False,
        default=EvidenceSource.MANUAL,
    )

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


class PersonIdentitySafeSerializer(serializers.ModelSerializer):
    person = serializers.UUIDField(source="person_id", read_only=True)
    masked_value = serializers.SerializerMethodField()

    class Meta:
        model = PersonIdentity
        fields = (
            "id",
            "person",
            "kind",
            "masked_value",
            "source",
            "is_primary",
            "verified_at",
            "created_at",
            "updated_at",
        )

    @staticmethod
    def get_masked_value(identity):
        value = identity.normalized_value
        if identity.kind == PersonIdentityKind.EMAIL and "@" in value:
            local, domain = value.split("@", 1)
            return f"{local[:1]}***@{domain}"
        return f"***{value[-4:]}" if value else "***"


class PersonSerializer(serializers.ModelSerializer):
    identities = PersonIdentitySafeSerializer(many=True, read_only=True)
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
        return self._validate_profile_terms(value, "skills")

    def validate_roles(self, value):
        return self._validate_profile_terms(value, "roles")

    @staticmethod
    def _validate_profile_terms(value, field_name):
        cleaned = _validate_string_list(value, field_name)
        if len(cleaned) > 50:
            raise serializers.ValidationError(
                f"{field_name} cannot contain more than 50 values."
            )
        if any(len(item) > 120 for item in cleaned):
            raise serializers.ValidationError(
                f"{field_name} values cannot exceed 120 characters."
            )
        return cleaned

    def validate_attributes(self, value):
        return _validate_bounded_json_object(
            value,
            "attributes",
            max_bytes=16 * 1024,
        )


class EvidenceSerializer(OrgRelatedSerializerMixin, serializers.ModelSerializer):
    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.none())
    summary = serializers.CharField(max_length=5000, trim_whitespace=True)
    source = serializers.ChoiceField(
        choices=((EvidenceSource.MANUAL, "Manual"),),
        required=False,
        default=EvidenceSource.MANUAL,
    )

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

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {name: ["Unknown field."] for name in unknown}
                )
        return super().to_internal_value(data)

    def validate_facts(self, value):
        return _validate_supported_facts(value)

    def validate_source_uri(self, value):
        return _validate_safe_source_uri(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        person = attrs.get("person", getattr(self.instance, "person", None))
        if person and person.org_id != self.org().id:
            raise serializers.ValidationError("Person belongs to another org.")
        observed_at = attrs.get(
            "observed_at", getattr(self.instance, "observed_at", None)
        )
        if observed_at is None:
            observed_at = timezone.now()
            attrs["observed_at"] = observed_at
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


class EvidenceSafeSerializer(serializers.ModelSerializer):
    """Direct API response that omits raw facts and provider references."""

    class Meta:
        model = Evidence
        fields = (
            "id",
            "person",
            "kind",
            "source",
            "summary",
            "observed_at",
            "valid_until",
            "confidence",
            "content_hash",
            "created_at",
        )
        read_only_fields = fields


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
            "scoring_policy_version",
            "owner",
            "opened_at",
            "closes_at",
            "match_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "scoring_policy_version",
            "match_count",
            "created_at",
            "updated_at",
        )

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
            "projection_state",
            "retired_at",
            "retirement_reason",
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
            "feedback_revision",
            "recommendation_verdict",
            "latest_outcome_code",
            "latest_outcome_at",
            "scoring_policy_version",
            "scoring_policy_checksum",
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
            "scoring_policy_version",
            "scoring_policy_checksum",
            "dimension_weights",
            "component_weights",
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


class MatchFeedbackAttributionRequestSerializer(StrictSerializer):
    evidence_id = serializers.UUIDField(required=False, allow_null=True)
    dimension = serializers.ChoiceField(choices=MatchFeedbackDimension.choices)
    assessment = serializers.ChoiceField(choices=MatchFeedbackAssessment.choices)
    reason_code = serializers.RegexField(
        regex=r"^[a-z0-9][a-z0-9_.-]{0,63}$",
        required=False,
        allow_blank=True,
        default="",
    )


class MatchFeedbackMutationSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    expected_ranking_revision = serializers.IntegerField(min_value=0)
    action = serializers.ChoiceField(
        choices=MatchFeedbackAction.choices,
        required=False,
        default=MatchFeedbackAction.RECORD,
    )
    verdict = serializers.ChoiceField(
        choices=MatchRecommendationVerdict.choices,
        required=False,
    )
    outcome_code = serializers.ChoiceField(
        choices=MatchOutcomeCode.choices,
        required=False,
    )
    reason_code = serializers.RegexField(
        regex=r"^[a-z0-9][a-z0-9_.-]{0,63}$",
    )
    note = serializers.CharField(max_length=1000, allow_blank=True, required=False)
    occurred_at = serializers.DateTimeField(required=False, default=timezone.now)
    source = serializers.ChoiceField(
        choices=MatchFeedbackSource.choices,
        required=False,
        default=MatchFeedbackSource.MANUAL,
    )
    supersedes_id = serializers.UUIDField(required=False, allow_null=True)
    attributions = MatchFeedbackAttributionRequestSerializer(
        many=True,
        required=False,
        max_length=20,
    )


class MatchFeedbackAttributionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchFeedbackAttribution
        fields = ("id", "evidence", "dimension", "assessment", "reason_code")
        read_only_fields = fields


class MatchFeedbackEventSerializer(serializers.ModelSerializer):
    attributions = MatchFeedbackAttributionSerializer(many=True, read_only=True)

    class Meta:
        model = MatchFeedbackEvent
        fields = (
            "id",
            "match",
            "match_revision",
            "event_kind",
            "action",
            "verdict",
            "outcome_code",
            "reason_code",
            "occurred_at",
            "recorded_at",
            "source",
            "expected_feedback_revision",
            "resulting_feedback_revision",
            "based_on_ranking_revision",
            "supersedes",
            "attributions",
        )
        read_only_fields = fields


class ScoringWeightsSerializer(StrictSerializer):
    skills = serializers.FloatField(min_value=0, max_value=100)
    titles = serializers.FloatField(min_value=0, max_value=100)
    locations = serializers.FloatField(min_value=0, max_value=100)
    availability = serializers.FloatField(min_value=0, max_value=100)

    def validate(self, attrs):
        if not any(attrs.values()):
            raise serializers.ValidationError("At least one weight must be positive.")
        return attrs


class ComponentWeightsSerializer(StrictSerializer):
    fit = serializers.FloatField(min_value=0, max_value=100)
    eligibility = serializers.FloatField(min_value=0, max_value=100)
    trust = serializers.FloatField(min_value=0, max_value=100)
    relationship = serializers.FloatField(min_value=0, max_value=100)
    availability = serializers.FloatField(min_value=0, max_value=100)

    def validate(self, attrs):
        if not any(attrs.values()):
            raise serializers.ValidationError("At least one weight must be positive.")
        return attrs


class MatchScoringPolicyDraftSerializer(StrictSerializer):
    opportunity_type = serializers.ChoiceField(choices=MatchOpportunityType.choices)
    dimension_weights = ScoringWeightsSerializer()
    component_weights = ComponentWeightsSerializer()
    expected_revision = serializers.IntegerField(min_value=0)
    rationale = serializers.CharField(max_length=1000, allow_blank=True, required=False)


class MatchScoringPolicyMutationSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    action = serializers.ChoiceField(choices=("publish", "reject"), default="publish")
    reason_code = serializers.RegexField(
        regex=r"^[a-z0-9][a-z0-9_.-]{0,63}$",
        required=False,
        allow_blank=True,
        default="",
    )


class MatchScoringPolicyVersionSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()

    @staticmethod
    def get_state(obj):
        actions = set(obj.events.values_list("action", flat=True))
        if "rejected" in actions:
            return "rejected"
        if getattr(obj.policy, "active_version_id", None) == obj.id:
            return "active"
        if "published" in actions:
            return "superseded"
        return "draft"

    class Meta:
        model = MatchScoringPolicyVersion
        fields = (
            "id",
            "policy",
            "version",
            "dimension_weights",
            "component_weights",
            "checksum",
            "source",
            "rationale",
            "state",
            "created_at",
        )
        read_only_fields = fields


class MatchScoringPolicySerializer(serializers.ModelSerializer):
    active_version_detail = MatchScoringPolicyVersionSerializer(
        source="active_version", read_only=True
    )

    class Meta:
        model = MatchScoringPolicy
        fields = (
            "id",
            "opportunity_type",
            "revision",
            "active_version",
            "active_version_detail",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class WeightSuggestionGenerateSerializer(StrictSerializer):
    opportunity_type = serializers.ChoiceField(choices=MatchOpportunityType.choices)
    expected_revision = serializers.IntegerField(min_value=0)


class WeightSuggestionReviewSerializer(StrictSerializer):
    action = serializers.ChoiceField(choices=MatchWeightSuggestionReviewAction.choices)
    expected_revision = serializers.IntegerField(min_value=0)
    reason_code = serializers.RegexField(
        regex=r"^[a-z0-9][a-z0-9_.-]{0,63}$",
        required=False,
        allow_blank=True,
        default="",
    )


class MatchWeightSuggestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchWeightSuggestion
        fields = (
            "id",
            "policy",
            "opportunity_type",
            "dimension_weights",
            "component_weights",
            "rationale",
            "sample_count",
            "base_policy_checksum",
            "status",
            "revision",
            "generator",
            "accepted_draft",
            "reviewed_at",
            "created_at",
        )
        read_only_fields = fields


class FeedbackQueueMatchSerializer(serializers.ModelSerializer):
    match_id = serializers.UUIDField(source="id", read_only=True)
    person = MatchPersonSummarySerializer(read_only=True)
    opportunity = serializers.SerializerMethodField()
    verdict = serializers.CharField(source="recommendation_verdict", read_only=True)
    latest_outcome = serializers.SerializerMethodField()

    @staticmethod
    def get_opportunity(obj):
        return {
            "id": str(obj.opportunity_id),
            "title": obj.opportunity.title,
            "type": obj.opportunity.opportunity_type,
        }

    @staticmethod
    def get_latest_outcome(obj):
        return {
            "code": obj.latest_outcome_code,
            "occurred_at": obj.latest_outcome_at,
        }

    class Meta:
        model = Match
        fields = (
            "match_id",
            "person",
            "opportunity",
            "status",
            "overall_score",
            "verdict",
            "feedback_revision",
            "ranking_revision",
            "latest_outcome",
            "evaluated_at",
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
            "scoring_policy_version",
            "scoring_policy_checksum",
            "dimension_weights",
            "component_weights",
            "started_at",
            "completed_at",
            "outcome",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class PersonImportPreviewRequestSerializer(StrictSerializer):
    file = serializers.FileField()
    mapping = serializers.JSONField()

    def validate_file(self, value):
        if not str(value.name or "").lower().endswith(".csv"):
            raise serializers.ValidationError("File must have a .csv extension.")
        return value

    def validate_mapping(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError(
                    "mapping must be valid JSON."
                ) from exc
        if not isinstance(value, dict):
            raise serializers.ValidationError("mapping must be a JSON object.")
        return value


class CRMImportCandidateQuerySerializer(StrictSerializer):
    entity_type = serializers.ChoiceField(choices=("lead", "contact"))
    search = serializers.CharField(
        required=False, allow_blank=True, max_length=200, trim_whitespace=True
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=100, default=50)


class CRMImportPreviewRequestSerializer(StrictSerializer):
    entity_type = serializers.ChoiceField(choices=("lead", "contact"))
    record_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=500
    )

    def validate_record_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("record_ids must be unique.")
        return value


class PersonImportCommitRequestSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)


class PersonImportResolveRequestSerializer(StrictSerializer):
    action = serializers.ChoiceField(choices=PersonImportDecisionAction.choices)
    person_id = serializers.UUIDField(required=False, allow_null=True)
    expected_revision = serializers.IntegerField(min_value=0)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if (
            attrs["action"] == PersonImportDecisionAction.LINK_EXISTING
            and attrs.get("person_id") is None
        ):
            raise serializers.ValidationError(
                {"person_id": "Required for link_existing."}
            )
        if (
            attrs["action"] == PersonImportDecisionAction.SKIP
            and attrs.get("person_id") is not None
        ):
            raise serializers.ValidationError(
                {"person_id": "Must be omitted for skip."}
            )
        return attrs


class PersonImportConflictSerializer(serializers.ModelSerializer):
    candidates = serializers.SerializerMethodField()

    def get_candidates(self, obj):
        people_map = self.context.get("candidate_people", {})
        missing = [value for value in obj.person_ids if str(value) not in people_map]
        if missing:
            for person in Person.objects.filter(org=obj.org, id__in=missing):
                people_map[str(person.id)] = person
        return [
            {
                "id": person.id,
                "display_name": person.display_name,
                "current_title": person.current_title,
                "current_company": person.current_company,
                "location": person.location,
            }
            for value in obj.person_ids
            if (person := people_map.get(str(value))) is not None
        ]

    class Meta:
        model = PersonImportConflict
        fields = (
            "id",
            "code",
            "person_ids",
            "candidates",
            "status",
            "revision",
            "resolved_at",
        )
        read_only_fields = fields


class PersonImportRecordSerializer(serializers.ModelSerializer):
    conflict = PersonImportConflictSerializer(read_only=True)
    person_summary = serializers.SerializerMethodField()
    masked_identities = serializers.SerializerMethodField()
    field_errors = serializers.SerializerMethodField()

    @staticmethod
    def get_person_summary(obj):
        if obj.person_id is None:
            return None
        return {
            "id": obj.person.id,
            "display_name": obj.person.display_name,
            "current_title": obj.person.current_title,
            "current_company": obj.person.current_company,
            "location": obj.person.location,
        }

    @staticmethod
    def get_masked_identities(obj):
        if not isinstance(obj.masked_identities, list):
            return []
        safe = []
        valid_kinds = {value for value, _label in PersonIdentityKind.choices}
        for item in obj.masked_identities[:20]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            masked_value = str(item.get("masked_value") or "")[:500]
            if obj.batch.source == EvidenceSource.EMAIL and kind in valid_kinds:
                # Inbound-email previews have a stricter browser boundary than
                # operator-selected CSV/provider imports.  Even a masked address
                # can identify a private correspondent when combined with the
                # display name, so expose presence only.
                safe.append({"kind": kind, "present": True})
                continue
            if kind in valid_kinds and masked_value and "***" in masked_value:
                safe.append({"kind": kind, "masked_value": masked_value})
        return safe

    @staticmethod
    def get_field_errors(obj):
        if not isinstance(obj.field_errors, list):
            return []
        safe = []
        for item in obj.field_errors[:50]:
            if not isinstance(item, dict):
                continue
            safe.append(
                {
                    "field": str(item.get("field") or "row")[:80],
                    "code": str(item.get("code") or "invalid_row")[:80],
                    "detail": str(item.get("detail") or "Row validation failed.")[:500],
                }
            )
        return safe

    class Meta:
        model = PersonImportRecord
        fields = (
            "id",
            "row_number",
            "display_name",
            "status",
            "revision",
            "person",
            "person_summary",
            "masked_identities",
            "field_errors",
            "error_code",
            "conflict",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class PersonImportBatchSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()
    job_id = serializers.UUIDField(source="automation_job.id", read_only=True)
    job_status = serializers.CharField(source="automation_job.status", read_only=True)
    error_code = serializers.SerializerMethodField()
    status_url = serializers.SerializerMethodField()

    @staticmethod
    def get_counts(obj):
        return {
            "total": obj.total_count,
            "processed": obj.processed_count,
            "ready": obj.ready_count,
            "created": obj.created_count,
            "merged": obj.merged_count,
            "conflict": obj.conflict_count,
            "invalid": obj.invalid_count,
            "skipped": obj.skipped_count,
            "replayed": obj.replayed_count,
            "failed": obj.failed_count,
        }

    @staticmethod
    def get_status(obj):
        if (
            obj.automation_job_id
            and obj.automation_job.status in {"dead_letter", "cancelled"}
            and obj.status not in {"completed", "partial", "failed"}
        ):
            return "failed"
        return obj.status

    @staticmethod
    def get_error_code(obj):
        if obj.error_code:
            return obj.error_code
        if obj.automation_job_id:
            return obj.automation_job.last_error_code
        return ""

    @staticmethod
    def get_status_url(obj):
        return f"/api/matching/person-imports/{obj.id}/"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.source == EvidenceSource.EMAIL:
            # The stored namespace contains a mailbox-scoped digest used for
            # replay isolation.  It is an internal ledger key, not UI data.
            data["source_namespace"] = "email:inbound"
            data["original_filename"] = "Inbound email preview"
        return data

    class Meta:
        model = PersonImportBatch
        fields = (
            "id",
            "status",
            "revision",
            "original_filename",
            "file_size",
            "headers",
            "mapping",
            "source",
            "source_namespace",
            "counts",
            "job_id",
            "job_status",
            "error_code",
            "match_run_ids",
            "status_url",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class PersonImportDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonImportDecision
        fields = (
            "id",
            "record",
            "conflict",
            "action",
            "target_person",
            "expected_revision",
            "resulting_revision",
            "created_at",
        )
        read_only_fields = fields


class EvidenceGovernanceUpdateSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    idempotency_key = serializers.UUIDField(required=False, write_only=True)
    collection_method = serializers.ChoiceField(
        choices=EvidenceCollectionMethod.choices,
        required=False,
    )
    lawful_basis = serializers.ChoiceField(
        choices=EvidenceLawfulBasis.choices,
        required=False,
    )
    lawful_basis_notes = serializers.CharField(
        max_length=2000,
        allow_blank=True,
        required=False,
        trim_whitespace=True,
        write_only=True,
    )
    consent_at = serializers.DateTimeField(required=False, allow_null=True)
    consent_evidence_ref = serializers.CharField(
        max_length=1000,
        allow_blank=True,
        required=False,
        trim_whitespace=True,
        write_only=True,
    )
    country_code = serializers.RegexField(
        r"^[A-Za-z]{2,3}$",
        required=False,
        allow_blank=True,
        max_length=3,
    )
    allowed_channels = serializers.ListField(
        child=serializers.ChoiceField(choices=GovernanceContactChannel.choices),
        max_length=20,
        required=False,
    )
    allowed_purposes = serializers.ListField(
        child=serializers.ChoiceField(choices=PersonContactIntentPurpose.choices),
        max_length=20,
        required=False,
    )
    retention_until = serializers.DateTimeField(required=False, allow_null=True)
    processing_status = serializers.ChoiceField(
        choices=EvidenceProcessingStatus.choices,
        required=False,
    )
    source_content_sha256 = serializers.RegexField(
        r"^[0-9a-f]{64}$",
        required=False,
        allow_blank=True,
        max_length=64,
        write_only=True,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        mutable = set(attrs) - {"expected_revision", "idempotency_key"}
        if not mutable:
            raise serializers.ValidationError("At least one governance field is required.")
        return attrs


class EvidenceReviewSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    idempotency_key = serializers.UUIDField(required=False, write_only=True)
    decision = serializers.ChoiceField(choices=("confirm", "reject"), required=False)
    action = serializers.ChoiceField(choices=("confirm", "reject"), required=False)
    reason_code = serializers.RegexField(r"^[a-z0-9_.:-]{1,64}$")
    reason = serializers.CharField(
        max_length=1000,
        allow_blank=True,
        required=False,
        trim_whitespace=True,
        write_only=True,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        decision = attrs.get("decision") or attrs.get("action")
        if not decision:
            raise serializers.ValidationError({"decision": "This field is required."})
        if attrs.get("decision") and attrs.get("action") and attrs["decision"] != attrs["action"]:
            raise serializers.ValidationError("decision and action must agree.")
        attrs["action"] = decision
        attrs.pop("decision", None)
        return attrs


class PersonContactIntentMutationSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    idempotency_key = serializers.UUIDField(required=False, write_only=True)
    channel = serializers.ChoiceField(choices=GovernanceContactChannel.choices)
    purpose = serializers.ChoiceField(choices=PersonContactIntentPurpose.choices)
    state = serializers.ChoiceField(choices=PersonContactIntentState.choices)
    source = serializers.ChoiceField(
        choices=EvidenceSource.choices,
        default=EvidenceSource.MANUAL,
        required=False,
    )
    identity_id = serializers.UUIDField(required=False, allow_null=True)
    evidence_id = serializers.UUIDField(required=False, allow_null=True)
    opportunity_id = serializers.UUIDField(required=False, allow_null=True)
    confidence = serializers.DecimalField(
        max_digits=4,
        decimal_places=3,
        min_value=0,
        max_value=1,
        default="0.500",
        required=False,
    )
    observed_at = serializers.DateTimeField(required=False)
    valid_until = serializers.DateTimeField(required=False, allow_null=True)
    reason_code = serializers.RegexField(
        r"^[a-z0-9_.:-]{1,64}$",
        required=False,
        allow_blank=True,
    )


class ContactEligibilitySerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    idempotency_key = serializers.UUIDField(required=False, write_only=True)
    identity_id = serializers.UUIDField()
    channel = serializers.ChoiceField(choices=GovernanceContactChannel.choices)
    purpose = serializers.ChoiceField(choices=PersonContactIntentPurpose.choices)


class PersonExportSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)
    idempotency_key = serializers.UUIDField(required=False, write_only=True)


class PersonDeletionSerializer(PersonExportSerializer):
    action = serializers.ChoiceField(choices=("request", "cancel", "anonymize"))
    confirm_person_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["action"] == "anonymize" and "confirm_person_id" not in attrs:
            raise serializers.ValidationError(
                {"confirm_person_id": "Explicit person confirmation is required."}
            )
        return attrs


class RetentionScanSerializer(PersonExportSerializer):
    execute = serializers.BooleanField(default=False, required=False)
    limit = serializers.IntegerField(min_value=1, max_value=500, default=200, required=False)
