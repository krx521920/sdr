from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from integrations.models import (
    LinkedInConnection,
    LinkedInInvitation,
    LinkedInInvitationStatus,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppMessageStatus,
    WhatsAppPhoneRoute,
)
from leads.models import Lead
from sdr.compliance import (
    anonymize_intake,
    block_contact,
    ensure_intake_provenance,
    evaluate_contact,
    request_intake_deletion,
    scan_retention,
)
from sdr.domain import CompanySnapshot, LeadCandidate, LeadIdentity, LeadSource
from sdr.models import (
    LeadIntake,
    SDRChannelComplianceRule,
    SDRComplianceEvent,
    SDRComplianceSettings,
    SDRDoNotContactReason,
    SDRDoNotContactSource,
    SDRLawfulBasis,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRProvenanceStatus,
    SDRRetentionMode,
)
from sdr.outbound import import_prospect_csv


def _outbound_record(org, *, suffix="one"):
    lead = Lead.objects.create(
        org=org,
        email=f"{suffix}@example.com",
        first_name="Ada",
        company_name="Factory One",
        status="in process",
    )
    intake = LeadIntake.objects.create(
        org=org,
        source="outbound",
        source_record_id=f"compliance:{suffix}",
        raw_payload={
            "email": lead.email,
            "company_name": lead.company_name,
            "country": "US",
            "source_url": "https://example.com/source",
        },
        normalized_payload={
            "identity": {"email": lead.email, "first_name": "Ada"},
            "company": {"name": lead.company_name, "country": "US"},
        },
        status="completed",
        crm_lead=lead,
    )
    campaign = SDROutboundCampaign.objects.create(
        org=org,
        name=f"Compliance {suffix}",
        channels=["email", "whatsapp", "linkedin"],
    )
    prospect = SDROutboundProspect.objects.create(
        org=org,
        campaign=campaign,
        company_name=lead.company_name,
        email=lead.email,
        phone="15551234567",
        linkedin_url="https://www.linkedin.com/in/ada",
        country="US",
        source_url="https://example.com/source",
        dedupe_key=f"compliance-{suffix}",
        intake=intake,
    )
    return lead, intake, prospect


@pytest.mark.django_db
def test_provenance_is_explicit_and_never_infers_a_lawful_basis(org_a):
    _, intake, prospect = _outbound_record(org_a)
    candidate = LeadCandidate(
        org_id=org_a.id,
        source=LeadSource.OUTBOUND,
        source_record_id=str(prospect.id),
        identity=LeadIdentity(email=prospect.email),
        company=CompanySnapshot(name=prospect.company_name, country="United States"),
        attributes={"source_url": prospect.source_url},
    )

    provenance = ensure_intake_provenance(
        intake=intake,
        candidate=candidate,
        raw_payload=intake.raw_payload,
    )

    assert provenance.collection_method == "csv_import"
    assert provenance.source_url == "https://example.com/source"
    assert provenance.country_code == "US"
    assert provenance.lawful_basis == SDRLawfulBasis.UNASSESSED
    assert set(provenance.allowed_channels) == {
        "email",
        "whatsapp",
        "linkedin",
        "phone",
        "wechat",
    }
    assert SDRComplianceEvent.objects.filter(
        org=org_a, event_type="provenance_recorded"
    ).exists()


