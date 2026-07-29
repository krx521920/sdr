"""Tenant-aware SDR routing rules with a safe least-loaded fallback."""

from django.db import transaction
from django.db.models import Count, Q

from common.models import Profile
from common.utils import COUNTRIES
from sdr.adapters.django_crm import LeastLoadedSalesRouter
from sdr.domain import AssignmentDecision, LeadCandidate, QualificationResult
from sdr.models import (
    SDRRoutingRule,
    SDRRoutingRuleState,
    SDRRoutingStrategy,
)

COUNTRY_CODE_BY_KEY = {
    str(value).strip().casefold(): code for code, value in COUNTRIES
} | {code.casefold(): code for code, _ in COUNTRIES}
COUNTRY_CODE_BY_KEY.update(
    {
        "uk": "GB",
        "usa": "US",
        "united states of america": "US",
        "中国": "CN",
        "英国": "GB",
        "美国": "US",
    }
)


def normalize_country(value: str | None) -> str:
    """Return an ISO alpha-2 code when the configured country is recognized."""

    key = (value or "").strip().casefold()
    return COUNTRY_CODE_BY_KEY.get(key, (value or "").strip().upper())


class RuleBasedSalesRouter:
    """Apply the first usable matching rule, then retain legacy fallback behavior."""

    def __init__(self, *, fallback=None):
        self.fallback = fallback or LeastLoadedSalesRouter()

    def route(
        self, candidate: LeadCandidate, qualification: QualificationResult
    ) -> AssignmentDecision:
        return self._route(candidate, qualification, advance_round_robin=True)

    def preview(
        self, candidate: LeadCandidate, qualification: QualificationResult
    ) -> AssignmentDecision:
        """Evaluate the current rules without advancing round-robin state."""

        return self._route(candidate, qualification, advance_round_robin=False)

    def _route(
        self,
        candidate: LeadCandidate,
        qualification: QualificationResult,
        *,
        advance_round_robin: bool,
    ) -> AssignmentDecision:
        rules = SDRRoutingRule.objects.filter(
            org_id=candidate.org_id,
            is_active=True,
        ).prefetch_related("members__profile")

        for rule in rules:
            if not self._matches(rule, candidate, qualification):
                continue
            profiles = self._eligible_profiles(rule, candidate.org_id)
            if not profiles:
                continue
            profile = self._choose_profile(
                rule,
                profiles,
                advance_round_robin=advance_round_robin,
            )
            return AssignmentDecision(
                profile_id=profile.id,
                rule_id=rule.id,
                reason=(
                    f'rule="{rule.name}"; strategy={rule.strategy}; '
                    f"country={normalize_country(candidate.company.country) or 'unknown'}; "
                    f"source={candidate.source.value}; qualification={qualification.band.value}"
                ),
            )

        decision = self.fallback.route(candidate, qualification)
        return AssignmentDecision(
            profile_id=decision.profile_id,
            team_id=decision.team_id,
            reason=f"fallback: {decision.reason}",
        )

    @staticmethod
    def _matches(rule, candidate, qualification) -> bool:
        country = normalize_country(candidate.company.country)
        countries = {normalize_country(value) for value in rule.countries}
        if countries and country not in countries:
            return False
        if rule.sources and candidate.source.value not in rule.sources:
            return False
        return not (
            rule.qualification_bands
            and qualification.band.value not in rule.qualification_bands
        )

    @staticmethod
    def _eligible_profiles(rule, org_id):
        profile_ids = [
            member.profile_id
            for member in rule.members.all()
            if member.org_id == org_id and member.profile.org_id == org_id
        ]
        return list(
            Profile.objects.filter(
                id__in=profile_ids,
                org_id=org_id,
                is_active=True,
                has_sales_access=True,
            )
        )

    def _choose_profile(self, rule, profiles, *, advance_round_robin):
        profile_ids = {profile.id for profile in profiles}
        ordered_ids = [
            member.profile_id
            for member in rule.members.all()
            if member.profile_id in profile_ids
        ]
        if rule.strategy == SDRRoutingStrategy.DIRECT:
            return next(profile for profile in profiles if profile.id == ordered_ids[0])
        if rule.strategy == SDRRoutingStrategy.ROUND_ROBIN:
            return self._round_robin(
                rule,
                profiles,
                ordered_ids,
                advance=advance_round_robin,
            )
        return self._least_loaded(profiles, ordered_ids)

    @staticmethod
    def _least_loaded(profiles, ordered_ids):
        position = {profile_id: index for index, profile_id in enumerate(ordered_ids)}
        counts = {
            row["id"]: row["active_lead_count"]
            for row in Profile.objects.filter(id__in=position)
            .annotate(
                active_lead_count=Count(
                    "lead_assigned_users",
                    filter=Q(lead_assigned_users__is_active=True),
                    distinct=True,
                )
            )
            .values("id", "active_lead_count")
        }
        return min(
            profiles,
            key=lambda profile: (counts.get(profile.id, 0), position[profile.id]),
        )

    @staticmethod
    @transaction.atomic
    def _round_robin(rule, profiles, ordered_ids, *, advance):
        profile_by_id = {profile.id: profile for profile in profiles}
        if not advance:
            next_index = (
                SDRRoutingRuleState.objects.filter(rule=rule)
                .values_list("next_index", flat=True)
                .first()
                or 0
            )
            return profile_by_id[ordered_ids[next_index % len(ordered_ids)]]
        state, _ = SDRRoutingRuleState.objects.select_for_update().get_or_create(
            rule=rule,
            defaults={"org_id": rule.org_id},
        )
        index = state.next_index % len(ordered_ids)
        state.next_index += 1
        state.save(update_fields=["next_index", "updated_at"])
        return profile_by_id[ordered_ids[index]]
