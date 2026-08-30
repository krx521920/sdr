import re

from django.db.models import Count
from rest_framework import serializers

from integrations.models import (
    FEISHU_BASE_FIELD_KEYS,
    ApolloConnection,
    ExecutionChannel,
    FacebookConversionEvent,
    FacebookConversionSettings,
    FacebookOAuthSession,
    FacebookPageConnection,
    FeishuBaseConnection,
    FeishuBaseSync,
    LinkedInConnection,
    LinkedInInvitation,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
)
from integrations.providers.facebook.messenger import validate_auto_reply_template
from sdr.domain import QualificationBand


class ChannelSafetyOrganizationWriteSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    daily_limit = serializers.IntegerField(min_value=0, max_value=10_000_000)
    expected_revision = serializers.IntegerField(min_value=0)


class ChannelSafetyChannelWriteSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    test_mode = serializers.BooleanField()
    daily_limit = serializers.IntegerField(min_value=0, max_value=1_000_000)
    per_execution_limit = serializers.IntegerField(min_value=0, max_value=1_000_000)
    expected_revision = serializers.IntegerField(min_value=0)


class ChannelSafetyTargetWriteSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=ExecutionChannel.choices)
    identifier = serializers.CharField(max_length=1000, trim_whitespace=True)
    safe_label = serializers.CharField(max_length=120, trim_whitespace=True)


class ChannelSafetyApprovalWriteSerializer(serializers.Serializer):
    target_id = serializers.UUIDField()
    action = serializers.RegexField(r"^[a-z][a-z0-9_.:-]{0,63}$")
    payload_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    units = serializers.IntegerField(min_value=1, max_value=1_000_000)
    expires_in_seconds = serializers.IntegerField(min_value=60, max_value=86400)


class ChannelSafetyUnknownResolveSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=("delivered", "failed_consumed"))

EVENT_NAME_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")


class ApolloConnectionSerializer(serializers.ModelSerializer):
    api_key_configured = serializers.SerializerMethodField()

    class Meta:
        model = ApolloConnection
        fields = (
            "id",
            "api_key_configured",
            "api_key_hint",
            "is_active",
            "last_sync_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_api_key_configured(self, obj) -> bool:
        return bool(obj.api_key_ciphertext)


class ApolloConnectionWriteSerializer(serializers.Serializer):
    api_key = serializers.CharField(
        max_length=4096,
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        write_only=True,
    )
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs):
        connection = self.context.get("connection")
        has_key = bool(attrs.get("api_key")) or bool(
            connection and connection.api_key_ciphertext
        )
        if not has_key:
            raise serializers.ValidationError(
                {"api_key": "An Apollo API key is required."}
            )
        return attrs


