from rest_framework import serializers

from automation.models import AutomationJob, AutomationJobAttempt


class AutomationJobAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomationJobAttempt
        fields = (
            "id",
            "attempt_number",
            "status",
            "started_at",
            "finished_at",
            "error_code",
            "error_message",
        )
        read_only_fields = fields


class AutomationJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomationJob
        fields = (
            "id",
            "name",
            "idempotency_key",
            "queue",
            "status",
            "attempt_count",
            "max_attempts",
            "replay_count",
            "scheduled_for",
            "queued_at",
            "started_at",
            "completed_at",
            "last_error_code",
            "last_error_message",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AutomationJobDetailSerializer(AutomationJobSerializer):
    attempts = AutomationJobAttemptSerializer(many=True, read_only=True)
    payload = serializers.JSONField(read_only=True)
    result = serializers.JSONField(read_only=True)

    class Meta(AutomationJobSerializer.Meta):
        fields = AutomationJobSerializer.Meta.fields + (
            "payload",
            "result",
            "attempts",
        )
