import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql

RLS_TABLES = (
    "sdr_routing_rule",
    "sdr_routing_rule_member",
    "sdr_routing_rule_state",
)


def enable_sdr_routing_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in RLS_TABLES:
            cursor.execute(get_enable_policy_sql(table))


def disable_sdr_routing_rls(apps, schema_editor):
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
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SDRRoutingRule",
            fields=audit_fields()
            + [
                ("name", models.CharField(max_length=160)),
                ("priority", models.PositiveIntegerField(default=100)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "strategy",
                    models.CharField(
                        choices=[
                            ("least_loaded", "Least loaded"),
                            ("round_robin", "Round robin"),
                            ("direct", "Direct"),
                        ],
                        default="least_loaded",
                        max_length=24,
                    ),
                ),
                ("countries", models.JSONField(blank=True, default=list)),
                ("sources", models.JSONField(blank=True, default=list)),
                (
                    "qualification_bands",
                    models.JSONField(blank=True, default=list),
                ),
            ]
            + audit_relationship_fields(),
            options={
                "db_table": "sdr_routing_rule",
                "ordering": ("priority", "created_at", "id"),
                "indexes": [
                    models.Index(
                        fields=["org", "is_active", "priority"],
                        name="sdr_rule_org_active_idx",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="SDRRoutingRuleMember",
            fields=audit_fields()
            + [("position", models.PositiveIntegerField(default=0))]
            + audit_relationship_fields()
            + [
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sdr_routing_memberships",
                        to="common.profile",
                    ),
                ),
                (
                    "rule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="members",
                        to="sdr.sdrroutingrule",
                    ),
                ),
            ],
            options={
                "db_table": "sdr_routing_rule_member",
                "ordering": ("position", "created_at", "id"),
                "indexes": [
                    models.Index(
                        fields=["org", "rule", "position"],
                        name="sdr_member_org_rule_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("rule", "profile"),
                        name="unique_profile_per_sdr_routing_rule",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="SDRRoutingRuleState",
            fields=audit_fields()
            + [("next_index", models.PositiveBigIntegerField(default=0))]
            + audit_relationship_fields()
            + [
                (
                    "rule",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="state",
                        to="sdr.sdrroutingrule",
                    ),
                )
            ],
            options={"db_table": "sdr_routing_rule_state"},
        ),
        migrations.AddField(
            model_name="leadintake",
            name="routing_reason",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="leadintake",
            name="routing_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="lead_intakes",
                to="sdr.sdrroutingrule",
            ),
        ),
        migrations.RunPython(enable_sdr_routing_rls, disable_sdr_routing_rls),
    ]
