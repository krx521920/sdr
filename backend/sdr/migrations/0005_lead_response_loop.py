import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql

TABLES = (
    "sdr_response_settings",
    "sdr_lead_lifecycle_event",
    "sdr_lead_delivery",
)


def enable_response_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(get_enable_policy_sql(table))


def disable_response_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
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


def audit_relationship_fields():
    return [
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


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0004_multi_model_gateway"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SDRResponseSettings",
            fields=audit_fields()
            + [
                ("acknowledgement_email_enabled", models.BooleanField(default=False)),
                (
                    "acknowledgement_subject",
                    models.CharField(
                        default="Thanks for contacting {{ organization_name }}",
                        max_length=255,
                    ),
                ),
                (
                    "acknowledgement_body",
                    models.TextField(
                        default=(
                            "Hi {{ first_name }},\n\nThanks for contacting "
                            "{{ organization_name }}. We have received your request "
                            "and a member of our team will follow up shortly.\n\nBest,\n"
                            "{{ organization_name }}"
                        )
                    ),
                ),
                (
                    "acknowledgement_from_email",
                    models.EmailField(blank=True, max_length=254),
                ),
                ("sales_in_app_enabled", models.BooleanField(default=True)),
                ("feishu_enabled", models.BooleanField(default=False)),
                ("feishu_webhook_ciphertext", models.TextField(blank=True)),
                ("feishu_webhook_hint", models.CharField(blank=True, max_length=12)),
                (
                    "response_sla_seconds",
                    models.PositiveIntegerField(
                        default=60,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(86400),
                        ],
                    ),
                ),
            ]
            + audit_relationship_fields()
            + [
                (
                    "org",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sdr_response_settings",
                        to="common.org",
                    ),
                )
            ],
            options={"db_table": "sdr_response_settings"},
        ),
        migrations.CreateModel(
            name="LeadLifecycleEvent",
            fields=audit_fields()
            + [
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("received", "Received"),
                            ("queued", "Queued"),
                            ("processing", "Processing"),
                            ("qualified", "Qualified"),
                            ("assigned", "Assigned"),
                            ("crm_handoff", "CRM handoff"),
                            ("acknowledgement_sent", "Acknowledgement sent"),
                            ("sales_notified", "Sales notified"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        max_length=40,
                    ),
                ),
                ("event_key", models.CharField(max_length=120)),
                ("data", models.JSONField(blank=True, default=dict)),
                (
                    "occurred_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
            ]
            + audit_relationship_fields()
            + [
                (
                    "intake",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lifecycle_events",
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
            ],
            options={
                "db_table": "sdr_lead_lifecycle_event",
                "ordering": ("occurred_at", "created_at", "id"),
                "indexes": [
                    models.Index(
                        fields=["org", "event_type", "-occurred_at"],
                        name="sdr_lifecycle_org_type_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("org", "intake", "event_key"),
                        name="unique_sdr_lifecycle_event_key",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="LeadDelivery",
            fields=audit_fields()
            + [
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("acknowledgement_email", "Acknowledgement email"),
                            ("sales_in_app", "Sales in-app notification"),
                            ("sales_feishu", "Sales Feishu notification"),
                        ],
                        max_length=40,
                    ),
                ),
                ("recipient", models.CharField(max_length=500)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sending", "Sending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("last_error_code", models.CharField(blank=True, max_length=80)),
                ("last_error_message", models.TextField(blank=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
            ]
            + audit_relationship_fields()
            + [
                (
                    "intake",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
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
            ],
            options={
                "db_table": "sdr_lead_delivery",
                "ordering": ("created_at", "id"),
                "indexes": [
                    models.Index(
                        fields=["org", "status", "-created_at"],
                        name="sdr_delivery_org_status_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("org", "intake", "kind", "recipient"),
                        name="unique_sdr_lead_delivery",
                    )
                ],
            },
        ),
        migrations.RunPython(enable_response_rls, disable_response_rls),
    ]
