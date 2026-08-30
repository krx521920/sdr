# Generated manually for atomic person onboarding and tenant referential guards.

from django.db import migrations, models

CREATE_POSTGRES_PERSON_CHILD_GUARDS_SQL = """
CREATE OR REPLACE FUNCTION matching_validate_person_child_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM matching_person
        WHERE id = NEW.person_id
          AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION
            'matching child organization must match person organization'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER matching_person_identity_org_guard
BEFORE INSERT OR UPDATE OF person_id, org_id ON matching_person_identity
FOR EACH ROW EXECUTE FUNCTION matching_validate_person_child_org();

CREATE TRIGGER matching_evidence_person_org_guard
BEFORE INSERT OR UPDATE OF person_id, org_id ON matching_evidence
FOR EACH ROW EXECUTE FUNCTION matching_validate_person_child_org();
"""


DROP_POSTGRES_PERSON_CHILD_GUARDS_SQL = """
DROP TRIGGER IF EXISTS matching_evidence_person_org_guard ON matching_evidence;
DROP TRIGGER IF EXISTS matching_person_identity_org_guard
    ON matching_person_identity;
DROP FUNCTION IF EXISTS matching_validate_person_child_org();
"""


def validate_existing_person_child_orgs(schema_editor):
    """Fail migration if pre-trigger data already violates the tenant invariant."""

    guarded_tables = (
        "matching_person",
        "matching_person_identity",
        "matching_evidence",
    )
    with schema_editor.connection.cursor() as cursor:
        try:
            for table in guarded_tables:
                cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT child.id
                    FROM matching_person_identity AS child
                    JOIN matching_person AS person ON person.id = child.person_id
                    WHERE child.org_id <> person.org_id
                    UNION ALL
                    SELECT child.id
                    FROM matching_evidence AS child
                    JOIN matching_person AS person ON person.id = child.person_id
                    WHERE child.org_id <> person.org_id
                ) AS mismatches
                """
            )
            mismatch_count = cursor.fetchone()[0]
        finally:
            for table in guarded_tables:
                cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    if mismatch_count:
        raise RuntimeError(
            "Cannot install matching person child guards: "
            f"found {mismatch_count} cross-organization child rows."
        )


def create_postgres_person_child_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    validate_existing_person_child_orgs(schema_editor)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_POSTGRES_PERSON_CHILD_GUARDS_SQL)


def drop_postgres_person_child_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_POSTGRES_PERSON_CHILD_GUARDS_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("matching", "0002_match_audit_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="onboarding_evidence_ids",
            field=models.JSONField(default=list, editable=False),
        ),
        migrations.AddField(
            model_name="person",
            name="onboarding_identity_ids",
            field=models.JSONField(default=list, editable=False),
        ),
        migrations.AddField(
            model_name="person",
            name="onboarding_idempotency_key",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="person",
            name="onboarding_request_hash",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="person",
            constraint=models.UniqueConstraint(
                condition=models.Q(onboarding_idempotency_key__isnull=False),
                fields=("org", "onboarding_idempotency_key"),
                name="unique_matching_onboarding_key_per_org",
            ),
        ),
        migrations.RunPython(
            create_postgres_person_child_guards,
            drop_postgres_person_child_guards,
        ),
    ]
