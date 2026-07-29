"""Deterministic enrichment used before external research providers are added."""

from dataclasses import replace
from urllib.parse import urlparse

from sdr.domain import LeadCandidate

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "outlook.com",
    "qq.com",
    "yahoo.com",
}


class EmailDomainEnricher:
    def enrich(self, candidate: LeadCandidate) -> LeadCandidate:
        email = candidate.identity.email or ""
        domain = email.rpartition("@")[2].lower()
        attributes = dict(candidate.attributes)

        if domain:
            attributes["email_domain"] = domain
            attributes["is_business_email"] = domain not in FREE_EMAIL_DOMAINS

        company = candidate.company
        if not company.website and domain and domain not in FREE_EMAIL_DOMAINS:
            company = replace(company, website=f"https://{domain}")

        if company.website:
            parsed = urlparse(company.website)
            attributes["website_domain"] = (parsed.hostname or "").lower()

        return replace(candidate, company=company, attributes=attributes)
