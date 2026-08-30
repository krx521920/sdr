from importlib import import_module

import pytest

from common.models import MatchingAccessLevel, Profile, User


@pytest.mark.django_db
def test_matching_access_migration_backfill(org_a):
    admin = Profile.objects.create(
        user=User.objects.create_user(email="matching-admin@test.com"),
        org=org_a,
        role="ADMIN",
    )
    organization_admin = Profile.objects.create(
        user=User.objects.create_user(email="matching-org-admin@test.com"),
        org=org_a,
        role="USER",
        is_organization_admin=True,
    )
    sales = Profile.objects.create(
        user=User.objects.create_user(email="matching-sales@test.com"),
        org=org_a,
        role="USER",
        has_sales_access=True,
    )
    regular = Profile.objects.create(
        user=User.objects.create_user(email="matching-regular@test.com"),
        org=org_a,
        role="USER",
    )

    migration = import_module(
        "common.migrations.0030_profile_matching_access_level"
    )
    migration.backfill_matching_access(import_module("django.apps").apps, None)

    for profile in (admin, organization_admin, sales, regular):
        profile.refresh_from_db()
    assert admin.matching_access_level == MatchingAccessLevel.DECIDE
    assert organization_admin.matching_access_level == MatchingAccessLevel.DECIDE
    assert sales.matching_access_level == MatchingAccessLevel.READ
    assert regular.matching_access_level == MatchingAccessLevel.NONE
