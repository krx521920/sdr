import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql

TABLES = (
    "integration_feishu_base_connection",
    "integration_feishu_base_sync",
)


def enable_feishu_base_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(get_enable_policy_sql(table))


def disable_feishu_base_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in reversed(TABLES):
            cursor.execute(get_disable_policy_sql(table))


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0029_delete_sessiontoken"),
        ("integrations", "0011_linkedinconnection_linkedininvitation_and_more"),
        ("sdr", "0019_sdroutboundcampaign_linkedin_invitation_message"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FeishuBaseConnection",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True, verbose_name="Last Modified At"
                    ),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                ("app_id", models.CharField(blank=True, max_length=100)),
                ("app_secret_ciphertext", models.TextField(blank=True)),
                ("app_secret_hint", models.CharField(blank=True, max_length=12)),
                ("app_token", models.CharField(blank=True, max_length=255)),
                ("table_id", models.CharField(blank=True, max_length=255)),
                ("field_mapping", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=False)),
                ("last_validated_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "org",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feishu_base_connection",
                        to="common.org",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={"db_table": "integration_feishu_base_connection"},
        ),
        migrations.CreateModel(
            name="FeishuBaseSync",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True, verbose_name="Last Modified At"
                    ),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("queued", "Queued"),
                            ("syncing", "Syncing"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("record_id", models.CharField(blank=True, max_length=255)),
                ("destination_sha256", models.CharField(max_length=64)),
                ("payload_sha256", models.CharField(max_length=64)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("synced_field_names", models.JSONField(blank=True, default=list)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("error_message", models.CharField(blank=True, max_length=1000)),
                ("last_attempted_at", models.DateTimeField(blank=True, null=True)),
                ("synced_at", models.DateTimeField(blank=True, null=True)),
                ("failed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="syncs",
                        to="integrations.feishubaseconnection",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "intake",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feishu_base_sync",
                        to="sdr.leadintake",
                    ),
                ),
                (
                    "org",
                    models.ForeignKey(
                        help_text="Organization this record belongs to",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(class)s_set",
                        to="common.org",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={
                "db_table": "integration_feishu_base_sync",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="feishubaseconnection",
            index=models.Index(
                fields=["org", "is_active"],
                name="feishu_base_org_active_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="feishubasesync",
            index=models.Index(
                fields=["org", "status", "-created_at"],
                name="feishu_sync_org_status_idx",
            ),
        ),
        migrations.RunPython(enable_feishu_base_rls, disable_feishu_base_rls),
    ]
