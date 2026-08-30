# Durable, tenant-safe Person CSV import pipeline.

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from common.rls import get_disable_policy_sql, get_enable_policy_sql


RLS_TABLES = (
    "matching_person_import_batch",
    "matching_person_import_record",
    "matching_person_import_conflict",
    "matching_person_import_decision",
    "matching_person_import_impact",
    "matching_person_identity_observation",
)


def _base_fields():
    return [
        (
            "created_at",
            models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
        ),
        (
            "updated_at",
            models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
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
            "org",
            models.ForeignKey(
                help_text="Organization this record belongs to",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)s_set",
                to="common.org",
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
    ]


def validate_evidence_source_records(apps, schema_editor):
    """Fail clearly before tightening source records from per-person to global."""

    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        try:
            cursor.execute('ALTER TABLE "matching_evidence" NO FORCE ROW LEVEL SECURITY')
            cursor.execute(
                """
                SELECT org_id, source, source_record_id, COUNT(*)
                FROM matching_evidence
                WHERE source_record_id <> ''
                GROUP BY org_id, source, source_record_id
                HAVING COUNT(*) > 1
                LIMIT 1
                """
            )
            conflict = cursor.fetchone()
        finally:
            cursor.execute('ALTER TABLE "matching_evidence" FORCE ROW LEVEL SECURITY')
    if conflict:
        raise RuntimeError(
            "Cannot install global matching evidence source namespace uniqueness: "
            "duplicate historical (org, source, source_record_id) values exist."
        )


def enable_import_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in RLS_TABLES:
            cursor.execute(get_enable_policy_sql(table))


def disable_import_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in reversed(RLS_TABLES):
            cursor.execute(get_disable_policy_sql(table))


CREATE_IMPORT_GUARDS_SQL = """
CREATE OR REPLACE FUNCTION matching_validate_import_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'matching_person_import_batch' THEN
        IF NEW.requested_by_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM profile
            WHERE id = NEW.requested_by_id AND org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.automation_job_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM automation_job
            WHERE id = NEW.automation_job_id AND org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person_import_record' THEN
        IF NOT EXISTS (
            SELECT 1 FROM matching_person_import_batch
            WHERE id = NEW.batch_id AND org_id = NEW.org_id
        ) OR (NEW.person_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM matching_person
            WHERE id = NEW.person_id AND org_id = NEW.org_id
        )) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person_import_conflict' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM matching_person_import_record r
            WHERE r.id = NEW.record_id
              AND r.batch_id = NEW.batch_id
              AND r.org_id = NEW.org_id
        ) OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(COALESCE(NEW.person_ids, '[]'::jsonb)) p(id)
            WHERE NOT EXISTS (
                SELECT 1 FROM matching_person
                WHERE matching_person.id = p.id::uuid
                  AND matching_person.org_id = NEW.org_id
            )
        ) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person_import_decision' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM matching_person_import_conflict c
            JOIN matching_person_import_record r
              ON r.id = NEW.record_id
             AND r.batch_id = NEW.batch_id
             AND r.org_id = NEW.org_id
            WHERE c.id = NEW.conflict_id
              AND c.record_id = NEW.record_id
              AND c.batch_id = NEW.batch_id
              AND c.org_id = NEW.org_id
        ) OR (NEW.target_person_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM matching_person
            WHERE id = NEW.target_person_id AND org_id = NEW.org_id
        )) OR (NEW.actor_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM profile
            WHERE id = NEW.actor_id AND org_id = NEW.org_id
        )) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person_import_impact' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM matching_person_import_record r
            JOIN matching_person p
              ON p.id = NEW.person_id AND p.org_id = NEW.org_id
            WHERE r.id = NEW.record_id
              AND r.batch_id = NEW.batch_id
              AND r.org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person_identity_observation' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM matching_person_import_record r
            JOIN matching_person p
              ON p.id = NEW.person_id AND p.org_id = NEW.org_id
            JOIN matching_person_identity i
              ON i.id = NEW.identity_id
             AND i.person_id = NEW.person_id
             AND i.org_id = NEW.org_id
            WHERE r.id = NEW.record_id
              AND r.batch_id = NEW.batch_id
              AND r.org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'matching import child organization mismatch'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION matching_reject_import_decision_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'matching import decision is append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER matching_import_batch_org_guard
BEFORE INSERT OR UPDATE ON matching_person_import_batch
FOR EACH ROW EXECUTE FUNCTION matching_validate_import_org();
CREATE TRIGGER matching_import_record_org_guard
BEFORE INSERT OR UPDATE ON matching_person_import_record
FOR EACH ROW EXECUTE FUNCTION matching_validate_import_org();
CREATE TRIGGER matching_import_conflict_org_guard
BEFORE INSERT OR UPDATE ON matching_person_import_conflict
FOR EACH ROW EXECUTE FUNCTION matching_validate_import_org();
CREATE TRIGGER matching_import_decision_org_guard
BEFORE INSERT ON matching_person_import_decision
FOR EACH ROW EXECUTE FUNCTION matching_validate_import_org();
CREATE TRIGGER matching_import_impact_org_guard
BEFORE INSERT OR UPDATE ON matching_person_import_impact
FOR EACH ROW EXECUTE FUNCTION matching_validate_import_org();
CREATE TRIGGER matching_identity_observation_org_guard
BEFORE INSERT OR UPDATE ON matching_person_identity_observation
FOR EACH ROW EXECUTE FUNCTION matching_validate_import_org();
CREATE TRIGGER matching_import_decision_append_only
BEFORE UPDATE OR DELETE ON matching_person_import_decision
FOR EACH ROW EXECUTE FUNCTION matching_reject_import_decision_mutation();
"""


DROP_IMPORT_GUARDS_SQL = """
DROP TRIGGER IF EXISTS matching_import_decision_append_only
    ON matching_person_import_decision;
DROP TRIGGER IF EXISTS matching_identity_observation_org_guard
    ON matching_person_identity_observation;
DROP TRIGGER IF EXISTS matching_import_impact_org_guard
    ON matching_person_import_impact;
DROP TRIGGER IF EXISTS matching_import_decision_org_guard
    ON matching_person_import_decision;
DROP TRIGGER IF EXISTS matching_import_conflict_org_guard
    ON matching_person_import_conflict;
DROP TRIGGER IF EXISTS matching_import_record_org_guard
    ON matching_person_import_record;
DROP TRIGGER IF EXISTS matching_import_batch_org_guard
    ON matching_person_import_batch;
DROP FUNCTION IF EXISTS matching_reject_import_decision_mutation();
DROP FUNCTION IF EXISTS matching_validate_import_org();
"""


def create_import_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_IMPORT_GUARDS_SQL)


