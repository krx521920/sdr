from contextlib import nullcontext
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from matching.tasks import (
    expire_all_stale_import_previews,
    scan_all_governance_retention,
)


@pytest.mark.django_db
def test_periodic_governance_scan_isolates_org_failures(
    org_a,
    org_b,
    monkeypatch,
):
    seen = []

    def fake_scan(*, org, execute, limit, actor):
        seen.append(org.id)
        assert execute is True
        assert limit == 500
        assert actor is None
        if org.id == org_a.id:
            raise RuntimeError("isolated failure")
        return {
            "due": 2,
            "restricted": 1,
            "anonymized": 1,
            "expired": 0,
            "recomputed": 3,
        }

    monkeypatch.setattr(
        "matching.tasks.database_org_context",
        lambda _org_id: nullcontext(),
    )
    monkeypatch.setattr("matching.tasks.scan_governance_retention", fake_scan)

    result = scan_all_governance_retention.run()

    assert set(seen) == {org_a.id, org_b.id}
    assert result == {
        "due": 2,
        "restricted": 1,
        "anonymized": 1,
        "expired": 0,
        "recomputed": 3,
        "failed_orgs": 1,
    }


@pytest.mark.django_db
@override_settings(MATCHING_IMPORT_PREVIEW_RETENTION_DAYS=7)
def test_periodic_preview_expiry_is_bounded_and_isolates_org_failures(
    org_a,
    org_b,
    monkeypatch,
):
    seen = []
    started_at = timezone.now()

    def fake_expire(*, org, older_than, limit):
        seen.append(org.id)
        assert limit == 500
        assert started_at - timedelta(days=7, seconds=1) <= older_than
        assert older_than <= timezone.now() - timedelta(days=7)
        if org.id == org_a.id:
            raise RuntimeError("isolated failure")
        return {"expired_count": 3, "batch_ids": ["a", "b", "c"]}

    monkeypatch.setattr(
        "matching.tasks.database_org_context",
        lambda _org_id: nullcontext(),
    )
    monkeypatch.setattr(
        "matching.tasks.expire_stale_import_previews",
        fake_expire,
    )

    result = expire_all_stale_import_previews.run()

    assert set(seen) == {org_a.id, org_b.id}
    assert result == {"expired_count": 3, "failed_orgs": 1}
