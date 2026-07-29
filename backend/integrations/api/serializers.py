from rest_framework import serializers

from integrations.models import FacebookPageConnection


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
