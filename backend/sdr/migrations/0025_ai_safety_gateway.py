# Generated for the unified, fail-closed AI safety gateway.

import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import sdr.models
from common.rls import get_disable_policy_sql, get_enable_policy_sql


def enable_ai_audit_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(get_enable_policy_sql("sdr_ai_call_audit"))


def disable_ai_audit_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(get_disable_policy_sql("sdr_ai_call_audit"))


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0030_profile_matching_access_level"),
        ("sdr", "0024_alter_sdrapollocandidate_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="ai_audit_retention_days",
            field=models.PositiveSmallIntegerField(
                default=90,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="allowed_ai_providers",
            field=models.JSONField(default=sdr.models.default_ai_providers),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="allowed_ai_purposes",
            field=models.JSONField(default=sdr.models.default_ai_purposes),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="max_ai_input_chars",
            field=models.PositiveIntegerField(
                default=30000,
                validators=[
                    django.core.validators.MinValueValidator(1000),
                    django.core.validators.MaxValueValidator(200000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="max_ai_input_tokens",
            field=models.PositiveIntegerField(
                default=30000,
                validators=[
                    django.core.validators.MinValueValidator(256),
                    django.core.validators.MaxValueValidator(100000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="pii_handling",
            field=models.CharField(
                choices=[
                    ("redact", "Redact"),
                    ("block", "Block"),
                    ("allow", "Allow with tenant approval"),
                ],
                default="redact",
                max_length=12,
            ),
        ),
        migrations.CreateModel(
            name="SDRAICallAudit",
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
                ("request_id", models.UUIDField(db_index=True, default=uuid.uuid4)),
                ("purpose", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("provider", models.CharField(blank=True, max_length=24)),
                ("model", models.CharField(blank=True, max_length=100)),
                ("credential_source", models.CharField(blank=True, max_length=16)),
                ("route_index", models.PositiveSmallIntegerField(default=0)),
                ("prompt_version", models.CharField(max_length=100)),
                ("configuration_sha256", models.CharField(max_length=64)),
                ("input_sha256", models.CharField(blank=True, max_length=64)),
                ("field_paths", models.JSONField(blank=True, default=list)),
                ("pii_findings", models.JSONField(blank=True, default=dict)),
                ("redaction_count", models.PositiveIntegerField(default=0)),
                ("input_chars", models.PositiveIntegerField(default=0)),
                ("estimated_input_tokens", models.PositiveIntegerField(default=0)),
                ("input_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("output_tokens", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "estimated_cost_microusd",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                ("latency_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("response_id_sha256", models.CharField(blank=True, max_length=64)),
                ("failure_code", models.CharField(blank=True, max_length=100)),
                ("failure_reason", models.CharField(blank=True, max_length=500)),
                ("fallback_used", models.BooleanField(default=False)),
                ("retention_expires_at", models.DateTimeField()),
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
                "db_table": "sdr_ai_call_audit",
                "ordering": ("-created_at", "id"),
                "indexes": [
                    models.Index(
                        fields=["org", "purpose", "-created_at"],
                        name="sdr_ai_audit_purpose_idx",
                    ),
                    models.Index(
                        fields=["org", "status", "-created_at"],
                        name="sdr_ai_audit_status_idx",
                    ),
                ],
            },
        ),
        migrations.RunPython(enable_ai_audit_rls, disable_ai_audit_rls),
    ]
