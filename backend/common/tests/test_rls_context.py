"""Regression tests for organization-context middleware exemptions."""

import pytest

from common.middleware.rls_context import RequireOrgContext


@pytest.fixture
def middleware():
    return RequireOrgContext(lambda request: None)


@pytest.mark.parametrize(
    "path",
    [
        "/readyz/",
        "/api/sdr/public/ses-feedback/",
        "/api/sdr/public/nurture/open/signed-token/pixel.gif",
        "/api/sdr/public/nurture/click/signed-token/",
        "/api/sdr/public/nurture/unsubscribe/signed-token/",
        "/api/cases/inbound/12345678-1234-4234-8234-123456789abc/",
    ],
)
def test_signed_public_sdr_routes_are_org_exempt(middleware, path):
    assert middleware._is_exempt(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/readyz/extra/",
        "/api/sdr/public/",
        "/api/sdr/public/ses-feedback/extra/",
        "/api/sdr/public/nurture/",
        "/api/sdr/public/nurture/open-adjacent/signed-token/",
        "/api/sdr/nurture/sequences/",
        "/api/cases/inbound/",
        "/api/cases/inbound/not-a-uuid/",
        "/api/cases/inbound/12345678-1234-4234-8234-123456789abc/extra/",
        "/api/cases/inbound-adjacent/12345678-1234-4234-8234-123456789abc/",
    ],
)
def test_other_sdr_routes_still_require_org_context(middleware, path):
    assert middleware._is_exempt(path) is False
