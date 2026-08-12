"""Anonymous signed endpoints used by SDR nurture email clients."""

from __future__ import annotations

import base64

from django.core import signing
from django.http import HttpResponse, HttpResponseNotFound, HttpResponseRedirect
from django.views import View

from common.tasks import set_rls_context
from sdr.models import NurtureInteractionType
from sdr.tracking import parse_tracking_token, record_interaction

TRANSPARENT_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)


def _pixel_response() -> HttpResponse:
    response = HttpResponse(TRANSPARENT_GIF, content_type="image/gif")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class NurtureOpenTrackingView(View):
    http_method_names = ("get", "head")

    def get(self, request, token: str, *args, **kwargs):
        try:
            event = parse_tracking_token(token, NurtureInteractionType.OPEN)
            set_rls_context(event.org_id)
            record_interaction(
                event,
                remote_addr=request.META.get("REMOTE_ADDR", ""),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except signing.BadSignature:
            # A tracking pixel never leaks whether a signed delivery exists.
            pass
        return _pixel_response()

    def head(self, request, token: str, *args, **kwargs):
        # Prefetch and availability probes must not count as recipient opens.
        return _pixel_response()


class NurtureClickTrackingView(View):
    http_method_names = ("get", "head")

    def get(self, request, token: str, *args, **kwargs):
        try:
            event = parse_tracking_token(token, NurtureInteractionType.CLICK)
        except signing.BadSignature:
            return HttpResponseNotFound()
        set_rls_context(event.org_id)
        record_interaction(
            event,
            remote_addr=request.META.get("REMOTE_ADDR", ""),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        response = HttpResponseRedirect(event.target_url)
        response["Cache-Control"] = "no-store"
        response["Referrer-Policy"] = "no-referrer"
        return response

    def head(self, request, token: str, *args, **kwargs):
        # Do not redirect scanners or count their prefetch probes.
        return HttpResponse(status=204)
