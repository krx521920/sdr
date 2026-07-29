"""Resolve allow-listed job handlers from deployment settings."""

from collections.abc import Callable, Mapping
from typing import Any

from django.conf import settings
from django.utils.module_loading import import_string

from automation.errors import PermanentJobError

JobHandler = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


def get_job_handler(name: str) -> JobHandler:
    dotted_path = settings.AUTOMATION_JOB_HANDLERS.get(name)
    if not dotted_path:
        raise PermanentJobError(
            f"No handler is registered for job '{name}'",
            code="handler_not_registered",
        )
    handler = import_string(dotted_path)
    if not callable(handler):
        raise PermanentJobError(
            f"Configured handler for job '{name}' is not callable",
            code="handler_not_callable",
        )
    return handler
