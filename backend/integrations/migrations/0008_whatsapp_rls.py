from django.db import migrations

from common.rls import get_disable_policy_sql, get_enable_policy_sql

WHATSAPP_RLS_TABLES = (
    "integration_whatsapp_connection",
    "integration_whatsapp_message",
)


def enable_whatsapp_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in WHATSAPP_RLS_TABLES:
            cursor.execute(get_enable_policy_sql(table))


def disable_whatsapp_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in reversed(WHATSAPP_RLS_TABLES):
            cursor.execute(get_disable_policy_sql(table))


class Migration(migrations.Migration):
    dependencies = [
        (
            "integrations",
            "0007_whatsappphoneroute_whatsappbusinessconnection_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(enable_whatsapp_rls, disable_whatsapp_rls),
    ]
