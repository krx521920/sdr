import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("sdr", "0012_outbound_prospecting")]

    operations = [
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="daily_send_limit",
            field=models.PositiveSmallIntegerField(
                default=50,
                help_text=(
                    "Maximum prospects released into the SDR pipeline per local day."
                ),
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="last_refilled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="run_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="sequence",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="outbound_campaigns",
                to="sdr.sdrnurturesequence",
            ),
        ),
        migrations.AddField(
            model_name="sdroutboundprospect",
            name="queued_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sdroutboundprospect",
            name="queued_run",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
