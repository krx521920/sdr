import re

from django.db.models import Count
from rest_framework import serializers

from integrations.models import (
    FacebookConversionEvent,
    FacebookConversionSettings,
    FacebookOAuthSession,
    FacebookPageConnection,
)
from integrations.providers.facebook.messenger import validate_auto_reply_template
from sdr.domain import QualificationBand

EVENT_NAME_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")


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
            value = str(attrs.get(field_name, getattr(self.instance, field_name, ""))).strip()
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
