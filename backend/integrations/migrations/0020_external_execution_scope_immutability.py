from django.db import migrations

CREATE_SCOPE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION integration_validate_external_execution_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.org_id IS DISTINCT FROM OLD.org_id
        OR NEW.channel IS DISTINCT FROM OLD.channel
        OR NEW.action IS DISTINCT FROM OLD.action
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
        OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
        OR NEW.target_hash IS DISTINCT FROM OLD.target_hash
        OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
        OR NEW.units IS DISTINCT FROM OLD.units
        OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
    ) THEN
        RAISE EXCEPTION 'External execution request scope is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM integration_channel_execution_approval approval
        WHERE approval.id = NEW.approval_id
          AND approval.org_id = NEW.org_id
          AND approval.channel = NEW.channel
          AND approval.action = NEW.action
          AND approval.target_hash = NEW.target_hash
          AND approval.payload_hash = NEW.payload_hash
          AND approval.units = NEW.units
    ) THEN
        RAISE EXCEPTION 'External execution request approval scope mismatch'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER integration_external_execution_scope_guard
BEFORE INSERT OR UPDATE
ON integration_external_execution_request
FOR EACH ROW
EXECUTE FUNCTION integration_validate_external_execution_scope();
"""


DROP_SCOPE_GUARD_SQL = """
DROP TRIGGER IF EXISTS integration_external_execution_scope_guard
    ON integration_external_execution_request;
DROP FUNCTION IF EXISTS integration_validate_external_execution_scope();
"""


def install_scope_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_SCOPE_GUARD_SQL, params=None)


def remove_scope_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_SCOPE_GUARD_SQL, params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0019_whatsapp_execution_safety"),
    ]

    operations = [
        migrations.RunPython(install_scope_guard, remove_scope_guard),
    ]
