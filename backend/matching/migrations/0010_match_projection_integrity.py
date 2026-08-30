from django.db import migrations, models
from django.utils import timezone

CREATE_PROJECTION_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION matching_validate_match_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'matching_match' THEN
        IF NOT EXISTS (
            SELECT 1 FROM matching_opportunity o
            WHERE o.id = NEW.opportunity_id AND o.org_id = NEW.org_id
        ) OR NOT EXISTS (
            SELECT 1 FROM matching_person p
            WHERE p.id = NEW.person_id AND p.org_id = NEW.org_id
        ) THEN
            RAISE EXCEPTION 'matching projection organization mismatch'
                USING ERRCODE = '23514';
        END IF;

        IF NEW.projection_state = 'current' THEN
            PERFORM 1
            FROM matching_person p
            WHERE p.id = NEW.person_id
              AND p.org_id = NEW.org_id
              AND p.status = 'active'
              AND p.governance_status = 'active'
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'current match projection requires an active person'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME = 'matching_person' THEN
        IF (NEW.status <> 'active' OR NEW.governance_status <> 'active')
           AND EXISTS (
                SELECT 1 FROM matching_match m
                WHERE m.person_id = NEW.id
                  AND m.org_id = NEW.org_id
                  AND m.projection_state = 'current'
           ) THEN
            RAISE EXCEPTION 'person has a current match projection'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER matching_match_projection_guard
BEFORE INSERT OR UPDATE ON matching_match
FOR EACH ROW EXECUTE FUNCTION matching_validate_match_projection();

CREATE TRIGGER matching_person_projection_guard
BEFORE UPDATE OF status, governance_status ON matching_person
FOR EACH ROW EXECUTE FUNCTION matching_validate_match_projection();
"""


DROP_PROJECTION_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS matching_person_projection_guard ON matching_person;
DROP TRIGGER IF EXISTS matching_match_projection_guard ON matching_match;
DROP FUNCTION IF EXISTS matching_validate_match_projection();
"""


def backfill_projection_state(apps, schema_editor):
    Match = apps.get_model("matching", "Match")
    now = timezone.now()
    ineligible = Match.objects.exclude(
        person__status="active",
        person__governance_status="active",
    )
    for match in ineligible.iterator(chunk_size=500):
        person = match.person
        if person.governance_status != "active":
            reason = f"governance_{person.governance_status}"
        else:
            reason = f"person_status_{person.status}"
        Match.objects.filter(id=match.id).update(
            projection_state="retired",
            retired_at=now,
            retirement_reason=reason[:64],
            rank=None,
        )

    opportunity_ids = (
        Match.objects.filter(projection_state="current")
        .values_list("opportunity_id", flat=True)
        .distinct()
    )
    for opportunity_id in opportunity_ids.iterator(chunk_size=500):
        current = list(
            Match.objects.filter(
                opportunity_id=opportunity_id,
                projection_state="current",
            ).order_by("-overall_score", "-confidence", "person_id")
        )
        for rank, match in enumerate(current, start=1):
            if match.rank != rank:
                Match.objects.filter(id=match.id).update(rank=rank)


def create_projection_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_PROJECTION_GUARDS_SQL)


def drop_projection_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_PROJECTION_GUARDS_SQL)


class Migration(migrations.Migration):
    dependencies = [("matching", "0009_scrub_terminal_import_staging")]

    operations = [
        migrations.AddField(
            model_name="match",
            name="projection_state",
            field=models.CharField(
                choices=[("current", "Current"), ("retired", "Retired")],
                default="current",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="retirement_reason",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="matchrevision",
            name="revision_kind",
            field=models.CharField(
                choices=[
                    ("evaluation", "Evaluation"),
                    ("rerank", "Rerank"),
                    ("retirement", "Retirement"),
                ],
                default="evaluation",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_projection_state, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="match",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        projection_state="current",
                        retired_at__isnull=True,
                        retirement_reason="",
                    )
                    | (
                        models.Q(
                            projection_state="retired",
                            retired_at__isnull=False,
                            rank__isnull=True,
                        )
                        & ~models.Q(retirement_reason="")
                    )
                ),
                name="matching_match_projection_lifecycle",
            ),
        ),
        migrations.AddConstraint(
            model_name="match",
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(projection_state="current")
                    & models.Q(rank__isnull=False)
                ),
                fields=("org", "opportunity", "rank"),
                name="unique_current_match_rank_per_opportunity",
            ),
        ),
        migrations.AddIndex(
            model_name="match",
            index=models.Index(
                fields=["org", "opportunity", "projection_state", "rank"],
                name="matching_current_rank_idx",
            ),
        ),
        migrations.RunPython(create_projection_guards, drop_projection_guards),
    ]
