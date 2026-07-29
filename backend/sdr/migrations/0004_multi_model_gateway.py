import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql


def enable_gateway_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(get_enable_policy_sql("sdr_model_credential"))


def disable_gateway_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(get_disable_policy_sql("sdr_model_credential"))


PROVIDER_CHOICES = [
    ("openai", "OpenAI"),
    ("doubao", "Doubao / Volcengine Ark"),
    ("deepseek", "DeepSeek"),
]

REASONING_CHOICES = [
    ("none", "None"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra high"),
    ("max", "Maximum"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0003_lead_inspector"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="provider",
            field=models.CharField(
                choices=PROVIDER_CHOICES,
                default="openai",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="fallback_provider",
            field=models.CharField(
                blank=True,
                choices=PROVIDER_CHOICES,
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="fallback_model",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="sdrintelligencesettings",
            name="fallback_reasoning_effort",
            field=models.CharField(
                choices=REASONING_CHOICES,
                default="low",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="leadinspection",
            name="fallback_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "None"),
                    ("model", "Model provider"),
                    ("rules", "Deterministic rules"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="leadinspection",
            name="provider_attempts",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="SDRModelCredential",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        verbose_name="Last Modified At",
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
                ("provider", models.CharField(choices=PROVIDER_CHOICES, max_length=24)),
                ("api_key_ciphertext", models.TextField()),
                ("api_key_hint", models.CharField(blank=True, max_length=12)),
                ("is_active", models.BooleanField(default=True)),
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
                "db_table": "sdr_model_credential",
                "indexes": [
                    models.Index(
                        fields=["org", "provider", "is_active"],
                        name="sdr_credential_org_provider_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("org", "provider"),
                        name="unique_sdr_model_credential_per_org",
                    )
                ],
            },
        ),
        migrations.RunPython(enable_gateway_rls, disable_gateway_rls),
    ]
