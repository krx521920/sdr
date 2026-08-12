"""Allow-listed downstream handlers for accepted inbound email."""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from cases.models import EmailMessage, InboundMailbox

RouteTargetHandler = Callable[[EmailMessage], None]


def dispatch_to_route_target(
    *,
    mailbox: InboundMailbox,
    email_message: EmailMessage,
) -> None:
    dotted_path = settings.INBOUND_EMAIL_ROUTE_HANDLERS.get(mailbox.route_target)
    if not dotted_path:
        raise ImproperlyConfigured(
            f"No inbound email handler is configured for {mailbox.route_target!r}."
        )
    handler: RouteTargetHandler = import_string(dotted_path)
    if not callable(handler):
        raise ImproperlyConfigured(
            f"Inbound email handler for {mailbox.route_target!r} is not callable."
        )
    handler(email_message)
