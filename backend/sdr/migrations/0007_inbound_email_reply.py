from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0024_sdr_inbound_email"),
        ("sdr", "0006_lead_nurturing"),
    ]

    operations = [
        migrations.AddField(
            model_name="leadnurturedelivery",
            name="reply_message_id",
            field=models.CharField(blank=True, max_length=512),
        ),
    ]
