from django.db import migrations


FUNCTION_TEMPLATE = r"""
CREATE OR REPLACE FUNCTION integration_validate_feishu_base_person_import_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    bound_job automation_job%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        OLD.org_id IS DISTINCT FROM NEW.org_id
        OR OLD.connection_id IS DISTINCT FROM NEW.connection_id
        OR OLD.requested_by_id IS DISTINCT FROM NEW.requested_by_id
        OR OLD.execution_request_id IS DISTINCT FROM NEW.execution_request_id
        OR OLD.mapping_ciphertext IS DISTINCT FROM NEW.mapping_ciphertext
        OR OLD.mapping_sha256 IS DISTINCT FROM NEW.mapping_sha256
        OR OLD.destination_sha256 IS DISTINCT FROM NEW.destination_sha256
        OR OLD.source_namespace IS DISTINCT FROM NEW.source_namespace
        OR OLD.row_limit IS DISTINCT FROM NEW.row_limit
        OR (
            OLD.automation_job_id IS NOT NULL
            AND OLD.automation_job_id IS DISTINCT FROM NEW.automation_job_id
        )
        OR (
            OLD.import_batch_id IS NOT NULL
            AND OLD.import_batch_id IS DISTINCT FROM NEW.import_batch_id
        )
    ) THEN
        RAISE EXCEPTION 'Feishu Base person import immutable binding changed'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM integration_feishu_base_connection
        WHERE id = NEW.connection_id AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Feishu Base person import connection mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM profile
        WHERE id = NEW.requested_by_id AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Feishu Base person import requester mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM integration_external_execution_request
        WHERE id = NEW.execution_request_id
          AND org_id = NEW.org_id
          AND channel = 'feishu'
          AND action = 'import_person_records'
    ) THEN
        RAISE EXCEPTION 'Feishu Base person import execution mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.automation_job_id IS NOT NULL THEN
        SELECT * INTO bound_job FROM automation_job
        WHERE id = NEW.automation_job_id AND org_id = NEW.org_id;
        IF NOT FOUND
           OR bound_job.name <> 'integrations.feishu_base_person_import'
           OR {payload_key_count} <> 2
           OR bound_job.payload ->> 'import_id' <> NEW.id::text
           OR bound_job.payload ->> 'execution_request_id' <> NEW.execution_request_id::text
        THEN
            RAISE EXCEPTION 'Feishu Base person import job mismatch'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.import_batch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM matching_person_import_batch
        WHERE id = NEW.import_batch_id
          AND org_id = NEW.org_id
          AND source = 'feishu'
          AND source_namespace = NEW.source_namespace
    ) THEN
        RAISE EXCEPTION 'Feishu Base person import batch mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'previewed' AND NEW.import_batch_id IS NULL THEN
        RAISE EXCEPTION 'Previewed Feishu Base import requires a batch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
"""

FIXED_FUNCTION_SQL = FUNCTION_TEMPLATE.format(
    payload_key_count=(
        "(SELECT count(*) FROM jsonb_object_keys(bound_job.payload))"
    )
)

def install_fixed_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(FIXED_FUNCTION_SQL, params=None)


def preserve_fixed_guard_on_reverse(apps, schema_editor):
    """Keep the scope guard executable while temporarily rolled back to 0017.

    Migration 0017 used PostgreSQL's nonexistent ``jsonb_object_length`` and
    therefore cannot be restored safely.  The fixed function enforces the same
    intended invariant, so reverse migrations retain it instead of reintroducing
    a trigger that accepts deployment but fails on the next bound-job write.
    """
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(FIXED_FUNCTION_SQL, params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0017_feishu_base_person_import"),
    ]

    operations = [
        migrations.RunPython(install_fixed_guard, preserve_fixed_guard_on_reverse),
    ]
