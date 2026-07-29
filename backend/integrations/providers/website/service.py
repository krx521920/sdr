"""Website provider orchestration into the shared SDR intake service."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from integrations.providers.website.adapter import WebsiteFormNormalizer
from sdr.services import LeadIntakeResult, process_candidate_intake


def process_website_intake(
    *, org_id: UUID, payload: Mapping[str, Any]
) -> LeadIntakeResult:
    candidate = WebsiteFormNormalizer().normalize(org_id=org_id, payload=payload)
    return process_candidate_intake(candidate=candidate, raw_payload=payload)
