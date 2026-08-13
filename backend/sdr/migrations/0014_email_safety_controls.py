import datetime

import django.core.validators
from django.db import migrations, models

import sdr.models


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0013_outbound_campaign_execution"),
    ]

    operations = [
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="email_safety_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="org_daily_send_limit",
            field=models.PositiveIntegerField(
                default=1000,
                help_text=(
                    "Maximum nurture emails sent by the organization per local day."
                ),
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="bounce_rate_threshold",
            field=models.DecimalField(
                decimal_places=2,
                default=5,
                help_text=("Campaign bounce percentage that triggers a safety hold."),
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="complaint_rate_threshold",
            field=models.DecimalField(
                decimal_places=2,
                default=0.1,
                help_text=(
                    "Campaign complaint percentage that triggers a safety hold."
                ),
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="safety_min_sample_size",
            field=models.PositiveIntegerField(
                default=100,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1000000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="safety_window_days",
            field=models.PositiveSmallIntegerField(
                default=7,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(90),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="enforce_recipient_working_hours",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="default_recipient_timezone",
            field=models.CharField(
                default="UTC",
                max_length=64,
                validators=[sdr.models.validate_iana_timezone],
            ),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="recipient_send_window_start",
            field=models.TimeField(default=datetime.time(9, 0)),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="recipient_send_window_end",
            field=models.TimeField(default=datetime.time(17, 0)),
        ),
        migrations.AddField(
            model_name="sdrresponsesettings",
            name="recipient_send_weekdays",
            field=models.JSONField(
                default=sdr.models.default_send_weekdays,
                validators=[sdr.models.validate_send_weekdays],
            ),
        ),
        migrations.AddField(
            model_name="leadnurturedelivery",
            name="deferral_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="safety_hold",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="safety_paused_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="safety_cleared_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="safety_pause_reason",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="sdroutboundcampaign",
            name="safety_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="sdroutboundprospect",
            name="recipient_timezone",
            field=models.CharField(
                blank=True,
                max_length=64,
                validators=[sdr.models.validate_iana_timezone],
            ),
        ),
    ]
