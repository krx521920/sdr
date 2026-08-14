from django.db import migrations, models


INDEX_RENAMES = (
    (
        "sdremailproviderevent",
        "sdr_provider_event_org_type_idx",
        "sdr_event_org_type_idx",
        ["org", "event_type", "-event_at"],
    ),
    (
        "sdrmodelcredential",
        "sdr_credential_org_provider_idx",
        "sdr_cred_org_provider_idx",
        ["org", "provider", "is_active"],
    ),
)


def _ensure_index_names(apps, schema_editor, *, reverse=False):
    for model_name, old_name, new_name, fields in INDEX_RENAMES:
        if reverse:
            old_name, new_name = new_name, old_name

        model = apps.get_model("sdr", model_name)
        with schema_editor.connection.cursor() as cursor:
            constraints = schema_editor.connection.introspection.get_constraints(
                cursor, model._meta.db_table
            )

        if new_name in constraints:
            continue

        old_index = models.Index(fields=fields, name=old_name)
        new_index = models.Index(fields=fields, name=new_name)
        if old_name in constraints:
            schema_editor.rename_index(model, old_index, new_index)
        else:
            # A partially repaired database may have neither name. Recreate the
            # expected index instead of leaving migration state ahead of schema.
            schema_editor.add_index(model, new_index)


def rename_indexes_forward(apps, schema_editor):
    _ensure_index_names(apps, schema_editor)


def rename_indexes_reverse(apps, schema_editor):
    _ensure_index_names(apps, schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0020_compliance_governance"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(rename_indexes_forward, rename_indexes_reverse),
            ],
            state_operations=[
                migrations.RenameIndex(
                    model_name="sdremailproviderevent",
                    old_name="sdr_provider_event_org_type_idx",
                    new_name="sdr_event_org_type_idx",
                ),
                migrations.RenameIndex(
                    model_name="sdrmodelcredential",
                    old_name="sdr_credential_org_provider_idx",
                    new_name="sdr_cred_org_provider_idx",
                ),
            ],
        ),
    ]
