import django.db.models.deletion
from django.db import migrations, models


def backfill_webhook_routes(apps, schema_editor):
    """Create the non-RLS bootstrap without bypassing mailbox RLS."""

    Org = apps.get_model("common", "Org")
    InboundMailbox = apps.get_model("cases", "InboundMailbox")
    InboundMailboxWebhookRoute = apps.get_model("cases", "InboundMailboxWebhookRoute")
    db = schema_editor.connection
    previous = ""
    if db.vendor == "postgresql":
        with db.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_org', true)")
            previous = cursor.fetchone()[0] or ""
    try:
        for org_id in Org.objects.values_list("id", flat=True).iterator():
            if db.vendor == "postgresql":
                with db.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('app.current_org', %s, false)",
                        [str(org_id)],
                    )
            routes = [
                InboundMailboxWebhookRoute(
                    mailbox_id=mailbox_id,
                    org_id=org_id,
                )
                for mailbox_id in InboundMailbox.objects.filter(
                    org_id=org_id
                ).values_list("id", flat=True)
            ]
            InboundMailboxWebhookRoute.objects.bulk_create(
                routes,
                ignore_conflicts=True,
            )
    finally:
        if db.vendor == "postgresql":
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_org', %s, false)",
                    [previous],
                )


def remove_webhook_routes(apps, schema_editor):
    InboundMailboxWebhookRoute = apps.get_model("cases", "InboundMailboxWebhookRoute")
    InboundMailboxWebhookRoute.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0024_sdr_inbound_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="inboundmailbox",
            name="sns_topic_arn",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Exact AWS SNS Topic ARN authorized for this mailbox. The ARN "
                    "binds the topic name, AWS account, partition, and region."
                ),
                max_length=512,
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundmailbox",
            constraint=models.UniqueConstraint(
                condition=~models.Q(sns_topic_arn=""),
                fields=("sns_topic_arn",),
                name="uniq_inbound_mailbox_sns_topic",
            ),
        ),
        migrations.CreateModel(
            name="InboundMailboxWebhookRoute",
            fields=[
                (
                    "mailbox",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="webhook_route",
                        serialize=False,
                        to="cases.inboundmailbox",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "org",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inbound_mailbox_webhook_routes",
                        to="common.org",
                    ),
                ),
            ],
            options={
                "db_table": "inbound_mailbox_webhook_route",
                "ordering": ("mailbox_id",),
            },
        ),
        migrations.RunPython(
            backfill_webhook_routes,
            reverse_code=remove_webhook_routes,
        ),
    ]