@pytest.mark.django_db
def test_enforcement_country_rule_and_dnc_are_fail_closed(org_a):
    _, intake, prospect = _outbound_record(org_a)
    provenance = ensure_intake_provenance(intake=intake)
    settings = SDRComplianceSettings.objects.get(org=org_a)
    settings.enforcement_enabled = True
    settings.save(update_fields=["enforcement_enabled", "updated_at"])

    unassessed = evaluate_contact(
        org_id=org_a.id,
        channel="email",
        identifier=prospect.email,
        prospect=prospect,
        event_key="test:unassessed",
    )
    assert unassessed.allowed is False
    assert unassessed.code == "lawful_basis_unassessed"

    provenance.lawful_basis = SDRLawfulBasis.LEGITIMATE_INTEREST
    provenance.lawful_basis_notes = "Documented necessity and balancing review LI-42."
    provenance.save()
    SDRChannelComplianceRule.objects.create(
        org=org_a,
        country_code="US",
        channel="whatsapp",
        is_allowed=False,
    )
    country_block = evaluate_contact(
        org_id=org_a.id,
        channel="whatsapp",
        identifier=prospect.phone,
        prospect=prospect,
    )
    assert country_block.code == "country_channel_blocked"

    entry, created = block_contact(
        org_id=org_a.id,
        channel="linkedin",
        identifier=prospect.email,
        reason=SDRDoNotContactReason.UNSUBSCRIBED,
        source=SDRDoNotContactSource.DATA_SUBJECT,
    )
    assert created is True
    dnc = evaluate_contact(
        org_id=org_a.id,
        channel="linkedin",
        identifier=prospect.email,
        prospect=prospect,
    )
    assert entry.is_active is True
    assert dnc.code == "do_not_contact"


@pytest.mark.django_db
def test_consent_rule_requires_timestamp_evidence_and_permitted_channel(org_a):
    _, intake, prospect = _outbound_record(org_a)
    provenance = ensure_intake_provenance(intake=intake)
    provenance.lawful_basis = SDRLawfulBasis.CONSENT
    provenance.allowed_channels = ["email"]
    provenance.save()
    SDRChannelComplianceRule.objects.create(
        org=org_a,
        country_code="US",
        channel="whatsapp",
        requires_consent=True,
    )

    decision = evaluate_contact(
        org_id=org_a.id,
        channel="whatsapp",
        identifier=prospect.phone,
        prospect=prospect,
    )

    assert decision.allowed is False
    assert decision.code == "consent_required"


@pytest.mark.django_db
def test_csv_import_preserves_reviewed_compliance_fields(org_a):
    campaign = SDROutboundCampaign.objects.create(
        org=org_a,
        name="Compliance CSV",
        channels=["email", "linkedin"],
    )
    result = import_prospect_csv(
        campaign=campaign,
        csv_text=(
            "company_name,email,country,lawful_basis,lawful_basis_notes,"
            "allowed_channels\n"
            "Factory Two,buyer@example.com,US,legitimate_interest,"
            "Review LI-42,email|linkedin"
        ),
    )

    prospect = SDROutboundProspect.objects.get(campaign=campaign)
    assert result["created"] == 1
    assert prospect.lawful_basis == SDRLawfulBasis.LEGITIMATE_INTEREST
    assert prospect.lawful_basis_notes == "Review LI-42"
    assert prospect.allowed_channels == ["email", "linkedin"]


