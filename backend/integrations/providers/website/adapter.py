"""Normalize an authenticated website form into the SDR domain."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sdr.domain import CompanySnapshot, LeadCandidate, LeadIdentity, LeadSource


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


class WebsiteFormNormalizer:
    def normalize(self, *, org_id: UUID, payload: Mapping[str, Any]) -> LeadCandidate:
        email = _clean(payload.get("email"))
        attributes = {
            key: value
            for key, value in {
                "job_title": _clean(payload.get("job_title")),
                "message": _clean(payload.get("message")),
                "utm_source": _clean(payload.get("utm_source")),
                "utm_medium": _clean(payload.get("utm_medium")),
                "utm_campaign": _clean(payload.get("utm_campaign")),
                "page_url": _clean(payload.get("page_url")),
            }.items()
            if value is not None
        }
        return LeadCandidate(
            org_id=org_id,
            source=LeadSource.WEBSITE_FORM,
            source_record_id=str(payload["source_record_id"]),
            identity=LeadIdentity(
                first_name=_clean(payload.get("first_name")),
                last_name=_clean(payload.get("last_name")),
                email=email.lower() if email else None,
                phone=_clean(payload.get("phone")),
                linkedin_url=_clean(payload.get("linkedin_url")),
            ),
            company=CompanySnapshot(
                name=_clean(payload.get("company_name")),
                website=_clean(payload.get("website")),
                industry=_clean(payload.get("industry")),
                country=_clean(payload.get("country")),
            ),
            attributes=attributes,
        )
