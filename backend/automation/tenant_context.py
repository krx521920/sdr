"""PostgreSQL RLS context for jobs running outside request middleware."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from django.db import connection


@contextmanager
def database_org_context(org_id: UUID) -> Iterator[None]:
    if connection.vendor != "postgresql":
        yield
        return

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_org', true)")
        previous = cursor.fetchone()[0] or ""
        cursor.execute("SELECT set_config('app.current_org', %s, false)", [str(org_id)])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_org', %s, false)", [previous]
            )
