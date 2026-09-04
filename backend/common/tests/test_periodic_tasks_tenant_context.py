from collections import Counter
from contextlib import contextmanager

import pytest
from django.db import connection

from cases.tasks import auto_stop_stale_timers, scan_for_breached_cases
from common.rls import RLS_CONFIG
from common.tasks import purge_read_notifications
from invoices.tasks import (
    check_expired_estimates,
    check_overdue_invoices,
    generate_recurring_invoices,
    process_payment_reminders,
)
from opportunity.tasks import check_goal_milestones, check_stale_opportunities

PROTECTED_TABLES = set(RLS_CONFIG["tables"]) | {
    "sales_goal",
    "stage_aging_config",
}


@pytest.mark.django_db
def test_periodic_tasks_query_org_scoped_tables_only_inside_org_context(
    org_a,
    org_b,
    monkeypatch,
):
    active_orgs = []
    entered_orgs = []

    @contextmanager
    def tracking_org_context(org_id):
        active_orgs.append(org_id)
        entered_orgs.append(org_id)
        try:
            yield
        finally:
            active_orgs.pop()

    def reject_unscoped_queries(execute, sql, params, many, context):
        normalized_sql = sql.lower()
        touches_protected_table = any(
            f'"{table}"' in normalized_sql for table in PROTECTED_TABLES
        )
        assert not touches_protected_table or active_orgs, sql
        return execute(sql, params, many, context)

    monkeypatch.setattr(
        "invoices.tasks.database_org_context", tracking_org_context
    )
    monkeypatch.setattr("cases.tasks.database_org_context", tracking_org_context)
    monkeypatch.setattr("common.tasks.database_org_context", tracking_org_context)
    monkeypatch.setattr(
        "opportunity.tasks.database_org_context", tracking_org_context
    )

    with connection.execute_wrapper(reject_unscoped_queries):
        generate_recurring_invoices.run()
        check_overdue_invoices.run()
        process_payment_reminders.run()
        check_expired_estimates.run()
        scan_for_breached_cases.run()
        auto_stop_stale_timers.run()
        purge_read_notifications.run()
        check_stale_opportunities.run()
        check_goal_milestones.run()

    assert not active_orgs
    assert Counter(entered_orgs) == Counter({org_a.id: 9, org_b.id: 9})
