from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0023_sdrapollocandidate"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sdrapollocandidate",
            name="status",
            field=models.CharField(
                choices=[
                    (
                        "pending_enrichment_approval",
                        "Pending enrichment approval",
                    ),
                    ("enrichment_reserved", "Enrichment reserved"),
                    ("import_queued", "Person import queued"),
                    ("imported", "Imported"),
                    ("import_review_required", "Import review required"),
                    ("import_failed", "Import failed"),
                    ("import_retry_required", "Import retry required"),
                    ("unknown", "Unknown provider outcome"),
                    ("skipped", "Skipped"),
                ],
                default="pending_enrichment_approval",
                max_length=40,
            ),
        ),
    ]
