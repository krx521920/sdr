"""Role-based capabilities for the matching bounded context."""

from rest_framework import permissions

from common.models import MatchingAccessLevel

MATCHING_ACCESS_RANK = {
    MatchingAccessLevel.NONE: 0,
    MatchingAccessLevel.READ: 1,
    MatchingAccessLevel.MANAGE: 2,
    MatchingAccessLevel.RECOMPUTE: 3,
    MatchingAccessLevel.DECIDE: 4,
}


def is_org_admin(profile) -> bool:
    return bool(
        profile
        and (
            getattr(profile, "role", None) == "ADMIN"
            or getattr(profile, "is_organization_admin", False)
        )
    )


def effective_matching_access_level(profile) -> str:
    if is_org_admin(profile):
        return MatchingAccessLevel.DECIDE
    level = getattr(profile, "matching_access_level", MatchingAccessLevel.NONE)
    if level not in MATCHING_ACCESS_RANK:
        return MatchingAccessLevel.NONE
    return level


def has_matching_access(profile, required_level) -> bool:
    current = effective_matching_access_level(profile)
    required_rank = MATCHING_ACCESS_RANK.get(required_level)
    if required_rank is None:
        return False
    return MATCHING_ACCESS_RANK[current] >= required_rank


def matching_capabilities(profile) -> dict:
    admin = is_org_admin(profile)
    decide = has_matching_access(profile, MatchingAccessLevel.DECIDE)
    return {
        "read": has_matching_access(profile, MatchingAccessLevel.READ),
        "manage": has_matching_access(profile, MatchingAccessLevel.MANAGE),
        "recompute": has_matching_access(profile, MatchingAccessLevel.RECOMPUTE),
        "decide": decide,
        "feedback": decide,
        "calibrate": admin,
        "export": admin,
        "delete": admin,
        "retention": admin,
    }


class HasMatchingAccess(permissions.BasePermission):
    """Enforce each matching view's explicit method-to-level contract."""

    message = "Your matching access level does not allow this action."

    def has_permission(self, request, view):
        required_by_method = getattr(view, "matching_access_by_method", None)
        if not isinstance(required_by_method, dict):
            return False
        required_level = required_by_method.get(request.method.upper())
        if required_level is None:
            return False
        return has_matching_access(getattr(request, "profile", None), required_level)
