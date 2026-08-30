import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql

TABLES = (
    "integration_channel_execution_control",
    "integration_organization_execution_control",
    "integration_channel_test_target",
    "integration_channel_execution_approval",
    "integration_external_execution_request",
)


def enable_security(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(get_enable_policy_sql(table))
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION integration_execution_child_org_guard()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE child_org uuid;
            BEGIN
              IF TG_TABLE_NAME = 'integration_channel_execution_approval' THEN
                IF NEW.approved_by_id IS NOT NULL THEN
                  SELECT org_id INTO child_org FROM profile WHERE id = NEW.approved_by_id;
                  IF child_org IS DISTINCT FROM NEW.org_id THEN
                    RAISE EXCEPTION 'integration execution child organization mismatch';
                  END IF;
                END IF;
                IF NEW.consumed_by_request_id IS NOT NULL THEN
                  SELECT org_id INTO child_org FROM integration_external_execution_request
                    WHERE id = NEW.consumed_by_request_id;
                  IF child_org IS DISTINCT FROM NEW.org_id THEN
                    RAISE EXCEPTION 'integration execution child organization mismatch';
                  END IF;
                END IF;
              ELSIF TG_TABLE_NAME = 'integration_external_execution_request' THEN
                SELECT org_id INTO child_org FROM integration_channel_execution_approval
                  WHERE id = NEW.approval_id;
                IF child_org IS DISTINCT FROM NEW.org_id THEN
                  RAISE EXCEPTION 'integration execution child organization mismatch';
                END IF;
              END IF;
              RETURN NEW;
            END $$;
            """
        )
        for table in (
            "integration_channel_execution_approval",
            "integration_external_execution_request",
        ):
            cursor.execute(
                f"""
                CREATE TRIGGER {table}_org_guard
                BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION integration_execution_child_org_guard();
                """
            )


def disable_security(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in reversed(TABLES):
            cursor.execute(get_disable_policy_sql(table))
        cursor.execute("DROP FUNCTION IF EXISTS integration_execution_child_org_guard() CASCADE")


def audit_fields():
    return [
        ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
        ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Last Modified At")),
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
        (
            "org",
            models.ForeignKey(
                help_text="Organization this record belongs to",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)s_set",
                to="common.org",
            ),
        ),
    ]


CHANNELS = [
    ("email", "Email"),
    ("whatsapp", "WhatsApp"),
    ("linkedin", "LinkedIn"),
    ("feishu", "Feishu"),
    ("apollo", "Apollo"),
    ("facebook", "Facebook"),
    ("wechat", "WeChat"),
    ("wecom", "WeCom"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0029_delete_sessiontoken"),
        ("integrations", "0013_feishubasesync_external_erasure_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChannelExecutionControl",
            fields=audit_fields()
            + [
                ("channel", models.CharField(choices=CHANNELS, max_length=24)),
                ("enabled", models.BooleanField(default=False)),
                ("test_mode", models.BooleanField(default=True)),
                ("daily_limit", models.PositiveIntegerField(default=0)),
                ("per_execution_limit", models.PositiveIntegerField(default=0)),
                ("usage_date", models.DateField(blank=True, null=True)),
                ("reserved_units", models.PositiveIntegerField(default=0)),
                ("consumed_units", models.PositiveIntegerField(default=0)),
                ("revision", models.PositiveBigIntegerField(default=0)),
            ],
            options={"db_table": "integration_channel_execution_control"},
        ),
        migrations.CreateModel(
            name="ChannelTestTarget",
            fields=audit_fields()
            + [
                ("channel", models.CharField(choices=CHANNELS, max_length=24)),
                ("identifier_hash", models.CharField(max_length=64)),
                ("safe_label", models.CharField(max_length=120)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"db_table": "integration_channel_test_target"},
        ),
        migrations.CreateModel(
            name="OrganizationExecutionControl",
            fields=audit_fields()
            + [
                ("enabled", models.BooleanField(default=False)),
                ("daily_limit", models.PositiveIntegerField(default=0)),
                ("usage_date", models.DateField(blank=True, null=True)),
                ("reserved_units", models.PositiveIntegerField(default=0)),
                ("consumed_units", models.PositiveIntegerField(default=0)),
                ("revision", models.PositiveBigIntegerField(default=0)),
            ],
            options={"db_table": "integration_organization_execution_control"},
        ),
        migrations.CreateModel(
            name="ChannelExecutionApproval",
            fields=audit_fields()
            + [
                ("channel", models.CharField(choices=CHANNELS, max_length=24)),
                ("idempotency_key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                ("action", models.CharField(max_length=64)),
                ("target_hash", models.CharField(max_length=64)),
                ("payload_hash", models.CharField(max_length=64)),
                ("units", models.PositiveIntegerField(default=1)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="channel_approvals",
                        to="common.profile",
                    ),
                ),
            ],
            options={"db_table": "integration_channel_execution_approval"},
        ),
        migrations.CreateModel(
            name="ExternalExecutionRequest",
            fields=audit_fields()
            + [
                ("channel", models.CharField(choices=CHANNELS, max_length=24)),
                ("action", models.CharField(max_length=64)),
                ("idempotency_key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                ("target_hash", models.CharField(max_length=64)),
                ("payload_hash", models.CharField(max_length=64)),
                ("units", models.PositiveIntegerField(default=1)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("reserved", "Reserved"),
                            ("sending", "Sending"),
                            ("accepted", "Accepted"),
                            ("delivered", "Delivered"),
                            ("failed", "Failed"),
                            ("unknown", "Unknown"),
                        ],
                        default="reserved",
                        max_length=24,
                    ),
                ),
                ("provider_reference_hash", models.CharField(blank=True, max_length=64)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("reserved_at", models.DateTimeField()),
                ("sending_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("failed_at", models.DateTimeField(blank=True, null=True)),
                ("unknown_at", models.DateTimeField(blank=True, null=True)),
                (
                    "approval",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="requests",
                        to="integrations.channelexecutionapproval",
                    ),
                ),
            ],
            options={"db_table": "integration_external_execution_request"},
        ),
        migrations.AddField(
            model_name="channelexecutionapproval",
            name="consumed_by_request",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="consumed_approval",
                to="integrations.externalexecutionrequest",
            ),
        ),
        migrations.AddConstraint(
            model_name="channelexecutioncontrol",
            constraint=models.UniqueConstraint(
                fields=("org", "channel"), name="unique_integration_channel_control"
            ),
        ),
        migrations.AddIndex(
            model_name="channelexecutioncontrol",
            index=models.Index(fields=["org", "channel", "enabled"], name="int_ch_ctrl_idx"),
        ),
        migrations.AddConstraint(
            model_name="channeltesttarget",
            constraint=models.UniqueConstraint(
                fields=("org", "channel", "identifier_hash"),
                name="unique_integration_test_target",
            ),
        ),
        migrations.AddConstraint(
            model_name="organizationexecutioncontrol",
            constraint=models.UniqueConstraint(
                fields=("org",), name="unique_integration_organization_control"
            ),
        ),
        migrations.AddIndex(
            model_name="channeltesttarget",
            index=models.Index(fields=["org", "channel", "is_active"], name="int_test_target_idx"),
        ),
        migrations.AddIndex(
            model_name="channelexecutionapproval",
            index=models.Index(fields=["org", "channel", "expires_at"], name="int_approval_exp_idx"),
        ),
        migrations.AddConstraint(
            model_name="channelexecutionapproval",
            constraint=models.UniqueConstraint(
                fields=("org", "idempotency_key"),
                name="unique_integration_execution_approval_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="externalexecutionrequest",
            constraint=models.UniqueConstraint(
                fields=("org", "channel", "idempotency_key"),
                name="unique_integration_execution_request",
            ),
        ),
        migrations.AddIndex(
            model_name="externalexecutionrequest",
            index=models.Index(
                fields=["org", "channel", "status", "-created_at"],
                name="int_exec_status_idx",
            ),
        ),
        migrations.RunPython(enable_security, disable_security),
    ]
