import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0023_case_merge_record"),
    ]

    operations = [
        migrations.AddField(
            model_name="emailmessage",
            name="from_display_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="emailmessage",
            name="mailbox",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="email_messages",
                to="cases.inboundmailbox",
            ),
        ),
        migrations.AddField(
            model_name="inboundmailbox",
            name="route_target",
            field=models.CharField(
                choices=[
                    ("case", "Support tickets"),
                    ("sdr", "SDR leads and replies"),
                ],
                default="case",
                help_text=(
                    "Choose whether accepted messages create tickets or enter SDR."
                ),
                max_length=16,
            ),
        ),
    ]
