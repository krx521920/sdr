"""Public RFC 8058-compatible nurture unsubscribe endpoint."""

from __future__ import annotations

from django.core import signing
from django.http import HttpResponse, HttpResponseNotFound
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from common.tasks import set_rls_context
from sdr.models import (
    EmailSuppressionReason,
    EmailSuppressionSource,
    LeadNurtureDelivery,
    NurtureDeliveryStatus,
)
from sdr.suppression import parse_unsubscribe_token, suppress_email

CONFIRMATION_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Unsubscribe</title></head>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem">
<h1>Stop nurture emails?</h1>
<p>This will stop automated sales follow-up emails to this address.</p>
<form method="post"><input type="hidden" name="List-Unsubscribe" value="One-Click">
<button type="submit">Unsubscribe</button></form></body></html>"""

SUCCESS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Unsubscribed</title></head>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem">
<h1>You are unsubscribed</h1><p>Automated sales follow-up emails to this address have stopped.</p>
</body></html>"""


@method_decorator(csrf_exempt, name="dispatch")
class NurtureUnsubscribeView(View):
    http_method_names = ("get", "post")

    def get(self, request, token: str, *args, **kwargs):
        delivery = self._delivery(token)
        if delivery is None:
            return HttpResponseNotFound()
        return self._html(CONFIRMATION_HTML)

    def post(self, request, token: str, *args, **kwargs):
        delivery = self._delivery(token)
        if delivery is None:
            return HttpResponseNotFound()
        suppress_email(
            org_id=delivery.org_id,
            email=delivery.recipient,
            reason=EmailSuppressionReason.UNSUBSCRIBED,
            source=EmailSuppressionSource.ONE_CLICK,
            source_delivery=delivery,
            details={"delivery_id": str(delivery.id)},
        )
        return self._html(SUCCESS_HTML)

    @staticmethod
    def _delivery(token: str) -> LeadNurtureDelivery | None:
        try:
            event = parse_unsubscribe_token(token)
        except signing.BadSignature:
            return None
        set_rls_context(event.org_id)
        return (
            LeadNurtureDelivery.objects.filter(
                id=event.delivery_id,
                org_id=event.org_id,
                status=NurtureDeliveryStatus.SENT,
            )
            .select_related("enrollment__intake", "enrollment__sequence")
            .first()
        )

    @staticmethod
    def _html(content: str) -> HttpResponse:
        response = HttpResponse(content, content_type="text/html; charset=utf-8")
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        return response
