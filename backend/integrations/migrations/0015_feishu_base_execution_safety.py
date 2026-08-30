import hashlib
import hmac

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.secrets import decrypt_secret, encrypt_secret

CREATE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION integration_validate_feishu_base_sync_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM integration_feishu_base_connection
        WHERE id = NEW.connection_id AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Feishu Base connection organization mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM sdr_lead_intake
        WHERE id = NEW.intake_id AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Feishu Base intake organization mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.execution_request_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM integration_external_execution_request
        WHERE id = NEW.execution_request_id
          AND org_id = NEW.org_id
          AND channel = 'feishu'
          AND action IN ('sync_research_result', 'delete_research_record')
    ) THEN
        RAISE EXCEPTION 'Feishu Base execution request mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER integration_feishu_base_sync_scope_guard
BEFORE INSERT OR UPDATE OF org_id, connection_id, intake_id, execution_request_id
ON integration_feishu_base_sync
FOR EACH ROW EXECUTE FUNCTION integration_validate_feishu_base_sync_scope();
"""


DROP_GUARD_SQL = """
DROP TRIGGER IF EXISTS integration_feishu_base_sync_scope_guard
    ON integration_feishu_base_sync;
DROP FUNCTION IF EXISTS integration_validate_feishu_base_sync_scope();
"""


def _record_hash(*, org_id, record_id):
    message = f"feishu-base-record:v1:{org_id}:{record_id}".encode("utf-8")
    return hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def encrypt_existing_record_ids(apps, schema_editor):
    FeishuBaseSync = apps.get_model("integrations", "FeishuBaseSync")
    _set_sync_rls(schema_editor, enabled=False)
    try:
        for sync in FeishuBaseSync.objects.exclude(record_id="").iterator(
            chunk_size=500
        ):
            record_id = str(sync.record_id).strip()
            if not record_id:
                continue
            digest = _record_hash(org_id=sync.org_id, record_id=record_id)
            sync.record_id_ciphertext = encrypt_secret(record_id)
            sync.record_id_hash = digest
            sync.record_safe_label = f"Feishu Base record {digest[:8]}"
            sync.save(
                update_fields=(
                    "record_id_ciphertext",
                    "record_id_hash",
                    "record_safe_label",
                )
            )
    finally:
        _set_sync_rls(schema_editor, enabled=True)


def decrypt_existing_record_ids(apps, schema_editor):
    FeishuBaseSync = apps.get_model("integrations", "FeishuBaseSync")
    _set_sync_rls(schema_editor, enabled=False)
    try:
        for sync in FeishuBaseSync.objects.exclude(record_id_ciphertext="").iterator(
            chunk_size=500
        ):
            sync.record_id = decrypt_secret(sync.record_id_ciphertext)
            sync.save(update_fields=("record_id",))
    finally:
        _set_sync_rls(schema_editor, enabled=True)


def _set_sync_rls(schema_editor, *, enabled):
    if schema_editor.connection.vendor != "postgresql":
        return
    state = "ENABLE" if enabled else "DISABLE"
    force = " FORCE ROW LEVEL SECURITY" if enabled else ""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"ALTER TABLE integration_feishu_base_sync {state} ROW LEVEL SECURITY"
        )
        if force:
            cursor.execute(
                "ALTER TABLE integration_feishu_base_sync FORCE ROW LEVEL SECURITY"
            )


def install_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_GUARD_SQL)


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_GUARD_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0014_channel_execution_safety"),
    ]

    operations = [
        migrations.AddField(
            model_name="feishubasesync",
            name="execution_request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="feishu_base_syncs",
                to="integrations.externalexecutionrequest",
            ),
        ),
        migrations.AddField(
            model_name="feishubasesync",
            name="record_id_ciphertext",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="feishubasesync",
            name="record_id_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="feishubasesync",
            name="record_safe_label",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AlterField(
            model_name="feishubasesync",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("queued", "Queued"),
                    ("syncing", "Syncing"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                    ("unknown", "Unknown provider outcome"),
                    ("external_erasure_pending", "External erasure pending"),
                    ("external_erasure_completed", "External erasure completed"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
        migrations.RunPython(
            encrypt_existing_record_ids,
            decrypt_existing_record_ids,
        ),
        migrations.RemoveField(
            model_name="feishubasesync",
            name="record_id",
        ),
        migrations.AddIndex(
            model_name="feishubasesync",
            index=models.Index(
                fields=["org", "record_id_hash"],
                name="feishu_record_hash_idx",
            ),
        ),
        migrations.RunPython(install_guard, remove_guard),
    ]
