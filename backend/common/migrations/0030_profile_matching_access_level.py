from django.db import migrations, models


def backfill_matching_access(apps, schema_editor):
    Profile = apps.get_model("common", "Profile")
    Profile.objects.filter(
        models.Q(role="ADMIN") | models.Q(is_organization_admin=True)
    ).update(matching_access_level="decide")
    Profile.objects.filter(
        has_sales_access=True,
    ).exclude(
        models.Q(role="ADMIN") | models.Q(is_organization_admin=True)
    ).update(matching_access_level="read")


def reset_matching_access(apps, schema_editor):
    Profile = apps.get_model("common", "Profile")
    Profile.objects.update(matching_access_level="none")


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0029_delete_sessiontoken"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="matching_access_level",
            field=models.CharField(
                choices=[
                    ("none", "None"),
                    ("read", "Read"),
                    ("manage", "Manage"),
                    ("recompute", "Recompute"),
                    ("decide", "Decide"),
                ],
                default="none",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_matching_access, reset_matching_access),
    ]
