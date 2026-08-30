import django.db.models.deletion
from django.db import migrations, models

CREATE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION integration_validate_whatsapp_message_execution_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.execution_request_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM integration_external_execution_request
        WHERE id = NEW.execution_request_id
          AND org_id = NEW.org_id
          AND channel = 'whatsapp'
          AND action = 'send_message'
          AND idempotency_key = NEW.id
    ) THEN
        RAISE EXCEPTION 'WhatsApp execution request scope mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER integration_whatsapp_message_execution_scope_guard
BEFORE INSERT OR UPDATE OF id, org_id, execution_request_id
ON integration_whatsapp_message
FOR EACH ROW
EXECUTE FUNCTION integration_validate_whatsapp_message_execution_scope();
"""


DROP_GUARD_SQL = """
DROP TRIGGER IF EXISTS integration_whatsapp_message_execution_scope_guard
    ON integration_whatsapp_message;
DROP FUNCTION IF EXISTS integration_validate_whatsapp_message_execution_scope();
"""


def install_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_GUARD_SQL, params=None)


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_GUARD_SQL, params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0018_fix_feishu_import_scope_guard"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappmessage",
            name="execution_request",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="whatsapp_message",
                to="integrations.externalexecutionrequest",
            ),
        ),
        migrations.AlterField(
            model_name="whatsappmessage",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("queued", "Queued"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("delivered", "Delivered"),
                    ("read", "Read"),
                    ("unknown", "Unknown provider outcome"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.RunPython(install_guard, remove_guard),
    ]