def drop_import_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_IMPORT_GUARDS_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("automation", "0001_initial"),
        ("matching", "0003_person_onboarding"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="evidence",
            name="source_namespace",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.RunPython(validate_evidence_source_records, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="evidence",
            name="unique_matching_source_record",
        ),
        migrations.AddConstraint(
            model_name="evidence",
            constraint=models.UniqueConstraint(
                condition=~models.Q(source_record_id=""),
                fields=("org", "source", "source_namespace", "source_record_id"),
                name="unique_matching_source_namespace_record",
            ),
        ),
        migrations.RemoveIndex(
            model_name="evidence",
            name="matching_ev_org_id_6841eb_idx",
        ),
        migrations.AddIndex(
            model_name="evidence",
            index=models.Index(
                fields=["org", "source", "source_namespace", "source_record_id"],
                name="matching_ev_org_id_e6ad45_idx",
            ),
        ),
        migrations.CreateModel(
            name="PersonImportBatch",
            fields=[
                *_base_fields(),
                ("idempotency_key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                ("commit_idempotency_key", models.UUIDField(blank=True, null=True)),
                ("commit_request_hash", models.CharField(blank=True, max_length=64)),
                ("content_hash", models.CharField(max_length=64)),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("original_filename", models.CharField(max_length=255)),
                ("file_size", models.PositiveIntegerField(default=0)),
                ("mapping", models.JSONField(default=dict)),
                ("headers", models.JSONField(default=list)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("crm", "CRM"),
                            ("apollo", "Apollo"),
                            ("linkedin", "LinkedIn"),
                            ("whatsapp", "WhatsApp"),
                            ("wechat", "WeChat"),
                            ("feishu", "Feishu"),
                            ("email", "Email"),
                            ("manual", "Manual"),
                            ("ai", "AI"),
                            ("other", "Other"),
                        ],
                        default="manual",
                        max_length=24,
                    ),
                ),
                ("source_namespace", models.CharField(default="manual:csv", max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("previewed", "Previewed"),
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("partial", "Completed with issues"),
                            ("failed", "Failed"),
                        ],
                        default="previewed",
                        max_length=24,
                    ),
                ),
                ("revision", models.PositiveBigIntegerField(default=0)),
                ("total_count", models.PositiveIntegerField(default=0)),
                ("processed_count", models.PositiveIntegerField(default=0)),
                ("ready_count", models.PositiveIntegerField(default=0)),
                ("created_count", models.PositiveIntegerField(default=0)),
                ("merged_count", models.PositiveIntegerField(default=0)),
                ("conflict_count", models.PositiveIntegerField(default=0)),
                ("invalid_count", models.PositiveIntegerField(default=0)),
                ("skipped_count", models.PositiveIntegerField(default=0)),
                ("replayed_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("match_run_ids", models.JSONField(default=list)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "automation_job",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="person_import_batch",
                        to="automation.automationjob",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="requested_person_imports",
                        to="common.profile",
                    ),
                ),
            ],
            options={
                "db_table": "matching_person_import_batch",
                "ordering": ("-created_at",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("org", "idempotency_key"),
                        name="unique_matching_person_import_key",
                    )
                ],
                "indexes": [
                    models.Index(fields=["org", "status", "-created_at"], name="matching_pe_org_id_85e556_idx"),
                    models.Index(fields=["org", "content_hash"], name="matching_pe_org_id_d70cb8_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="PersonImportRecord",
            fields=[
                *_base_fields(),
                ("row_number", models.PositiveIntegerField()),
                ("row_hash", models.CharField(max_length=64)),
                ("source_record_id", models.CharField(max_length=255)),
                ("display_name", models.CharField(blank=True, max_length=255)),
                ("normalized_payload", models.JSONField(default=dict)),
                ("masked_identities", models.JSONField(default=list)),
                ("field_errors", models.JSONField(default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ready", "Ready"),
                            ("invalid", "Invalid"),
                            ("created", "Created"),
                            ("merged", "Merged"),
                            ("conflict", "Conflict"),
                            ("skipped", "Skipped"),
                            ("replayed", "Replayed"),
                            ("failed", "Failed"),
                        ],
                        default="ready",
                        max_length=24,
                    ),
                ),
                ("revision", models.PositiveBigIntegerField(default=0)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="records",
                        to="matching.personimportbatch",
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="import_records",
                        to="matching.person",
                    ),
                ),
            ],
            options={
                "db_table": "matching_person_import_record",
                "ordering": ("row_number", "id"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("batch", "row_number"),
                        name="unique_matching_import_row_number",
                    )
                ],
                "indexes": [
                    models.Index(fields=["org", "batch", "status", "row_number"], name="matching_pe_org_id_4e5285_idx"),
                    models.Index(fields=["org", "row_hash"], name="matching_pe_org_id_051c5c_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="PersonImportConflict",
            fields=[
                *_base_fields(),
                ("code", models.CharField(max_length=80)),
                ("person_ids", models.JSONField(default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Open"), ("resolved", "Resolved")],
                        default="open",
                        max_length=16,
                    ),
                ),
                ("revision", models.PositiveBigIntegerField(default=0)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conflicts",
                        to="matching.personimportbatch",
                    ),
                ),
                (
                    "record",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conflict",
                        to="matching.personimportrecord",
                    ),
                ),
            ],
            options={
                "db_table": "matching_person_import_conflict",
                "ordering": ("record__row_number",),
                "indexes": [models.Index(fields=["org", "batch", "status"], name="matching_pe_org_id_2135a1_idx")],
            },
        ),
        migrations.CreateModel(
            name="PersonImportImpact",
            fields=[
                *_base_fields(),
                (
                    "impact_type",
                    models.CharField(
                        choices=[("created", "Created"), ("merged", "Merged")],
                        max_length=16,
                    ),
                ),
                ("changed_fields", models.JSONField(default=list)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="impacts",
                        to="matching.personimportbatch",
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="import_impacts",
                        to="matching.person",
                    ),
                ),
                (
                    "record",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="impacts",
                        to="matching.personimportrecord",
                    ),
                ),
            ],
            options={
                "db_table": "matching_person_import_impact",
                "ordering": ("record__row_number",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("org", "batch", "person"),
                        name="unique_matching_import_person_impact",
                    )
                ],
                "indexes": [models.Index(fields=["org", "batch", "person"], name="matching_pe_org_id_a3489c_idx")],
            },
        ),
        migrations.CreateModel(
            name="PersonIdentityObservation",
            fields=[
                *_base_fields(),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("email", "Email"),
                            ("phone", "Phone"),
                            ("linkedin", "LinkedIn"),
                            ("whatsapp", "WhatsApp"),
                            ("wechat", "WeChat"),
                            ("external", "External"),
                        ],
                        max_length=24,
                    ),
                ),
                ("normalized_value_hash", models.CharField(max_length=64)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("crm", "CRM"), ("apollo", "Apollo"),
                            ("linkedin", "LinkedIn"), ("whatsapp", "WhatsApp"),
                            ("wechat", "WeChat"), ("feishu", "Feishu"),
                            ("email", "Email"), ("manual", "Manual"),
                            ("ai", "AI"), ("other", "Other"),
                        ],
                        max_length=24,
                    ),
                ),
                ("source_namespace", models.CharField(max_length=128)),
                ("source_record_id", models.CharField(max_length=255)),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="identity_observations", to="matching.personimportbatch")),
                ("identity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="observations", to="matching.personidentity")),
                ("person", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="identity_observations", to="matching.person")),
                ("record", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="identity_observations", to="matching.personimportrecord")),
            ],
            options={
                "db_table": "matching_person_identity_observation",
                "ordering": ("record__row_number", "kind"),
                "constraints": [
                    models.UniqueConstraint(fields=("batch", "record", "kind", "normalized_value_hash"), name="unique_matching_identity_observation"),
                    models.UniqueConstraint(fields=("org", "identity", "source", "source_namespace", "source_record_id"), name="unique_matching_cross_source_observation"),
                ],
                "indexes": [
                    models.Index(fields=["org", "person", "kind", "-observed_at"], name="matching_pe_org_id_a06049_idx"),
                    models.Index(fields=["org", "source", "source_namespace"], name="matching_pe_org_id_29b853_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="PersonImportDecision",
            fields=[
                *_base_fields(),
                ("action", models.CharField(choices=[("link_existing", "Link existing person"), ("skip", "Skip record")], max_length=24)),
                ("idempotency_key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                ("expected_revision", models.PositiveBigIntegerField()),
                ("resulting_revision", models.PositiveBigIntegerField()),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="person_import_decisions", to="common.profile")),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="decisions", to="matching.personimportbatch")),
                ("conflict", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="decisions", to="matching.personimportconflict")),
                ("record", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="decisions", to="matching.personimportrecord")),
                ("target_person", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="import_decisions", to="matching.person")),
            ],
            options={
                "db_table": "matching_person_import_decision",
                "ordering": ("-created_at",),
                "constraints": [
                    models.UniqueConstraint(fields=("org", "idempotency_key"), name="unique_matching_import_decision_key"),
                    models.UniqueConstraint(fields=("org", "conflict", "resulting_revision"), name="unique_matching_import_decision_revision"),
                    models.CheckConstraint(condition=models.Q(resulting_revision=models.F("expected_revision") + 1), name="matching_import_decision_revision_increments"),
                ],
                "indexes": [models.Index(fields=["org", "conflict", "-created_at"], name="matching_pe_org_id_386f25_idx")],
            },
            bases=(models.Model,),
        ),
        migrations.RunPython(enable_import_rls, disable_import_rls),
        migrations.RunPython(create_import_guards, drop_import_guards),
    ]
