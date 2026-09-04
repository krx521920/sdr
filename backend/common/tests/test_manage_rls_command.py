from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from django.core.management.base import CommandError

from common.management.commands import manage_rls


class _FakeConnection:
    vendor = "postgresql"

    def __init__(self, rows, policy_rows=None):
        self.cursor_object = Mock()
        self.cursor_object.fetchone.side_effect = rows
        if policy_rows is not None:
            self.cursor_object.fetchall.side_effect = policy_rows

    @contextmanager
    def cursor(self):
        yield self.cursor_object


def test_strict_user_verification_accepts_least_privileged_role(monkeypatch):
    connection = _FakeConnection(
        [("crm_user", False, False, False, False, False)]
    )
    monkeypatch.setattr(manage_rls, "connection", connection)

    manage_rls.Command().verify_user(strict=True)


@pytest.mark.parametrize(
    "role",
    [
        ("crm_user", True, False, False, False, False),
        ("crm_user", False, False, True, False, False),
        ("crm_user", False, True, False, False, False),
        ("crm_user", False, False, False, True, False),
        ("crm_user", False, False, False, False, True),
    ],
)
def test_strict_user_verification_rejects_privileged_role(monkeypatch, role):
    connection = _FakeConnection([role])
    monkeypatch.setattr(manage_rls, "connection", connection)

    with pytest.raises(CommandError):
        manage_rls.Command().verify_user(strict=True)


def test_strict_status_rejects_enabled_but_unforced_table(monkeypatch):
    command = manage_rls.Command()
    table_rows = [(True, True)] * len(command.ORG_SCOPED_TABLES)
    table_rows[0] = (True, False)
    connection = _FakeConnection(
        [("crm_user", False, False), *table_rows],
        policy_rows=[_valid_policies()] * len(command.ORG_SCOPED_TABLES),
    )
    monkeypatch.setattr(manage_rls, "connection", connection)

    with pytest.raises(CommandError):
        command.check_status(strict=True)


def test_strict_status_accepts_forced_rls_on_every_table(monkeypatch):
    command = manage_rls.Command()
    connection = _FakeConnection(
        [
            ("crm_user", False, False),
            *[(True, True)] * len(command.ORG_SCOPED_TABLES),
        ],
        policy_rows=[_valid_policies()] * len(command.ORG_SCOPED_TABLES),
    )
    monkeypatch.setattr(manage_rls, "connection", connection)

    command.check_status(strict=True)


def _valid_policies():
    expression = (
        "((org_id)::text = ( SELECT NULLIF("
        "current_setting('app.current_org'::text, true), ''::text"
        ') AS "nullif"))'
    )
    return [
        ("org_insert_check", "PERMISSIVE", ["public"], "INSERT", None, expression),
        ("org_isolation", "PERMISSIVE", ["public"], "ALL", expression, None),
    ]


@pytest.mark.parametrize(
    "policy_rows",
    [
        [],
        [("org_isolation", "PERMISSIVE", ["public"], "ALL", "true", None)],
        [
            ("org_insert_check", "PERMISSIVE", ["public"], "INSERT", None, "true"),
            ("org_isolation", "PERMISSIVE", ["public"], "ALL", "true", None),
        ],
        [
            *_valid_policies(),
            ("bypass", "PERMISSIVE", ["public"], "ALL", "true", None),
        ],
    ],
)
def test_strict_status_rejects_missing_weak_or_extra_policy(monkeypatch, policy_rows):
    command = manage_rls.Command()
    connection = _FakeConnection(
        [
            ("crm_user", False, False),
            *[(True, True)] * len(command.ORG_SCOPED_TABLES),
        ],
        policy_rows=[policy_rows] * len(command.ORG_SCOPED_TABLES),
    )
    monkeypatch.setattr(manage_rls, "connection", connection)

    with pytest.raises(CommandError, match="tenant-bound policies"):
        command.check_status(strict=True)


@pytest.mark.parametrize(
    "expression",
    [
        (
            "((org_id)::text <> ( SELECT NULLIF("
            "current_setting('app.current_org'::text, true), ''::text"
            ') AS "nullif"))'
        ),
        (
            "((org_id)::text = ( SELECT NULLIF("
            "current_setting('app.other_org'::text, true), ''::text"
            ') AS "nullif"))'
        ),
        (
            "((org_id)::text = ( SELECT NULLIF("
            "current_setting('app.current_org'::text, true), ''::text"
            ') AS "nullif")) OR org_id IS NOT NULL'
        ),
    ],
)
def test_strict_status_rejects_deceptive_policy_expressions(monkeypatch, expression):
    command = manage_rls.Command()
    policies = [
        ("org_insert_check", "PERMISSIVE", ["public"], "INSERT", None, expression),
        ("org_isolation", "PERMISSIVE", ["public"], "ALL", expression, None),
    ]
    connection = _FakeConnection(
        [
            ("crm_user", False, False),
            *[(True, True)] * len(command.ORG_SCOPED_TABLES),
        ],
        policy_rows=[policies] * len(command.ORG_SCOPED_TABLES),
    )
    monkeypatch.setattr(manage_rls, "connection", connection)

    with pytest.raises(CommandError, match="tenant-bound policies"):
        command.check_status(strict=True)


@pytest.mark.parametrize(
    "policies",
    [
        [
            *_valid_policies()[:1],
            (
                "org_isolation",
                "PERMISSIVE",
                ["public"],
                "ALL",
                _valid_policies()[1][4],
                "true",
            ),
        ],
        [
            (
                "org_insert_check",
                "PERMISSIVE",
                ["public"],
                "INSERT",
                "true",
                _valid_policies()[0][5],
            ),
            *_valid_policies()[1:],
        ],
    ],
)
def test_strict_status_rejects_unexpected_policy_clauses(monkeypatch, policies):
    command = manage_rls.Command()
    connection = _FakeConnection(
        [
            ("crm_user", False, False),
            *[(True, True)] * len(command.ORG_SCOPED_TABLES),
        ],
        policy_rows=[policies] * len(command.ORG_SCOPED_TABLES),
    )
    monkeypatch.setattr(manage_rls, "connection", connection)

    with pytest.raises(CommandError, match="tenant-bound policies"):
        command.check_status(strict=True)
