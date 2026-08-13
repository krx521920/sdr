from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0020_compliance_governance"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="sdremailproviderevent",
            old_name="sdr_provider_event_org_type_idx",
            new_name="sdr_event_org_type_idx",
        ),
        migrations.RenameIndex(
            model_name="sdrmodelcredential",
            old_name="sdr_credential_org_provider_idx",
            new_name="sdr_cred_org_provider_idx",
        ),
    ]
