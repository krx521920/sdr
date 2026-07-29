from rest_framework import serializers

from integrations.models import FacebookOAuthSession, FacebookPageConnection


class FacebookPageConnectionCreateSerializer(serializers.Serializer):
    page_access_token = serializers.CharField(max_length=4096, trim_whitespace=True)
    token_expires_at = serializers.DateTimeField(required=False, allow_null=True)


class FacebookPageConnectionSerializer(serializers.ModelSerializer):
    page_id = serializers.CharField(read_only=True)

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
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


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
