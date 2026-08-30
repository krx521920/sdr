"""Lock ordering helpers for tenant-scoped matching projection writes."""

from common.models import Org


def lock_matching_org(org_id):
    """Serialize projection mutations inside one tenant transaction.

    Callers must enter ``transaction.atomic()`` before acquiring this lock and
    must take it before any matching Person, Opportunity, or Match row lock.
    """

    return Org.objects.select_for_update().only("id").get(id=org_id)
