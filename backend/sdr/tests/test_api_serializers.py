from integrations.api.website_serializers import WebsiteLeadIntakeSerializer


def test_website_intake_requires_email_or_phone():
    serializer = WebsiteLeadIntakeSerializer(
        data={"source_record_id": "submission-1", "first_name": "Ada"}
    )

    assert serializer.is_valid() is False
    assert "non_field_errors" in serializer.errors


def test_website_intake_accepts_idempotent_email_submission():
    serializer = WebsiteLeadIntakeSerializer(
        data={
            "source_record_id": "submission-1",
            "email": "ada@example.com",
            "utm_campaign": "enterprise-sdr",
        }
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["source_record_id"] == "submission-1"
