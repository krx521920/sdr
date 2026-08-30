from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0012_feishubaseconnection_feishubasesync"),
    ]

    operations = [
        migrations.AlterField(
            model_name="feishubasesync",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("queued", "Queued"),
                    ("syncing", "Syncing"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                    ("external_erasure_pending", "External erasure pending"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
    ]
