from django.db import migrations


TERMINAL_STATUSES = ("created", "merged", "replayed", "skipped", "failed")


def scrub_terminal_import_staging(apps, schema_editor):
    PersonImportRecord = apps.get_model("matching", "PersonImportRecord")

    # Terminal rows retain their immutable ledger identity and relationships but
    # no longer need the executable normalized payload used by the worker.
    PersonImportRecord.objects.filter(status__in=TERMINAL_STATUSES).update(
        normalized_payload={}
    )

    # Apply the stronger graph-erasure projection to people anonymized before
    # this release. Provider/source IDs remain as non-addressable audit keys.
    PersonImportRecord.objects.filter(
        person__governance_status="anonymized"
    ).update(
        display_name="",
        normalized_payload={},
        masked_identities=[],
        field_errors=[],
    )


class Migration(migrations.Migration):
    dependencies = [("matching", "0008_scoring_policy")]

    operations = [
        migrations.RunPython(
            scrub_terminal_import_staging,
            reverse_code=migrations.RunPython.noop,
        )
    ]