@pytest.mark.django_db
def test_retention_anonymizes_sdr_data_but_preserves_crm_record(org_a):
    lead, intake, prospect = _outbound_record(org_a)
    whatsapp_connection = WhatsAppBusinessConnection.objects.create(
        org=org_a,
        route=WhatsAppPhoneRoute.objects.create(
            org=org_a,
            phone_number_id="retention-phone-route",
        ),
        access_token_ciphertext="encrypted-token",
        is_active=True,
    )
    whatsapp_message = WhatsAppMessage.objects.create(
        org=org_a,
        connection=whatsapp_connection,
        campaign=prospect.campaign,
        prospect=prospect,
        campaign_run=1,
        recipient=prospect.phone,
        template_name="approved_template",
        status=WhatsAppMessageStatus.QUEUED,
        provider_message_id="wamid.personal",
        provider_status_snapshot={"recipient_id": prospect.phone},
    )
    linkedin_connection = LinkedInConnection.objects.create(
        org=org_a,
        access_token_ciphertext="encrypted-token",
        partner_access_confirmed=True,
        is_active=True,
    )
    linkedin_invitation = LinkedInInvitation.objects.create(
        org=org_a,
        connection=linkedin_connection,
        campaign=prospect.campaign,
        prospect=prospect,
        campaign_run=1,
        recipient=prospect.email,
        message_body="Personalized invitation",
        status=LinkedInInvitationStatus.FAILED,
        provider_invitation_id="urn:li:invitation:personal",
        provider_status_snapshot={"recipient": prospect.email},
    )
    old = timezone.now() - timedelta(days=60)
    LeadIntake.objects.filter(id=intake.id).update(created_at=old)
    intake.refresh_from_db()
    settings = SDRComplianceSettings.objects.create(
        org=org_a,
        retention_mode=SDRRetentionMode.ANONYMIZE_SDR,
        retention_days=30,
        deletion_grace_days=0,
    )
    provenance = ensure_intake_provenance(intake=intake)
    provenance.retention_until = old + timedelta(days=30)
    provenance.save(update_fields=["retention_until", "updated_at"])

    preview = scan_retention(org_id=org_a.id, execute=False)
    assert preview["due"] == 1
    assert preview["anonymized"] == 0
    provenance.refresh_from_db()
    assert provenance.status == SDRProvenanceStatus.RETENTION_DUE

    executed = scan_retention(org_id=org_a.id, execute=True)
    assert executed["anonymized"] == 1
    assert executed["crm_records_changed"] == 0
    intake.refresh_from_db()
    prospect.refresh_from_db()
    lead.refresh_from_db()
    whatsapp_message.refresh_from_db()
    linkedin_invitation.refresh_from_db()
    assert intake.raw_payload == {}
    assert prospect.email == ""
    assert prospect.company_name == "Anonymized"
    assert lead.email == "one@example.com"
    assert lead.first_name == "Ada"
    assert whatsapp_message.status == WhatsAppMessageStatus.SKIPPED
    assert whatsapp_message.recipient.startswith("redacted:")
    assert whatsapp_message.provider_message_id == ""
    assert whatsapp_message.provider_status_snapshot == {}
    assert linkedin_invitation.status == LinkedInInvitationStatus.SKIPPED
    assert linkedin_invitation.recipient.endswith("@invalid.local")
    assert linkedin_invitation.message_body == ""
    assert linkedin_invitation.provider_invitation_id == ""
    assert linkedin_invitation.provider_status_snapshot == {}
    settings.refresh_from_db()
    assert settings.last_retention_scan_at is not None


@pytest.mark.django_db
def test_deletion_request_is_audited_and_waits_for_grace_period(org_a):
    _, intake, _ = _outbound_record(org_a)
    SDRComplianceSettings.objects.create(
        org=org_a,
        retention_mode=SDRRetentionMode.DISABLED,
        deletion_grace_days=30,
    )

    provenance = request_intake_deletion(intake)
    result = scan_retention(org_id=org_a.id, execute=True)

    assert provenance.status == SDRProvenanceStatus.DELETION_REQUESTED
    assert result["due"] == 0
    assert SDRComplianceEvent.objects.filter(
        org=org_a, event_type="deletion_requested"
    ).exists()


@pytest.mark.django_db
def test_deletion_lifecycle_blocks_contact_with_enforcement_disabled(org_a):
    _, intake, prospect = _outbound_record(org_a, suffix="deletion-block")
    ensure_intake_provenance(intake=intake)

    provenance = request_intake_deletion(intake)

    assert SDRComplianceSettings.objects.get(org=org_a).enforcement_enabled is False
    for channel, identifier in (
        ("email", prospect.email),
        ("whatsapp", prospect.phone),
        ("linkedin", prospect.linkedin_url),
    ):
        decision = evaluate_contact(
            org_id=org_a.id,
            channel=channel,
            identifier=identifier,
            prospect=prospect,
            event_key=f"test:deletion-requested:{channel}",
        )
        assert decision.allowed is False
        assert decision.code == "data_deletion_requested"

    original_email = prospect.email
    provenance = anonymize_intake(intake)
    decision = evaluate_contact(
        org_id=org_a.id,
        channel="email",
        identifier=original_email,
        intake=intake,
        event_key="test:data-anonymized:email",
    )
    assert provenance.status == SDRProvenanceStatus.ANONYMIZED
    assert decision.allowed is False
    assert decision.code == "data_anonymized"


