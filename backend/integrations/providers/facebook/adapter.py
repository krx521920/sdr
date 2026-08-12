"""Validate Meta webhooks and normalize fetched Lead Ads records."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sdr.domain import CompanySnapshot, LeadCandidate, LeadIdentity, LeadSource


class FacebookWebhookError(ValueError):
    """The webhook body is not a supported Meta Page notification."""


@dataclass(frozen=True, slots=True)
class FacebookLeadEvent:
    page_id: str
    leadgen_id: str
    form_id: str | None = None
    ad_id: str | None = None
    adgroup_id: str | None = None
    created_time: int | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "page_id": self.page_id,
                "leadgen_id": self.leadgen_id,
                "form_id": self.form_id,
                "ad_id": self.ad_id,
                "adgroup_id": self.adgroup_id,
                "created_time": self.created_time,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class FacebookMessageEvent:
    page_id: str
    sender_psid: str
    message_id: str
    body: str
    attachment_types: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_payload(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "sender_psid": self.sender_psid,
            "message_id": self.message_id,
            "body": self.body,
            "attachment_types": list(self.attachment_types),
            "occurred_at": self.occurred_at.isoformat(),
        }


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _first(values: Mapping[str, list[str]], *names: str) -> str | None:
    for name in names:
        candidates = values.get(name, [])
        if candidates:
            return _clean(candidates[0])
    return None


class FacebookLeadAdsAdapter:
    provider = LeadSource.FACEBOOK_AD

    def __init__(self, *, app_secret: str):
        self.app_secret = app_secret

    def verify_signature(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        signature = next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "x-hub-signature-256"
            ),
            "",
        )
        if not self.app_secret or not signature.startswith("sha256="):
            return False
        digest = hmac.new(
            self.app_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, f"sha256={digest}")

    def parse_events(self, *, body: bytes) -> tuple[FacebookLeadEvent, ...]:
        entries = self._entries(body)

        events: list[FacebookLeadEvent] = []
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise FacebookWebhookError("webhook entry items must be objects")
            entry_page_id = _clean(entry.get("id"))
            changes = entry.get("changes", [])
            if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
                raise FacebookWebhookError("webhook changes must be a list")
            for change in changes:
                if not isinstance(change, Mapping) or change.get("field") != "leadgen":
                    continue
                value = change.get("value")
                if not isinstance(value, Mapping):
                    raise FacebookWebhookError("leadgen value must be an object")
                page_id = _clean(value.get("page_id")) or entry_page_id
                leadgen_id = _clean(value.get("leadgen_id"))
                if not page_id or not leadgen_id:
                    raise FacebookWebhookError(
                        "leadgen event requires page_id and leadgen_id"
                    )
                if entry_page_id and page_id != entry_page_id:
                    raise FacebookWebhookError(
                        "entry Page id does not match leadgen Page id"
                    )
                identity = (page_id, leadgen_id)
                if identity in seen:
                    continue
                seen.add(identity)
                created_time = value.get("created_time")
                try:
                    created_timestamp = (
                        int(created_time) if created_time is not None else None
                    )
                except (TypeError, ValueError) as exc:
                    raise FacebookWebhookError(
                        "leadgen created_time must be a Unix timestamp"
                    ) from exc
                events.append(
                    FacebookLeadEvent(
                        page_id=page_id,
                        leadgen_id=leadgen_id,
                        form_id=_clean(value.get("form_id")),
                        ad_id=_clean(value.get("ad_id")),
                        adgroup_id=_clean(value.get("adgroup_id")),
                        created_time=created_timestamp,
                    )
                )
        return tuple(events)

    def parse_message_events(self, *, body: bytes) -> tuple[FacebookMessageEvent, ...]:
        entries = self._entries(body)
        events: list[FacebookMessageEvent] = []
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise FacebookWebhookError("webhook entry items must be objects")
            page_id = _clean(entry.get("id"))
            messaging = entry.get("messaging", [])
            if not isinstance(messaging, Sequence) or isinstance(
                messaging, (str, bytes)
            ):
                raise FacebookWebhookError("webhook messaging must be a list")
            for item in messaging:
                if not isinstance(item, Mapping):
                    raise FacebookWebhookError("messaging items must be objects")
                message = item.get("message")
                if not isinstance(message, Mapping) or message.get("is_echo"):
                    continue
                recipient = item.get("recipient", {})
                sender = item.get("sender", {})
                recipient_page_id = (
                    _clean(recipient.get("id"))
                    if isinstance(recipient, Mapping)
                    else None
                )
                sender_psid = (
                    _clean(sender.get("id")) if isinstance(sender, Mapping) else None
                )
                resolved_page_id = page_id or recipient_page_id
                message_id = _clean(message.get("mid"))
                if not resolved_page_id or not sender_psid or not message_id:
                    raise FacebookWebhookError(
                        "Messenger event requires Page, sender, and message ids"
                    )
                if recipient_page_id and recipient_page_id != resolved_page_id:
                    raise FacebookWebhookError(
                        "entry Page id does not match Messenger recipient id"
                    )
                if sender_psid == resolved_page_id:
                    continue
                identity = (resolved_page_id, message_id)
                if identity in seen:
                    continue
                seen.add(identity)

                attachment_types = self._attachment_types(message.get("attachments"))
                text = _clean(message.get("text")) or ""
                if not text and attachment_types:
                    text = f"[Messenger attachment: {', '.join(attachment_types)}]"
                if not text:
                    continue
                events.append(
                    FacebookMessageEvent(
                        page_id=resolved_page_id,
                        sender_psid=sender_psid,
                        message_id=message_id,
                        body=text[:10000],
                        attachment_types=attachment_types,
                        occurred_at=self._message_occurred_at(
                            item.get("timestamp"),
                            entry.get("time"),
                        ),
                    )
                )
        return tuple(events)

    def normalize(
        self,
        *,
        org_id: UUID,
        event: FacebookLeadEvent,
        lead: Mapping[str, Any],
    ) -> LeadCandidate:
        fields = self._field_values(lead.get("field_data", []))
        first_name = _first(fields, "first_name")
        last_name = _first(fields, "last_name")
        full_name = _first(fields, "full_name")
        if full_name and not first_name and not last_name:
            first_name, separator, last_name = full_name.strip().partition(" ")
            last_name = last_name if separator else None

        email = _first(fields, "email", "work_email")
        received_at = self._received_at(lead.get("created_time"), event.created_time)
        known_fields = {
            "company_name",
            "company",
            "country",
            "email",
            "first_name",
            "full_name",
            "job_title",
            "last_name",
            "linkedin_url",
            "message",
            "phone",
            "phone_number",
            "website",
            "work_email",
        }
        custom_answers = {
            key: value for key, value in fields.items() if key not in known_fields
        }
        attributes = {
            key: value
            for key, value in {
                "job_title": _first(fields, "job_title"),
                "message": _first(fields, "message"),
                "facebook_page_id": event.page_id,
                "facebook_form_id": event.form_id or _clean(lead.get("form_id")),
                "facebook_ad_id": event.ad_id or _clean(lead.get("ad_id")),
                "facebook_adgroup_id": event.adgroup_id,
                "facebook_platform": _clean(lead.get("platform")),
                "facebook_is_organic": lead.get("is_organic"),
                "facebook_answers": custom_answers or None,
            }.items()
            if value is not None
        }
        return LeadCandidate(
            org_id=org_id,
            source=LeadSource.FACEBOOK_AD,
            source_record_id=str(lead.get("id") or event.leadgen_id),
            identity=LeadIdentity(
                first_name=first_name,
                last_name=last_name,
                email=email.lower() if email else None,
                phone=_first(fields, "phone_number", "phone"),
                linkedin_url=_first(fields, "linkedin_url"),
            ),
            company=CompanySnapshot(
                name=_first(fields, "company_name", "company"),
                website=_first(fields, "website"),
                country=_first(fields, "country"),
            ),
            attributes=attributes,
            received_at=received_at,
        )

    @staticmethod
    def _field_values(field_data: Any) -> dict[str, list[str]]:
        if not isinstance(field_data, Sequence) or isinstance(field_data, (str, bytes)):
            raise FacebookWebhookError("lead field_data must be a list")
        fields: dict[str, list[str]] = {}
        for item in field_data:
            if not isinstance(item, Mapping):
                continue
            name = _clean(item.get("name"))
            values = item.get("values", [])
            if (
                not name
                or not isinstance(values, Sequence)
                or isinstance(values, (str, bytes))
            ):
                continue
            fields[name.lower()] = [
                cleaned for value in values if (cleaned := _clean(value)) is not None
            ]
        return fields

    @staticmethod
    def _received_at(value: Any, fallback_timestamp: int | None) -> datetime:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                    UTC
                )
            except ValueError:
                pass
        if fallback_timestamp is not None:
            return datetime.fromtimestamp(fallback_timestamp, tz=UTC)
        return datetime.now(UTC)

    @staticmethod
    def _entries(body: bytes) -> Sequence[Any]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FacebookWebhookError("webhook body must be valid JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("object") != "page":
            raise FacebookWebhookError("webhook object must be 'page'")
        entries = payload.get("entry", [])
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise FacebookWebhookError("webhook entry must be a list")
        return entries

    @staticmethod
    def _attachment_types(value: Any) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return ()
        return tuple(
            dict.fromkeys(
                cleaned[:32]
                for item in value
                if isinstance(item, Mapping)
                and (cleaned := _clean(item.get("type"))) is not None
            )
        )

    @staticmethod
    def _message_occurred_at(value: Any, entry_value: Any) -> datetime:
        for candidate in (value, entry_value):
            try:
                timestamp = int(candidate)
            except (TypeError, ValueError):
                continue
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue
        return datetime.now(UTC)
