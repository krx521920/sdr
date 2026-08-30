from django.db import migrations

NARROW_PROJECTION_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION matching_validate_match_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    validate_match_scope boolean := false;
BEGIN
    IF TG_TABLE_NAME = 'matching_match' THEN
        IF TG_OP = 'INSERT' THEN
            validate_match_scope := true;
        ELSIF TG_OP = 'UPDATE' THEN
            validate_match_scope := (
                NEW.org_id IS DISTINCT FROM OLD.org_id
                OR NEW.person_id IS DISTINCT FROM OLD.person_id
                OR NEW.opportunity_id IS DISTINCT FROM OLD.opportunity_id
                OR NEW.projection_state IS DISTINCT FROM OLD.projection_state
            );
        END IF;

        IF validate_match_scope THEN
            IF NOT EXISTS (
                SELECT 1 FROM matching_opportunity o
                WHERE o.id = NEW.opportunity_id AND o.org_id = NEW.org_id
            ) OR NOT EXISTS (
                SELECT 1 FROM matching_person p
                WHERE p.id = NEW.person_id AND p.org_id = NEW.org_id
            ) THEN
                RAISE EXCEPTION 'matching projection organization mismatch'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.projection_state = 'current' THEN
                PERFORM 1
                FROM matching_person p
                WHERE p.id = NEW.person_id
                  AND p.org_id = NEW.org_id
                  AND p.status = 'active'
                  AND p.governance_status = 'active'
                FOR SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'current match projection requires an active person'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person' THEN
        IF (NEW.status <> 'active' OR NEW.governance_status <> 'active')
           AND EXISTS (
                SELECT 1 FROM matching_match m
                WHERE m.person_id = NEW.id
                  AND m.org_id = NEW.org_id
                  AND m.projection_state = 'current'
           ) THEN
            RAISE EXCEPTION 'person has a current match projection'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


RESTORE_PROJECTION_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION matching_validate_match_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'matching_match' THEN
        IF NOT EXISTS (
            SELECT 1 FROM matching_opportunity o
            WHERE o.id = NEW.opportunity_id AND o.org_id = NEW.org_id
        ) OR NOT EXISTS (
            SELECT 1 FROM matching_person p
            WHERE p.id = NEW.person_id AND p.org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'matching projection organization mismatch'
                USING ERRCODE = '23514';
        END IF;

        IF NEW.projection_state = 'current' THEN
            PERFORM 1
            FROM matching_person p
            WHERE p.id = NEW.person_id
              AND p.org_id = NEW.org_id
              AND p.status = 'active'
              AND p.governance_status = 'active'
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'current match projection requires an active person'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person' THEN
        IF (NEW.status <> 'active' OR NEW.governance_status <> 'active')
           AND EXISTS (
                SELECT 1 FROM matching_match m
                WHERE m.person_id = NEW.id
                  AND m.org_id = NEW.org_id
                  AND m.projection_state = 'current'
           ) THEN
            RAISE EXCEPTION 'person has a current match projection'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


def narrow_projection_guard_locks(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(NARROW_PROJECTION_GUARD_SQL)


def restore_projection_guard_locks(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(RESTORE_PROJECTION_GUARD_SQL)


class Migration(migrations.Migration):
    dependencies = [("matching", "0010_match_projection_integrity")]

    operations = [
        migrations.RunPython(
            narrow_projection_guard_locks,
            restore_projection_guard_locks,
        )
    ]
