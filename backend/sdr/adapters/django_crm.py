"""Django ORM adapters for the existing BottleCRM record system."""

from django.db import transaction
from django.db.models import Count, Q

from common.models import Profile
from contacts.models import Contact
from leads.models import Lead
from sdr.domain import (
    AssignmentDecision,
    CRMWriteResult,
    HandoffPackage,
    LeadCandidate,
    QualificationBand,
    QualificationResult,
)


class DjangoLeadDeduplicator:
    def find_existing(self, candidate: LeadCandidate):
        identity_query = Q()
        if candidate.identity.email:
            identity_query |= Q(email__iexact=candidate.identity.email)
        if candidate.identity.phone:
            identity_query |= Q(phone=candidate.identity.phone)
        if not identity_query:
            return None

        lead = (
            Lead.objects.filter(org_id=candidate.org_id, is_active=True)
            .filter(identity_query)
            .order_by("-updated_at")
            .first()
        )
        return lead.id if lead else None


class LeastLoadedSalesRouter:
    def route(
        self, candidate: LeadCandidate, qualification: QualificationResult
    ) -> AssignmentDecision:
        profiles = Profile.objects.filter(
            org_id=candidate.org_id,
            is_active=True,
            has_sales_access=True,
        )
        if not profiles.exists():
            profiles = Profile.objects.filter(
                org_id=candidate.org_id,
                is_active=True,
                role="ADMIN",
            )

        profile = (
            profiles.annotate(
                active_lead_count=Count(
                    "lead_assigned_users",
                    filter=Q(lead_assigned_users__is_active=True),
                    distinct=True,
                )
            )
            .order_by("active_lead_count", "created_at", "id")
            .first()
        )
        if not profile:
            return AssignmentDecision(reason="no active sales profile available")

        return AssignmentDecision(
            profile_id=profile.id,
            reason=(
                f"least-loaded sales profile; qualification={qualification.band.value}"
            ),
        )


class DjangoCRMWriter:
    @transaction.atomic
    def write_handoff(self, package: HandoffPackage) -> CRMWriteResult:
        candidate = package.candidate
        lead = None
        created = package.existing_lead_id is None
        if package.existing_lead_id:
            lead = (
                Lead.objects.select_for_update()
                .filter(id=package.existing_lead_id, org_id=candidate.org_id)
                .first()
            )
            created = lead is None

        if lead is None:
            lead = Lead(org_id=candidate.org_id, status="assigned", source="other")

        self._apply_candidate(lead, package)
        lead.save()

        profile = self._assignment_profile(package)
        if profile:
            lead.assigned_to.add(profile)

        contact = self._upsert_contact(package, profile)
        if contact:
            lead.contacts.add(contact)

        return CRMWriteResult(
            lead_id=lead.id,
            created=created,
            contact_id=contact.id if contact else None,
        )

    @staticmethod
    def _apply_candidate(lead: Lead, package: HandoffPackage) -> None:
        candidate = package.candidate
        identity = candidate.identity
        company = candidate.company

        values = {
            "title": company.name
            or " ".join(filter(None, [identity.first_name, identity.last_name]))
            or identity.email
            or identity.phone
            or "Inbound lead",
            "first_name": identity.first_name,
            "last_name": identity.last_name,
            "email": identity.email,
            "phone": identity.phone,
            "job_title": candidate.attributes.get("job_title"),
            "website": company.website,
            "linkedin_url": identity.linkedin_url,
            "industry": company.industry,
            "country": company.country.upper()[:3] if company.country else None,
            "company_name": company.name,
            "description": candidate.attributes.get("message"),
            "rating": DjangoCRMWriter._rating(package.qualification.band),
            "is_active": True,
        }
        for field_name, value in values.items():
            if value is not None and (
                not getattr(lead, field_name, None) or created_field(lead, field_name)
            ):
                setattr(lead, field_name, value)

        custom_fields = dict(lead.custom_fields or {})
        custom_fields["sdr"] = {
            "source": candidate.source.value,
            "source_record_id": candidate.source_record_id,
            "qualification_score": package.qualification.score,
            "qualification_band": package.qualification.band.value,
            "qualification_reasons": list(package.qualification.reasons),
            "model_version": package.qualification.model_version,
            "metadata": dict(package.qualification.metadata),
            "attributes": dict(candidate.attributes),
        }
        lead.custom_fields = custom_fields

    @staticmethod
    def _rating(band: QualificationBand) -> str:
        return {
            QualificationBand.HIGH: "HOT",
            QualificationBand.MEDIUM: "WARM",
            QualificationBand.LOW: "COLD",
            QualificationBand.DISQUALIFIED: "COLD",
        }[band]

    @staticmethod
    def _assignment_profile(package: HandoffPackage):
        profile_id = package.assignment.profile_id
        if not profile_id:
            return None
        return Profile.objects.filter(
            id=profile_id,
            org_id=package.candidate.org_id,
            is_active=True,
        ).first()

    @staticmethod
    def _upsert_contact(package: HandoffPackage, profile):
        candidate = package.candidate
        identity = candidate.identity
        identity_query = Q()
        if identity.email:
            identity_query |= Q(email__iexact=identity.email)
        if identity.phone:
            identity_query |= Q(phone=identity.phone)
        if not identity_query:
            return None

        contact = (
            Contact.objects.filter(org_id=candidate.org_id)
            .filter(identity_query)
            .order_by("-updated_at")
            .first()
        )
        if contact is None:
            contact = Contact(org_id=candidate.org_id)

        contact.first_name = identity.first_name or contact.first_name or ""
        contact.last_name = identity.last_name or contact.last_name or ""
        contact.email = identity.email or contact.email
        contact.phone = identity.phone or contact.phone
        contact.linkedin_url = identity.linkedin_url or contact.linkedin_url
        contact.organization = candidate.company.name or contact.organization
        contact.title = candidate.attributes.get("job_title") or contact.title
        contact.description = candidate.attributes.get("message") or contact.description
        contact.is_active = True
        contact.save()
        if profile:
            contact.assigned_to.add(profile)
        return contact


def created_field(instance, field_name: str) -> bool:
    """Allow fresh model defaults to be replaced without clearing existing data."""

    return instance._state.adding and field_name in {"title", "rating", "is_active"}
