import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql

TABLE = "sdr_apollo_candidate"


CREATE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION sdr_validate_apollo_candidate_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM sdr_outbound_source
        WHERE id = NEW.source_id AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Apollo candidate source organization mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM integration_external_execution_request
        WHERE id = NEW.search_request_id
          AND org_id = NEW.org_id
          AND channel = 'apollo'
          AND action = 'search_people'
    ) THEN
        RAISE EXCEPTION 'Apollo candidate search request mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.enrichment_request_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM integration_external_execution_request
        WHERE id = NEW.enrichment_request_id
          AND org_id = NEW.org_id
          AND channel = 'apollo'
          AND action = 'enrich_person'
    ) THEN
        RAISE EXCEPTION 'Apollo candidate enrichment request mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.import_batch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM matching_person_import_batch
        WHERE id = NEW.import_batch_id AND org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Apollo candidate import batch organization mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER sdr_apollo_candidate_org_guard
BEFORE INSERT OR UPDATE OF org_id, source_id, search_request_id,
    enrichment_request_id, import_batch_id
ON sdr_apollo_candidate
FOR EACH ROW EXECUTE FUNCTION sdr_validate_apollo_candidate_org();
"""


DROP_GUARD_SQL = """
DROP TRIGGER IF EXISTS sdr_apollo_candidate_org_guard ON sdr_apollo_candidate;
DROP FUNCTION IF EXISTS sdr_validate_apollo_candidate_org();
"""


def enable_security(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(get_enable_policy_sql(TABLE))
        cursor.execute(CREATE_GUARD_SQL)


def disable_security(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_GUARD_SQL)
        cursor.execute(get_disable_policy_sql(TABLE))


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0014_channel_execution_safety"),
        ("matching", "0008_scoring_policy"),
        ("sdr", "0022_compliance_audit_guards_and_wechat"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SDRApolloCandidate",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True, verbose_name="Last Modified At"
                    ),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                ("provider_person_id_ciphertext", models.TextField()),
                ("provider_person_id_hash", models.CharField(max_length=64)),
                ("safe_label", models.CharField(max_length=120)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            (
                                "pending_enrichment_approval",
                                "Pending enrichment approval",
                            ),
                            ("enrichment_reserved", "Enrichment reserved"),
                            ("import_queued", "Person import queued"),
                            ("imported", "Imported"),
                            ("unknown", "Unknown provider outcome"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending_enrichment_approval",
                        max_length=40,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "enrichment_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="apollo_enrichment_candidates",
                        to="integrations.externalexecutionrequest",
                    ),
                ),
                (
                    "import_batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="apollo_candidates",
                        to="matching.personimportbatch",
                    ),
                ),
                (
                    "org",
                    models.ForeignKey(
                        help_text="Organization this record belongs to",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(class)s_set",
                        to="common.org",
                    ),
                ),
                (
                    "search_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="apollo_search_candidates",
                        to="integrations.externalexecutionrequest",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="apollo_candidates",
                        to="sdr.sdroutboundsource",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={
                "db_table": TABLE,
                "ordering": ("created_at", "id"),
                "indexes": [
                    models.Index(
                        fields=["org", "source", "status"],
                        name="sdr_apollo_cand_status_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("org", "source", "provider_person_id_hash"),
                        name="unique_sdr_apollo_candidate",
                    )
                ],
            },
        ),
        migrations.RunPython(enable_security, disable_security),
    ]
