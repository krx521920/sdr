"""
Management command to check Row-Level Security (RLS) status.

Usage:
    python manage.py manage_rls --status          # Check RLS status
    python manage.py manage_rls --test            # Test RLS is working
    python manage.py manage_rls --verify-user     # Verify DB user is not superuser

RLS Configuration: See common/rls/__init__.py for centralized policy definitions.
RLS is enabled/disabled via Django migrations, not this command.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from common.rls import RLS_CONFIG, get_check_rls_status_sql, get_set_context_sql


class Command(BaseCommand):
    help = "Check Row-Level Security (RLS) status for multi-tenancy"

    # Use centralized RLS configuration
    ORG_SCOPED_TABLES = RLS_CONFIG["tables"]

    # PostgreSQL 16's canonical rendering (via pg_policies/pg_get_expr) of the
    # policy emitted by common.rls.get_enable_policy_sql().  Strict mode uses
    # an allow-list instead of looking for a few reassuring substrings: an
    # expression containing the right names can still be unsafe when joined to
    # an always-true branch with OR.
    _EXPECTED_POLICY_EXPRESSION = "".join(
        """
        ((org_id)::text = ( SELECT NULLIF(
            current_setting('app.current_org'::text, true), ''::text
        ) AS "nullif"))
        """.lower().split()
    )

    @classmethod
    def _policy_expression_is_tenant_bound(cls, expression):
        normalized = "".join(str(expression or "").lower().split())
        return normalized == cls._EXPECTED_POLICY_EXPRESSION

    def _policies_are_strict(self, cursor, table):
        """Reject missing or additional permissive policies that could bypass RLS."""
        cursor.execute(
            """
            SELECT policyname, permissive, roles, cmd, qual, with_check
            FROM pg_policies
            WHERE schemaname = 'public' AND tablename = %s
            ORDER BY policyname
            """,
            [table],
        )
        policies = {row[0]: row[1:] for row in cursor.fetchall()}
        if set(policies) != {"org_isolation", "org_insert_check"}:
            return False

        isolation = policies["org_isolation"]
        insert_check = policies["org_insert_check"]
        (
            isolation_permissive,
            isolation_roles,
            isolation_cmd,
            isolation_qual,
            isolation_with_check,
        ) = isolation
        (
            insert_permissive,
            insert_roles,
            insert_cmd,
            insert_qual,
            insert_with_check,
        ) = insert_check
        return (
            isolation_permissive == "PERMISSIVE"
            and insert_permissive == "PERMISSIVE"
            and isolation_roles == ["public"]
            and insert_roles == ["public"]
            and isolation_cmd == "ALL"
            and insert_cmd == "INSERT"
            and isolation_with_check is None
            and insert_qual is None
            and self._policy_expression_is_tenant_bound(isolation_qual)
            and self._policy_expression_is_tenant_bound(insert_with_check)
        )

    def add_arguments(self, parser):
        parser.add_argument(
            "--status", action="store_true", help="Check RLS status for all tables"
        )
        parser.add_argument(
            "--test", action="store_true", help="Test that RLS is working correctly"
        )
        parser.add_argument(
            "--verify-user",
            action="store_true",
            help="Verify database user is not a superuser",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Exit non-zero unless the application role is least-privileged "
                "and every configured table has forced RLS."
            ),
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            message = "RLS is only supported on PostgreSQL"
            if options["strict"]:
                raise CommandError(message)
            self.stderr.write(self.style.ERROR(message))
            return

        if options["status"]:
            self.check_status(strict=options["strict"])
        elif options["test"]:
            self.test_rls()
        elif options["verify_user"]:
            self.verify_user(strict=options["strict"])
        else:
            self.check_status(strict=options["strict"])

    def check_status(self, *, strict=False):
        """Check RLS status for all org-scoped tables."""
        self.stdout.write(self.style.MIGRATE_HEADING("RLS Status:"))
        self.stdout.write("")

        with connection.cursor() as cursor:
            # Check if current user is superuser
            cursor.execute(
                """
                SELECT rolname, rolsuper, rolbypassrls
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
            user, is_super, can_bypass_rls = cursor.fetchone()

            if is_super or can_bypass_rls:
                self.stdout.write(
                    self.style.WARNING(
                        f'  Database user "{user}" can bypass RLS!'
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  Database user "{user}" is not a superuser - RLS will be enforced'
                    )
                )

            self.stdout.write("")

            enabled_count = 0
            forced_count = 0
            disabled_count = 0
            missing_count = 0
            invalid_policy_count = 0

            for table in self.ORG_SCOPED_TABLES:
                cursor.execute(get_check_rls_status_sql(), [table])

                result = cursor.fetchone()
                if result:
                    rls_enabled, rls_forced = result
                    if rls_enabled:
                        status = self.style.SUCCESS("ENABLED")
                        if rls_forced:
                            status += " (forced)"
                            forced_count += 1
                        enabled_count += 1
                    else:
                        status = self.style.WARNING("disabled")
                        disabled_count += 1
                    if strict and not self._policies_are_strict(cursor, table):
                        status += " " + self.style.ERROR("(invalid policies)")
                        invalid_policy_count += 1
                else:
                    status = self.style.ERROR("TABLE NOT FOUND")
                    missing_count += 1

                self.stdout.write(f"  {table}: {status}")

            self.stdout.write("")
            self.stdout.write(
                "  "
                f"Enabled: {enabled_count}, Forced: {forced_count}, "
                f"Disabled: {disabled_count}, Missing: {missing_count}, "
                f"Invalid policies: {invalid_policy_count}"
            )

            if strict and (
                is_super
                or can_bypass_rls
                or disabled_count
                or missing_count
                or invalid_policy_count
                or forced_count != len(self.ORG_SCOPED_TABLES)
            ):
                raise CommandError(
                    "Strict RLS verification failed: the application role must not "
                    "bypass RLS and every configured table must have forced, "
                    "tenant-bound policies without additional bypass policies."
                )

    def test_rls(self):
        """Test that RLS is working correctly."""
        self.stdout.write(self.style.MIGRATE_HEADING("Testing RLS..."))

        set_context_sql = get_set_context_sql()

        with connection.cursor() as cursor:
            # Find orgs that have leads (need to check each org since RLS is active)
            cursor.execute(
                "SELECT id FROM organization ORDER BY created_at DESC LIMIT 50"
            )
            all_orgs = cursor.fetchall()

            orgs_with_leads = []
            for (org_id,) in all_orgs:
                cursor.execute(set_context_sql, [str(org_id)])
                cursor.execute("SELECT COUNT(*) FROM lead")
                if cursor.fetchone()[0] > 0:
                    orgs_with_leads.append(str(org_id))
                    if len(orgs_with_leads) >= 2:
                        break

            if len(orgs_with_leads) < 1:
                # Fall back to first 2 orgs for testing
                cursor.execute("SELECT id FROM organization LIMIT 2")
                orgs = cursor.fetchall()
                if len(orgs) < 2:
                    self.stdout.write(
                        self.style.WARNING(
                            "Need at least 2 orgs to test RLS. Skipping."
                        )
                    )
                    return
                org_a = str(orgs[0][0])
                org_b = str(orgs[1][0])
            else:
                org_a = orgs_with_leads[0]
                org_b = (
                    orgs_with_leads[1]
                    if len(orgs_with_leads) > 1
                    else str(all_orgs[0][0])
                )

            # Test with org_a context
            cursor.execute(set_context_sql, [org_a])
            cursor.execute("SELECT COUNT(*) FROM lead")
            count_a = cursor.fetchone()[0]

            # Test with org_b context
            cursor.execute(set_context_sql, [org_b])
            cursor.execute("SELECT COUNT(*) FROM lead")
            count_b = cursor.fetchone()[0]

            # Test with no context
            cursor.execute(set_context_sql, [""])
            cursor.execute("SELECT COUNT(*) FROM lead")
            count_none = cursor.fetchone()[0]

            self.stdout.write(f"  Leads with org_a context: {count_a}")
            self.stdout.write(f"  Leads with org_b context: {count_b}")
            self.stdout.write(f"  Leads with no context: {count_none}")

            if count_a == 0 and count_b == 0 and count_none == 0:
                self.stdout.write(
                    self.style.WARNING(
                        "No lead data found. Create leads for different orgs to test RLS isolation."
                    )
                )
            elif count_none == 0 and (count_a > 0 or count_b > 0):
                self.stdout.write(
                    self.style.SUCCESS("RLS is working - no data without context")
                )
            elif count_none > 0:
                self.stdout.write(
                    self.style.WARNING(
                        "RLS may not be fully enabled - data visible without context. "
                        "This is expected if the policy allows empty context."
                    )
                )

    def verify_user(self, *, strict=False):
        """Verify the database user is not a superuser."""
        self.stdout.write(self.style.MIGRATE_HEADING("Verifying database user..."))

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    rolname,
                    rolsuper,
                    rolcreatedb,
                    rolbypassrls,
                    has_database_privilege(current_user, current_database(), 'CREATE'),
                    has_schema_privilege(current_user, 'public', 'CREATE')
                FROM pg_roles
                WHERE rolname = current_user
            """
            )
            (
                user,
                is_super,
                can_create_db,
                can_bypass_rls,
                can_create_schema,
                can_create_objects,
            ) = cursor.fetchone()

            self.stdout.write(f"  Current user: {user}")
            self.stdout.write(f"  Is superuser: {is_super}")
            self.stdout.write(f"  Can create DB: {can_create_db}")
            self.stdout.write(f"  Can bypass RLS: {can_bypass_rls}")
            self.stdout.write(f"  Can create schemas: {can_create_schema}")
            self.stdout.write(f"  Can create objects in public: {can_create_objects}")

            if is_super or can_bypass_rls:
                self.stdout.write("")
                self.stdout.write(
                    self.style.ERROR(
                        "WARNING: This role can bypass RLS!\n"
                        "Create a non-superuser for the application:\n\n"
                        "  CREATE USER crm_app WITH PASSWORD 'secure_password';\n"
                        "  GRANT CONNECT ON DATABASE bottlecrm TO crm_app;\n"
                        "  GRANT USAGE ON SCHEMA public TO crm_app;\n"
                        "  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;\n"
                        "  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_app;\n"
                    )
                )
                raise CommandError("Database user can bypass RLS")
            if strict and (can_create_db or can_create_schema or can_create_objects):
                raise CommandError(
                    "Strict database role verification failed: application user "
                    "must not have database or schema creation privileges."
                )
            if can_create_db:
                self.stdout.write(
                    self.style.WARNING(
                        "Database user can create databases; allowed for test roles only."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS("Database user is properly configured for RLS")
                )
