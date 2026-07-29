import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql

RLS_TABLES = ("sdr_intelligence_settings", "sdr_lead_inspection")


def enable_inspector_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in RLS_TABLES:
            cursor.execute(get_enable_policy_sql(table))


def disable_inspector_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in reversed(RLS_TABLES):
            cursor.execute(get_disable_policy_sql(table))


def audit_fields():
    return [
        (
            "created_at",
            models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
        ),
        (
            "updated_at",
            models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
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
    ]


def audit_relationship_fields(*, include_org=True):
    fields = [
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
    ]
    if include_org:
        fields.insert(
            1,
            (
                "org",
                models.ForeignKey(
                    help_text="Organization this record belongs to",
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="%(class)s_set",
                    to="common.org",
                ),
            ),
        )
    return fields


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0002_sdr_routing_rules"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SDRIntelligenceSettings",
            fields=audit_fields()
            + [
                ("is_enabled", models.BooleanField(default=False)),
                ("research_enabled", models.BooleanField(default=True)),
                ("ai_scoring_enabled", models.BooleanField(default=True)),
                (
                    "model",
                    models.CharField(default="gpt-5.6-luna", max_length=100),
                ),
                (
                    "reasoning_effort",
                    models.CharField(
                        choices=[
                            ("none", "None"),
                            ("low", "Low"),
                            ("medium", "Medium"),
                            ("high", "High"),
                            ("xhigh", "Extra high"),
                            ("max", "Maximum"),
                        ],
                        default="low",
                        max_length=16,
                    ),
                ),
                ("icp_description", models.TextField(blank=True)),
                ("positive_signals", models.TextField(blank=True)),
                ("negative_signals", models.TextField(blank=True)),
                (
                    "max_research_pages",
                    models.PositiveSmallIntegerField(
                        default=2,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(3),
                        ],
                    ),
                ),
                (
                    "website_timeout_seconds",
                    models.PositiveSmallIntegerField(
                        default=5,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(15),
                        ],
                    ),
                ),
            ]
            + audit_relationship_fields(include_org=False)
            + [
                (
                    "org",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sdr_intelligence_settings",
                        to="common.org",
                    ),
                )
            ],
            options={"db_table": "sdr_intelligence_settings"},
        ),
        migrations.CreateModel(
            name="LeadInspection",
            fields=audit_fields()
            + [
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("partial", "Partial"),
                            ("failed", "Failed"),
                        ],
                        default="running",
                        max_length=16,
                    ),
                ),
                ("website_url", models.URLField(blank=True, max_length=1000)),
                ("source_urls", models.JSONField(blank=True, default=list)),
                ("research_summary", models.TextField(blank=True)),
                ("research_facts", models.JSONField(blank=True, default=dict)),
                ("content_sha256", models.CharField(blank=True, max_length=64)),
                ("provider", models.CharField(blank=True, max_length=32)),
                ("model", models.CharField(blank=True, max_length=100)),
                ("prompt_version", models.CharField(blank=True, max_length=64)),
                (
                    "configuration_sha256",
                    models.CharField(blank=True, max_length=64),
                ),
                (
                    "provider_response_id",
                    models.CharField(blank=True, max_length=255),
                ),
                (
                    "qualification_score",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("qualification_band", models.CharField(blank=True, max_length=32)),
                (
                    "qualification_reasons",
                    models.JSONField(blank=True, default=list),
                ),
                ("used_fallback", models.BooleanField(default=False)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("error_message", models.CharField(blank=True, max_length=1000)),
                (
                    "input_tokens",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "output_tokens",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ]
            + audit_relationship_fields()
            + [
                (
                    "intake",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inspection",
                        to="sdr.leadintake",
                    ),
                )
            ],
            options={
                "db_table": "sdr_lead_inspection",
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(
                        fields=["org", "status", "-created_at"],
                        name="sdr_inspect_org_status_idx",
                    )
                ],
            },
        ),
        migrations.RunPython(enable_inspector_rls, disable_inspector_rls),
    ]