@pytest.mark.django_db
def test_wechat_identifier_is_normalized_and_dnc_is_enforced(org_a):
    entry, created = block_contact(
        org_id=org_a.id,
        channel="wechat",
        identifier="  Ada_Lovelace-7  ",
        reason=SDRDoNotContactReason.DATA_REQUEST,
        source=SDRDoNotContactSource.DATA_SUBJECT,
    )

    decision = evaluate_contact(
        org_id=org_a.id,
        channel="wechat",
        identifier="ADA_LOVELACE-7",
        event_key="wechat:dnc:test",
    )

    assert created is True
    assert entry.identifier == "ada_lovelace-7"
    assert decision.allowed is False
    assert decision.code == "do_not_contact"
    event = SDRComplianceEvent.objects.get(
        event_key="contact:wechat:dnc:test:do_not_contact"
    )
    assert "identifier" not in event.snapshot
    assert event.snapshot["identifier_hash"] == entry.identifier_hash


@pytest.mark.django_db
def test_portable_governance_context_is_fail_closed_and_supports_consent(org_a):
    SDRComplianceSettings.objects.create(
        org=org_a,
        enforcement_enabled=True,
        require_lawful_basis=True,
    )
    SDRChannelComplianceRule.objects.create(
        org=org_a,
        country_code="CN",
        channel="wechat",
        requires_consent=True,
    )

    restricted = evaluate_contact(
        org_id=org_a.id,
        channel="wechat",
        identifier="not-valid",
        governance={
            "lawful_basis": "consent",
            "notes": "",
            "consent": {},
            "country": "CN",
            "allowed_channels": ["wechat"],
            "processing_status": "restricted",
        },
        event_key="governance:restricted",
    )
    allowed = evaluate_contact(
        org_id=org_a.id,
        channel="wechat",
        identifier="ada_lovelace-7",
        governance={
            "lawful_basis": "consent",
            "notes": "",
            "consent": {
                "granted": True,
                "recorded_at": timezone.now().isoformat(),
                "evidence_reference": "consent-receipt-42",
            },
            "country": "CN",
            "allowed_channels": ["wechat"],
            "processing_status": "active",
        },
    )

    assert restricted.code == "data_processing_restricted"
    assert allowed.allowed is True


@pytest.mark.django_db
def test_compliance_events_are_append_only_in_application_code(org_a):
    event = SDRComplianceEvent.objects.create(
        org=org_a,
        event_type="contact_blocked",
        event_key="append-only:test",
        reason="Original fact",
    )

    event.reason = "Mutated fact"
    with pytest.raises(ValidationError, match="cannot be updated"):
        event.save()
    with pytest.raises(ValidationError, match="cannot be updated"):
        SDRComplianceEvent.objects.filter(id=event.id).update(reason="Mutated")
    with pytest.raises(ValidationError, match="cannot be deleted"):
        event.delete()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_compliance_api_is_admin_and_tenant_scoped(admin_client, org_a, org_b):
    _, intake_a, _ = _outbound_record(org_a, suffix="tenant-a")
    _, intake_b, _ = _outbound_record(org_b, suffix="tenant-b")
    ensure_intake_provenance(intake=intake_a)
    ensure_intake_provenance(intake=intake_b)

    overview = admin_client.get("/api/sdr/compliance/")
    provenance = admin_client.get("/api/sdr/compliance/provenance/")
    created = admin_client.post(
        "/api/sdr/compliance/dnc/",
        {
            "channel": "email",
            "identifier": "blocked@example.com",
            "reason": "admin",
        },
        format="json",
    )

    assert overview.status_code == 200, overview.json()
    assert provenance.status_code == 200, provenance.json()
    assert provenance.json()["count"] == 1
    assert provenance.json()["results"][0]["intake_id"] == str(intake_a.id)
    assert created.status_code == 201, created.json()
    assert created.json()["identifier"] == "blocked@example.com"
