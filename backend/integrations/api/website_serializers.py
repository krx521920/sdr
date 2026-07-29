from rest_framework import serializers


class WebsiteLeadIntakeSerializer(serializers.Serializer):
    source_record_id = serializers.CharField(max_length=255)
    first_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=25, required=False, allow_blank=True)
    company_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    website = serializers.URLField(required=False, allow_blank=True)
    industry = serializers.CharField(max_length=255, required=False, allow_blank=True)
    country = serializers.CharField(max_length=3, required=False, allow_blank=True)
    job_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    linkedin_url = serializers.URLField(required=False, allow_blank=True)
    message = serializers.CharField(required=False, allow_blank=True)
    page_url = serializers.URLField(required=False, allow_blank=True)
    utm_source = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utm_medium = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utm_campaign = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )

    def validate(self, attrs):
        if not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError("email or phone is required")
        return attrs


class WebsiteLeadIntakeResponseSerializer(serializers.Serializer):
    intake_id = serializers.UUIDField()
    lead_id = serializers.UUIDField(allow_null=True)
    crm_created = serializers.BooleanField(allow_null=True)
    matched_existing = serializers.BooleanField()
    qualification_score = serializers.IntegerField(allow_null=True)
    qualification_band = serializers.CharField(allow_blank=True)
    assigned_profile_id = serializers.UUIDField(allow_null=True)
    routing_rule_id = serializers.UUIDField(allow_null=True)
    routing_reason = serializers.CharField(allow_blank=True)
    replayed = serializers.BooleanField()
