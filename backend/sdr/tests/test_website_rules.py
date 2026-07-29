from uuid import uuid4

from integrations.providers.website.adapter import WebsiteFormNormalizer
from sdr.enrichment import EmailDomainEnricher
from sdr.scoring import RuleBasedLeadScorer


def test_website_form_is_normalized_enriched_and_scored():
    candidate = WebsiteFormNormalizer().normalize(
        org_id=uuid4(),
        payload={
            "source_record_id": "website-42",
            "first_name": " Ada ",
            "email": "ADA@EXAMPLE.COM",
            "phone": "+1-555-0100",
            "company_name": "Example Inc",
            "job_title": "VP Sales",
            "message": "We need an automated qualification workflow.",
        },
    )

    enriched = EmailDomainEnricher().enrich(candidate)
    result = RuleBasedLeadScorer().score(enriched)

    assert enriched.identity.email == "ada@example.com"
    assert enriched.company.website == "https://example.com"
    assert enriched.attributes["is_business_email"] is True
    assert result.score == 90
    assert result.band.value == "high"