class LinkedInConnectionSerializer(serializers.ModelSerializer):
    access_token_configured = serializers.SerializerMethodField()
    invitation_summary = serializers.SerializerMethodField()

    class Meta:
        model = LinkedInConnection
        fields = (
            "id",
            "access_token_configured",
            "access_token_hint",
            "is_active",
            "partner_access_confirmed",
            "last_invitation_sent_at",
            "invitation_summary",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_access_token_configured(self, obj) -> bool:
        return bool(obj.access_token_ciphertext)

    def get_invitation_summary(self, obj) -> dict[str, int]:
        counts = {
            row["status"]: row["count"]
            for row in LinkedInInvitation.objects.filter(org_id=obj.org_id)
            .values("status")
            .annotate(count=Count("id"))
        }
        return {
            "total": sum(counts.values()),
            **{
                value: counts.get(value, 0)
                for value in (
                    "pending",
                    "queued",
                    "sending",
                    "sent",
                    "failed",
                    "skipped",
                )
            },
        }


class LinkedInConnectionWriteSerializer(serializers.Serializer):
    access_token = serializers.CharField(
        max_length=4096,
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        write_only=True,
    )
    is_active = serializers.BooleanField(required=False)
    partner_access_confirmed = serializers.BooleanField(required=False)

    def validate(self, attrs):
        connection = self.context.get("connection")
        enabled = attrs.get("is_active", connection.is_active if connection else False)
        partner_confirmed = attrs.get(
            "partner_access_confirmed",
            connection.partner_access_confirmed if connection else False,
        )
        has_token = bool(attrs.get("access_token")) or bool(
            connection and connection.access_token_ciphertext
        )
        if enabled and not has_token:
            raise serializers.ValidationError(
                {"access_token": "An official LinkedIn API access token is required."}
            )
        if enabled and not partner_confirmed:
            raise serializers.ValidationError(
                {
                    "partner_access_confirmed": (
                        "Confirm that this organization has approved LinkedIn partner API access."
                    )
                }
            )
        return attrs


class FeishuBaseConnectionSerializer(serializers.ModelSerializer):
    app_secret_configured = serializers.SerializerMethodField()
    app_id_configured = serializers.SerializerMethodField()
    app_token_configured = serializers.SerializerMethodField()
    table_id_configured = serializers.SerializerMethodField()
    sync_summary = serializers.SerializerMethodField()

    class Meta:
        model = FeishuBaseConnection
        fields = (
            "id",
            "app_id",
            "app_id_configured",
            "app_secret_configured",
            "app_token_configured",
            "table_id_configured",
            "field_mapping",
            "is_active",
            "last_validated_at",
            "last_sync_at",
            "sync_summary",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_app_secret_configured(self, obj) -> bool:
        return bool(obj.app_secret_ciphertext)

    def get_app_id_configured(self, obj) -> bool:
        return bool(obj.app_id)

    def get_app_token_configured(self, obj) -> bool:
        return bool(obj.app_token)

    def get_table_id_configured(self, obj) -> bool:
        return bool(obj.table_id)

    def get_sync_summary(self, obj) -> dict[str, int]:
        counts = {
            row["status"]: row["count"]
            for row in FeishuBaseSync.objects.filter(org_id=obj.org_id)
            .values("status")
            .annotate(count=Count("id"))
        }
        statuses = (
            "pending",
            "queued",
            "syncing",
            "succeeded",
            "failed",
            "skipped",
            "unknown",
            "external_erasure_pending",
            "external_erasure_completed",
        )
        return {
            "total": sum(counts.values()),
            **{value: counts.get(value, 0) for value in statuses},
        }


class FeishuBaseConnectionWriteSerializer(serializers.Serializer):
    app_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    app_secret = serializers.CharField(
        max_length=4096,
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        write_only=True,
    )
    app_token = serializers.CharField(max_length=255, required=False, allow_blank=True)
    table_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    field_mapping = serializers.DictField(
        child=serializers.CharField(max_length=100, trim_whitespace=True),
        required=False,
    )
    is_active = serializers.BooleanField(required=False)

    def validate_field_mapping(self, value):
        unknown = sorted(set(value) - FEISHU_BASE_FIELD_KEYS)
        if unknown:
            raise serializers.ValidationError(
                f"Unknown field mapping keys: {', '.join(unknown)}"
            )
        names = list(value.values())
        if len(names) != len(set(names)):
            raise serializers.ValidationError(
                "Each Feishu field can be mapped only once."
            )
        return value

    def validate(self, attrs):
        connection = self.context.get("connection")
        enabled = attrs.get("is_active", connection.is_active if connection else False)
        if not enabled:
            return attrs
        values = {
            "app_id": attrs.get("app_id", connection.app_id if connection else ""),
            "app_secret": bool(attrs.get("app_secret"))
            or bool(connection and connection.app_secret_ciphertext),
            "app_token": attrs.get(
                "app_token", connection.app_token if connection else ""
            ),
            "table_id": attrs.get(
                "table_id", connection.table_id if connection else ""
            ),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise serializers.ValidationError(
                {
                    name: "This value is required before enabling Base sync."
                    for name in missing
                }
            )
        return attrs


class FeishuBaseExecutionWriteSerializer(serializers.Serializer):
    """Second-stage grant for one exact Feishu Base execution.

    An empty object deliberately remains valid: it is the read-only first
    stage that returns the execution intent. Supplying either grant field
    requires the other so no partial authorization can reach the service.
    """

    approval_id = serializers.UUIDField(required=False)
    idempotency_key = serializers.UUIDField(required=False)

    def validate(self, attrs):
        unknown = sorted(
            set(getattr(self, "initial_data", {}) or {})
            - {"approval_id", "idempotency_key"}
        )
        if unknown:
            raise serializers.ValidationError(
                {name: "This field is not accepted." for name in unknown}
            )
        has_approval = "approval_id" in attrs
        has_key = "idempotency_key" in attrs
        if has_approval != has_key:
            raise serializers.ValidationError(
                "approval_id and idempotency_key must be supplied together."
            )
        return attrs


class FeishuBasePersonImportWriteSerializer(serializers.Serializer):
    """One-off Base-to-Person mapping plus an optional exact execution grant."""

    mapping = serializers.DictField(
        child=serializers.CharField(max_length=100, trim_whitespace=True),
        allow_empty=False,
    )
    limit = serializers.IntegerField(
        required=False,
        default=100,
        min_value=1,
        max_value=500,
    )
    approval_id = serializers.UUIDField(required=False)
    idempotency_key = serializers.UUIDField(required=False)

    def validate_mapping(self, value):
        allowed = {
            "display_name",
            "first_name",
            "last_name",
            "current_title",
            "current_company",
            "location",
            "email",
            "phone",
            "linkedin",
            "evidence_summary",
            "observed_at",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise serializers.ValidationError("Unsupported person import field.")
        names = list(value.values())
        if len(names) != len(set(names)):
            raise serializers.ValidationError(
                "Each Feishu field can be mapped only once."
            )
        if not {"email", "phone", "linkedin"}.intersection(value):
            raise serializers.ValidationError(
                "Map at least one of email, phone, or linkedin."
            )
        if "display_name" not in value and not {"first_name", "last_name"}.intersection(
            value
        ):
            raise serializers.ValidationError(
                "Map display_name, first_name, or last_name."
            )
        return value

    def validate(self, attrs):
        unknown = sorted(
            set(getattr(self, "initial_data", {}) or {})
            - {"mapping", "limit", "approval_id", "idempotency_key"}
        )
        if unknown:
            raise serializers.ValidationError(
                {name: "This field is not accepted." for name in unknown}
            )
        has_approval = "approval_id" in attrs
        has_key = "idempotency_key" in attrs
        if has_approval != has_key:
            raise serializers.ValidationError(
                "approval_id and idempotency_key must be supplied together."
            )
        return attrs


class FacebookPageConnectionCreateSerializer(serializers.Serializer):
    page_access_token = serializers.CharField(max_length=4096, trim_whitespace=True)
    token_expires_at = serializers.DateTimeField(required=False, allow_null=True)


class FacebookPageConnectionSerializer(serializers.ModelSerializer):
    page_id = serializers.CharField(read_only=True)
    messenger_message_count = serializers.IntegerField(
        source="messenger_messages.count",
        read_only=True,
    )
    messenger_reply_summary = serializers.SerializerMethodField()

    @staticmethod
    def get_messenger_reply_summary(obj):
        counts = {
            row["status"]: row["count"]
            for row in obj.messenger_replies.values("status").annotate(
                count=Count("id")
            )
        }
        return {
            status: counts.get(status, 0)
            for status in ("pending", "queued", "sending", "sent", "skipped", "failed")
        }

    class Meta:
        model = FacebookPageConnection
        fields = (
            "id",
            "page_id",
            "page_name",
            "access_token_hint",
            "token_expires_at",
            "is_active",
            "last_webhook_at",
            "messenger_enabled",
            "messenger_auto_reply_enabled",
            "messenger_auto_reply_template",
            "last_message_at",
            "last_message_reply_at",
            "messenger_message_count",
            "messenger_reply_summary",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class FacebookMessengerSettingsSerializer(serializers.Serializer):
    messenger_enabled = serializers.BooleanField(required=False)
    messenger_auto_reply_enabled = serializers.BooleanField(required=False)
    messenger_auto_reply_template = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=2000,
        trim_whitespace=True,
    )

    def validate_messenger_auto_reply_template(self, value):
        try:
            return validate_auto_reply_template(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide a Messenger setting to update.")
        if (
            attrs.get("messenger_enabled") is False
            and attrs.get("messenger_auto_reply_enabled") is True
        ):
            raise serializers.ValidationError(
                "Messenger intake must be enabled before automatic replies."
            )
        return attrs


class FacebookMessengerManualReplySerializer(serializers.Serializer):
    client_request_id = serializers.UUIDField()
    body = serializers.CharField(
        allow_blank=False,
        max_length=2000,
        trim_whitespace=True,
    )


class FacebookOAuthStartSerializer(serializers.Serializer):
    session_id = serializers.UUIDField(read_only=True)
    authorization_url = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)


class FacebookOAuthSessionSerializer(serializers.ModelSerializer):
    pages = serializers.JSONField(source="pages_snapshot", read_only=True)

    class Meta:
        model = FacebookOAuthSession
        fields = (
            "id",
            "status",
            "pages",
            "error_code",
            "expires_at",
            "completed_at",
            "created_at",
        )
        read_only_fields = fields


class FacebookOAuthPageSelectionSerializer(serializers.Serializer):
    page_ids = serializers.ListField(
        child=serializers.CharField(max_length=64, trim_whitespace=True),
        min_length=1,
        allow_empty=False,
    )


class FacebookConversionSettingsSerializer(serializers.ModelSerializer):
    access_token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=4096,
        trim_whitespace=True,
    )
    access_token_configured = serializers.SerializerMethodField()
    event_summary = serializers.SerializerMethodField()
    qualified_bands = serializers.ListField(
        child=serializers.ChoiceField(
            choices=[(band.value, band.value.title()) for band in QualificationBand]
        ),
        allow_empty=True,
    )

    class Meta:
        model = FacebookConversionSettings
        fields = (
            "is_enabled",
            "pixel_id",
            "access_token",
            "access_token_configured",
            "access_token_hint",
            "lead_event_source",
            "raw_lead_event_name",
            "qualified_lead_event_name",
            "converted_event_name",
            "qualified_bands",
            "test_event_code",
            "last_event_sent_at",
            "event_summary",
            "updated_at",
        )
        read_only_fields = (
            "access_token_configured",
            "access_token_hint",
            "last_event_sent_at",
            "event_summary",
            "updated_at",
        )

    def get_access_token_configured(self, obj) -> bool:
        return bool(obj.access_token_ciphertext)

    def get_event_summary(self, obj) -> dict[str, int]:
        counts = {
            row["status"]: row["count"]
            for row in FacebookConversionEvent.objects.filter(org_id=obj.org_id)
            .values("status")
            .annotate(count=Count("id"))
        }
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
        }

    def validate_pixel_id(self, value: str) -> str:
        cleaned = value.strip()
        if cleaned and (not cleaned.isdigit() or len(cleaned) > 32):
            raise serializers.ValidationError("Enter the numeric Meta Pixel ID.")
        return cleaned

    def validate(self, attrs):
        attrs = super().validate(attrs)
        for field_name in (
            "lead_event_source",
            "raw_lead_event_name",
            "qualified_lead_event_name",
            "converted_event_name",
        ):
            value = str(
                attrs.get(field_name, getattr(self.instance, field_name, ""))
            ).strip()
            if not EVENT_NAME_PATTERN.fullmatch(value):
                raise serializers.ValidationError(
                    {field_name: "Use 1-100 printable characters."}
                )
            attrs[field_name] = value

        is_enabled = attrs.get(
            "is_enabled",
            self.instance.is_enabled if self.instance else False,
        )
        pixel_id = attrs.get(
            "pixel_id",
            self.instance.pixel_id if self.instance else "",
        )
        has_token = bool(attrs.get("access_token", "").strip()) or bool(
            self.instance and self.instance.access_token_ciphertext
        )
        if is_enabled and not pixel_id:
            raise serializers.ValidationError(
                {"pixel_id": "A Meta Pixel ID is required before enabling feedback."}
            )
        if is_enabled and not has_token:
            raise serializers.ValidationError(
                {"access_token": "A Conversions API access token is required."}
            )
        return attrs

    def update(self, instance, validated_data):
        access_token = validated_data.pop("access_token", "").strip()
        for field_name, value in validated_data.items():
            setattr(instance, field_name, value)
        if access_token:
            instance.set_access_token(access_token)
        instance.save()
        return instance


class WhatsAppBusinessConnectionSerializer(serializers.ModelSerializer):
    phone_number_id = serializers.CharField(read_only=True)
    access_token_configured = serializers.SerializerMethodField()
    message_summary = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppBusinessConnection
        fields = (
            "id",
            "phone_number_id",
            "business_account_id",
            "display_phone_number",
            "access_token_configured",
            "access_token_hint",
            "is_active",
            "last_message_sent_at",
            "last_webhook_at",
            "message_summary",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_access_token_configured(self, obj) -> bool:
        return bool(obj.access_token_ciphertext)

    def get_message_summary(self, obj) -> dict[str, int]:
        counts = {
            row["status"]: row["count"]
            for row in WhatsAppMessage.objects.filter(org_id=obj.org_id)
            .values("status")
            .annotate(count=Count("id"))
        }
        return {
            "total": sum(counts.values()),
            **{
                status: counts.get(status, 0)
                for status in (
                    "pending",
                    "queued",
                    "sending",
                    "sent",
                    "delivered",
                    "read",
                    "unknown",
                    "failed",
                    "skipped",
                )
            },
        }


class WhatsAppMessageExecutionSerializer(serializers.Serializer):
    """Strict optional approval for one immutable WhatsApp message."""

    approval_id = serializers.UUIDField(required=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError(
                "Use an object for WhatsApp execution approval."
            )
        unknown = sorted(set(data) - {"approval_id"})
        if unknown:
            raise serializers.ValidationError(
                {key: "Unsupported field." for key in unknown}
            )
        return super().to_internal_value(data)


class WhatsAppMessageListQuerySerializer(serializers.Serializer):
    campaign_id = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(
        choices=WhatsAppMessageStatus.choices,
        required=False,
    )
    limit = serializers.IntegerField(
        min_value=1,
        max_value=100,
        default=50,
        required=False,
    )

    def to_internal_value(self, data):
        unknown = sorted(set(data.keys()) - {"campaign_id", "status", "limit"})
        if unknown:
            raise serializers.ValidationError(
                {key: "Unsupported query parameter." for key in unknown}
            )
        return super().to_internal_value(data)


class WhatsAppBusinessConnectionWriteSerializer(serializers.Serializer):
    phone_number_id = serializers.RegexField(r"^\d{1,64}$", max_length=64)
    business_account_id = serializers.RegexField(
        r"^\d{1,64}$",
        max_length=64,
        required=False,
        allow_blank=True,
    )
    display_phone_number = serializers.CharField(
        max_length=32,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    access_token = serializers.CharField(
        max_length=4096,
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        write_only=True,
    )
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs):
        connection = self.context.get("connection")
        enabled = attrs.get(
            "is_active",
            connection.is_active if connection else False,
        )
        has_token = bool(attrs.get("access_token")) or bool(
            connection and connection.access_token_ciphertext
        )
        if (enabled or connection is None) and not has_token:
            raise serializers.ValidationError(
                {
                    "access_token": "An access token is required before enabling WhatsApp."
                }
            )
        return attrs
