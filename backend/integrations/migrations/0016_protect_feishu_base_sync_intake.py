import django.db.models.deletion
from django.db import migrations, models


def clear_feishu_secret_hints(apps, schema_editor):
    FeishuBaseConnection = apps.get_model("integrations", "FeishuBaseConnection")
    is_postgres = schema_editor.connection.vendor == "postgresql"
    if is_postgres:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE integration_feishu_base_connection "
                "DISABLE ROW LEVEL SECURITY"
            )
    try:
        FeishuBaseConnection.objects.exclude(app_secret_hint="").update(
            app_secret_hint=""
        )
    finally:
        if is_postgres:
            with schema_editor.connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE integration_feishu_base_connection "
                    "ENABLE ROW LEVEL SECURITY"
                )
                cursor.execute(
                    "ALTER TABLE integration_feishu_base_connection "
                    "FORCE ROW LEVEL SECURITY"
                )


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0015_feishu_base_execution_safety"),
    ]

    operations = [
        migrations.RunPython(clear_feishu_secret_hints, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="feishubasesync",
            name="intake",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="feishu_base_sync",
                to="sdr.leadintake",
            ),
        ),
    ]
