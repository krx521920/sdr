from django.db import migrations, models

CREATE_POSTGRES_GUARDS_SQL = """
CREATE OR REPLACE FUNCTION sdr_reject_compliance_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'SDR compliance events are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER sdr_compliance_event_append_only
BEFORE UPDATE OR DELETE ON sdr_compliance_event
FOR EACH ROW EXECUTE FUNCTION sdr_reject_compliance_event_mutation();

CREATE OR REPLACE FUNCTION sdr_validate_compliance_child_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'sdr_data_provenance' THEN
        IF NOT EXISTS (
            SELECT 1 FROM sdr_lead_intake
            WHERE id = NEW.intake_id AND org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'SDR compliance child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'sdr_compliance_event' THEN
        IF NEW.intake_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM sdr_lead_intake
            WHERE id = NEW.intake_id AND org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'SDR compliance child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.prospect_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM sdr_outbound_prospect
            WHERE id = NEW.prospect_id AND org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'SDR compliance child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER sdr_data_provenance_org_guard
BEFORE INSERT OR UPDATE OF intake_id, org_id ON sdr_data_provenance
FOR EACH ROW EXECUTE FUNCTION sdr_validate_compliance_child_org();

CREATE TRIGGER sdr_compliance_event_org_guard
BEFORE INSERT ON sdr_compliance_event
FOR EACH ROW EXECUTE FUNCTION sdr_validate_compliance_child_org();
"""


DROP_POSTGRES_GUARDS_SQL = """
DROP TRIGGER IF EXISTS sdr_compliance_event_org_guard ON sdr_compliance_event;
DROP TRIGGER IF EXISTS sdr_data_provenance_org_guard ON sdr_data_provenance;
DROP TRIGGER IF EXISTS sdr_compliance_event_append_only ON sdr_compliance_event;
DROP FUNCTION IF EXISTS sdr_validate_compliance_child_org();
DROP FUNCTION IF EXISTS sdr_reject_compliance_event_mutation();
"""


def create_postgres_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_POSTGRES_GUARDS_SQL)


def drop_postgres_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_POSTGRES_GUARDS_SQL)


CHANNEL_CHOICES = [
    ("email", "Email"),
    ("whatsapp", "WhatsApp"),
    ("linkedin", "LinkedIn"),
    ("phone", "Phone"),
    ("wechat", "WeChat"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("sdr", "0021_rename_sdr_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sdrchannelcompliancerule",
            name="channel",
            field=models.CharField(choices=CHANNEL_CHOICES, max_length=16),
        ),
        migrations.AlterField(
            model_name="sdrcomplianceevent",
            name="channel",
            field=models.CharField(
                blank=True,
                choices=CHANNEL_CHOICES,
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="sdrdonotcontactentry",
            name="channel",
            field=models.CharField(choices=CHANNEL_CHOICES, max_length=16),
        ),
        migrations.RunPython(create_postgres_guards, drop_postgres_guards),
    ]
