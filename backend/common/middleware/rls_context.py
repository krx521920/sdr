"""
Database middleware for Row-Level Security (RLS) context.

This middleware sets the PostgreSQL session variable `app.current_org`
which is used by RLS policies to filter data at the database level.

Enable RLS policies after this middleware is in place:

    ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
    CREATE POLICY org_isolation ON leads
      USING (org_id = current_setting('app.current_org', true)::uuid);

Usage in settings.py:
    MIDDLEWARE = [
        ...
        'common.middleware.get_company.GetProfileAndOrg',
        'common.middleware.rls_context.SetOrgContext',  # After GetProfileAndOrg
        ...
    ]
"""

import logging
import re

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)

_INBOUND_EMAIL_WEBHOOK_PATH_RE = re.compile(
    r"^/api/cases/inbound/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/$"
)


class SetOrgContext:
    """
    Middleware to set PostgreSQL session variable for Row-Level Security.

    This sets `app.current_org` to the user's organization ID, which is
    used by RLS policies to automatically filter data at the database level.

    Security: This provides defense-in-depth. Even if application code
    forgets to filter by org, the database will enforce isolation.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Set org context before processing request
        self._set_org_context(request)

        response = self.get_response(request)

        # Reset context after request
        self._reset_org_context()

        return response

    def _set_org_context(self, request):
        """
        Set the PostgreSQL session variable for RLS.

        Args:
            request: Django request object with profile attached
        """
        if not hasattr(request, "org") or request.org is None:
            return

        org_id = str(request.org.id)

        try:
            with connection.cursor() as cursor:
                # Set the session variable (is_local=false for session scope)
                # Required because Django uses autocommit mode by default
                cursor.execute(
                    "SELECT set_config('app.current_org', %s, false)", [org_id]
                )
                logger.debug("Set RLS context: app.current_org = %s", org_id)

        except Exception as e:
            # RLS might not be configured - log but don't fail
            logger.debug("Could not set RLS context: %s", e)

    def _reset_org_context(self):
        """
        Reset the PostgreSQL session variable after request.
        Critical to prevent context leakage between requests on pooled connections.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.current_org', '', false)")
        except Exception:
            pass


class RequireOrgContext:
    """
    Stricter middleware that fails if org context is not set.

    Use this instead of SetOrgContext when you want to ensure
    all requests have proper org context (after RLS is fully enabled).

    Usage in settings.py:
        MIDDLEWARE = [
            ...
            'common.middleware.get_company.GetProfileAndOrg',
            'common.middleware.rls_context.RequireOrgContext',
            ...
        ]
    """

    # Exact paths that don't require org context. Keep single webhooks here so
    # an appended or adjacent route cannot inherit the exemption.
    EXACT_EXEMPT_PATHS = {
        "/api/sdr/public/ses-feedback/",
    }

    # Path prefixes that don't require org context.
    EXEMPT_PATHS = [
        "/healthz/",
        "/api/auth/refresh-token/",
        "/api/auth/me/",
        "/api/auth/switch-org/",
        "/api/auth/google/",
        "/api/auth/magic-link/request/",
        "/api/auth/magic-link/verify/",
        "/api/auth/magic-link/verify-code/",
        "/api/org/",
        "/admin/",
        "/swagger-ui/",
        "/api/schema/",
        # Public CSAT survey link (Tier 2 csat) — anonymous, sets RLS
        # context manually inside the view from the survey's own org_id.
        "/api/public/csat/",
        # Meta signs the request body; the Page id is then resolved to an org
        # and RLS context is set explicitly by the background integration job.
        "/api/integrations/facebook/webhook/",
        # Meta signs WhatsApp status events; the phone-number route resolves
        # the tenant and the handler enters that database org context itself.
        "/api/integrations/whatsapp/webhook/",
        # Signed state binds the public Meta OAuth callback to the tenant; the
        # callback sets that tenant's RLS context before reading its session.
        "/api/integrations/facebook/oauth/callback/",
        # Nurture links carry signed, event-specific tokens. Each view validates
        # its token and enters the embedded tenant's RLS context before queries.
        # Keep these prefixes narrow rather than exempting all future public
        # SDR routes.
        "/api/sdr/public/nurture/open/",
        "/api/sdr/public/nurture/click/",
        "/api/sdr/public/nurture/unsubscribe/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check if path requires org context
        if not self._is_exempt(request.path):
            # Skip check for URLs that don't resolve (let Django return 404)
            from django.urls import resolve
            from django.urls.exceptions import Resolver404

            try:
                resolve(request.path)
            except Resolver404:
                return self.get_response(request)

            if not hasattr(request, "org") or request.org is None:
                return JsonResponse(
                    {"detail": "Organization context is required. Please login again."},
                    status=403,
                )

        # Set org context
        self._set_org_context(request)

        response = self.get_response(request)

        # Reset context
        self._reset_org_context()

        return response

    def _is_exempt(self, path):
        """Check if path is exempt from org context requirement."""
        return (
            path in self.EXACT_EXEMPT_PATHS
            or _INBOUND_EMAIL_WEBHOOK_PATH_RE.fullmatch(path) is not None
            or any(path.startswith(exempt) for exempt in self.EXEMPT_PATHS)
        )

    def _set_org_context(self, request):
        """Set PostgreSQL session variable (session scope for autocommit mode)."""
        if not hasattr(request, "org") or request.org is None:
            return

        org_id = str(request.org.id)

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_org', %s, false)", [org_id]
                )
        except Exception as e:
            logger.warning("Failed to set RLS context: %s", e)

    def _reset_org_context(self):
        """Reset PostgreSQL session variable after request."""
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.current_org', '', false)")
        except Exception:
            pass


# SQL to enable RLS on all org-scoped tables
RLS_SETUP_SQL = """
-- Enable RLS on main tables
-- Run this after all org-scoped tables are identified

-- Example for leads table:
-- ALTER TABLE lead ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE lead FORCE ROW LEVEL SECURITY;  -- Apply to table owner too
-- CREATE POLICY org_isolation ON lead
--   USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid);

-- Tables that need RLS policies:
-- lead, accounts, contacts, opportunity, cases, tasks, invoices,
-- comment, attachments, document, teams, activity, tags, address,
-- api_settings, board, board_column, board_task, board_member

-- Note: Use NULLIF to handle empty string when context is not set
-- This makes the policy return no rows when context is not set (fail-safe)
"""
