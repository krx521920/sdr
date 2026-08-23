"""Validation and personalization for SDR-owned LinkedIn invitation copy."""

from __future__ import annotations

import re

from sdr.models import SDROutboundProspect

VARIABLE_PATTERN = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*}}", re.IGNORECASE)
ALLOWED_VARIABLES = frozenset(
    {"first_name", "last_name", "full_name", "company_name", "job_title"}
)


class LinkedInInvitationTemplateError(ValueError):
    pass


def validate_invitation_template(value: str) -> str:
    cleaned = value.strip()
    unknown = sorted(
        {match.group(1).lower() for match in VARIABLE_PATTERN.finditer(cleaned)}
        - ALLOWED_VARIABLES
    )
    if unknown:
        raise LinkedInInvitationTemplateError(
            f"Unsupported LinkedIn invitation variables: {', '.join(unknown)}."
        )
    remaining = VARIABLE_PATTERN.sub("", cleaned)
    if "{{" in remaining or "}}" in remaining:
        raise LinkedInInvitationTemplateError(
            "LinkedIn invitation variables must use {{ variable_name }} syntax."
        )
    return cleaned


def render_invitation_message(
    template: str,
    prospect: SDROutboundProspect,
) -> str:
    cleaned = validate_invitation_template(template)
    values = {
        "first_name": prospect.first_name,
        "last_name": prospect.last_name,
        "full_name": " ".join(
            value for value in (prospect.first_name, prospect.last_name) if value
        ),
        "company_name": prospect.company_name,
        "job_title": prospect.job_title,
    }
    rendered = VARIABLE_PATTERN.sub(
        lambda match: values[match.group(1).lower()].strip(),
        cleaned,
    ).strip()
    if len(rendered) > 300:
        raise LinkedInInvitationTemplateError(
            "The personalized LinkedIn invitation message exceeds 300 characters."
        )
    return rendered
