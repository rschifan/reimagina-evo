"""Indicator layer: jobs_runtime.job_run, indicator_catalog, indicator_dependency,
indicator_result, serving_geo.current_indicator_v, and supporting triggers.

Brings the Layer 2 contract documented in INDICATORS_LAYER.md + JOB_LAYER.md to
Postgres.

scenario_id columns are plain uuid with NO FK — app_core.scenario lands with
Layer 3 and the FK is added together with the scenario table. indicator_result
gets a CASCADE check that drops scenario rows via indicator_result.scenario_id
in the Layer 3 migration.

Revision ID: r016
Revises: r015
Create Date: 2026-04-19
"""

from __future__ import annotations

from alembic import op

revision = "r016"
down_revision = "r015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── jobs_runtime.job_run ───────────────────────────────────────────────
    op.execute("""
        CREATE TABLE jobs_runtime.job_run (
            id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            scenario_id   uuid,
            model_family  text        NOT NULL,
            status        text        NOT NULL
                                      CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
            code_version  text        NOT NULL,
            queued_at     timestamptz NOT NULL DEFAULT now(),
            started_at    timestamptz,
            completed_at  timestamptz,
            error_message text,
            CHECK (started_at IS NULL OR started_at >= queued_at),
            CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
        )
    """)
    op.execute(
        "CREATE INDEX job_run_scenario_idx ON jobs_runtime.job_run (scenario_id)"
    )
    op.execute("CREATE INDEX job_run_status_idx   ON jobs_runtime.job_run (status)")
    op.execute(
        "CREATE INDEX job_run_family_idx   ON jobs_runtime.job_run (model_family, status)"
    )

    # ── indicator_catalog ──────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE curated_geo.indicator_catalog (
            id                 uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
            slug               text             NOT NULL UNIQUE,
            name               text             NOT NULL,
            description        text,
            model_family       text,
            computation_kind   text             NOT NULL
                                                CHECK (computation_kind IN ('direct','composite')),
            value_type         text             NOT NULL
                                                CHECK (value_type IN ('numeric','text','json')),
            unit               text,
            valid_min          double precision,
            valid_max          double precision,
            aggregation_method text             NOT NULL
                                                CHECK (aggregation_method IN
                                                  ('mean','weighted_mean','sum','min','max','none')),
            applies_to         text[]           NOT NULL,
            created_at         timestamptz      NOT NULL DEFAULT now(),
            CHECK (cardinality(applies_to) > 0),
            CHECK (valid_min IS NULL OR valid_max IS NULL OR valid_min <= valid_max),
            CHECK (value_type = 'numeric' OR (valid_min IS NULL AND valid_max IS NULL)),
            CHECK (
                (computation_kind = 'direct'    AND model_family IS NOT NULL) OR
                (computation_kind = 'composite' AND model_family IS NULL)
            )
        )
    """)

    # ── indicator_dependency (composite indicator DAG) ─────────────────────
    op.execute("""
        CREATE TABLE curated_geo.indicator_dependency (
            indicator_id   uuid             NOT NULL
                                            REFERENCES curated_geo.indicator_catalog(id) ON DELETE CASCADE,
            depends_on_id  uuid             NOT NULL
                                            REFERENCES curated_geo.indicator_catalog(id) ON DELETE RESTRICT,
            weight         double precision,
            PRIMARY KEY (indicator_id, depends_on_id),
            CHECK (indicator_id <> depends_on_id),
            CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1))
        )
    """)

    # Deferred trigger: verifies acyclicity, composite parent, and weight sum
    # after all rows for a given indicator are inserted in the same txn.
    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.indicator_dependency_validate()
        RETURNS TRIGGER AS $$
        DECLARE
            parent_kind text;
            weighted_count int;
            null_weight_count int;
            weight_sum double precision;
            cycle_exists boolean;
        BEGIN
            FOR parent_kind IN
                SELECT ic.computation_kind
                FROM curated_geo.indicator_catalog ic
                JOIN (
                    SELECT DISTINCT indicator_id
                    FROM curated_geo.indicator_dependency
                ) dep ON dep.indicator_id = ic.id
            LOOP
                IF parent_kind <> 'composite' THEN
                    RAISE EXCEPTION 'indicator_dependency.indicator_id must reference a composite indicator';
                END IF;
            END LOOP;

            WITH RECURSIVE dep_graph(indicator_id, depends_on_id, depth) AS (
                SELECT indicator_id, depends_on_id, 1
                FROM curated_geo.indicator_dependency
                UNION ALL
                SELECT d.indicator_id, dg.depends_on_id, dg.depth + 1
                FROM curated_geo.indicator_dependency d
                JOIN dep_graph dg ON dg.indicator_id = d.depends_on_id
                WHERE dg.depth < 50
            )
            SELECT EXISTS(
                SELECT 1 FROM dep_graph WHERE indicator_id = depends_on_id
            ) INTO cycle_exists;
            IF cycle_exists THEN
                RAISE EXCEPTION 'indicator_dependency contains a cycle';
            END IF;

            FOR weighted_count, null_weight_count, weight_sum IN
                SELECT
                    COUNT(*) FILTER (WHERE weight IS NOT NULL),
                    COUNT(*) FILTER (WHERE weight IS NULL),
                    COALESCE(SUM(weight), 0)
                FROM curated_geo.indicator_dependency
                GROUP BY indicator_id
            LOOP
                IF weighted_count > 0 AND null_weight_count > 0 THEN
                    RAISE EXCEPTION 'indicator_dependency: mix of weighted and null-weight rows for one parent';
                END IF;
                IF weighted_count > 0 AND abs(weight_sum - 1.0) > 1e-9 THEN
                    RAISE EXCEPTION 'indicator_dependency: weights must sum to 1.0 (got %)', weight_sum;
                END IF;
            END LOOP;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER indicator_dependency_validate_trigger
        AFTER INSERT OR UPDATE OR DELETE ON curated_geo.indicator_dependency
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION curated_geo.indicator_dependency_validate()
    """)

    # ── indicator_result ───────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE curated_geo.indicator_result (
            id              uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
            feature_id      uuid             NOT NULL
                                             REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
            feature_type    text             NOT NULL,
            scenario_id     uuid,
            indicator_id    uuid             NOT NULL REFERENCES curated_geo.indicator_catalog(id),
            indicator_slug  text             NOT NULL,
            job_run_id      uuid             NOT NULL
                                             REFERENCES jobs_runtime.job_run(id) ON DELETE CASCADE,
            model_version   text,
            value_numeric   double precision,
            value_text      text,
            value_json      jsonb,
            quality_flag    text             NOT NULL DEFAULT 'measured'
                                             CHECK (quality_flag IN
                                               ('measured','estimated','imputed','missing','not_applicable')),
            confidence      double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            missing_reason  text,
            status          text             NOT NULL DEFAULT 'current'
                                             CHECK (status IN ('current','stale','failed')),
            computed_at     timestamptz      NOT NULL DEFAULT now(),
            updated_at      timestamptz      NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ir_one_current_base
          ON curated_geo.indicator_result (feature_id, indicator_id)
          WHERE scenario_id IS NULL AND status = 'current'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ir_one_current_scenario
          ON curated_geo.indicator_result (feature_id, scenario_id, indicator_id)
          WHERE scenario_id IS NOT NULL AND status = 'current'
    """)
    op.execute("""
        CREATE INDEX ir_feature_scenario_idx
          ON curated_geo.indicator_result (feature_id, scenario_id, indicator_id)
          WHERE status = 'current'
    """)
    op.execute("""
        CREATE INDEX ir_scenario_slug_idx
          ON curated_geo.indicator_result (scenario_id, indicator_slug)
          WHERE status = 'current'
    """)
    op.execute("""
        CREATE INDEX ir_base_slug_idx
          ON curated_geo.indicator_result (indicator_slug, feature_id)
          WHERE scenario_id IS NULL AND status = 'current'
    """)
    op.execute("""
        CREATE INDEX ir_stale_idx
          ON curated_geo.indicator_result (status)
          WHERE status = 'stale'
    """)
    op.execute("""
        CREATE INDEX ir_job_run_idx
          ON curated_geo.indicator_result (job_run_id)
    """)

    # Before-write trigger: denormalisation + business-rule validation.
    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.indicator_result_before_write()
        RETURNS TRIGGER AS $$
        DECLARE
            feat_type text;
            catalog_slug text;
            catalog_applies text[];
            catalog_valid_min double precision;
            catalog_valid_max double precision;
            run_scenario uuid;
            run_code_version text;
            values_set int;
        BEGIN
            SELECT type_slug INTO feat_type
            FROM curated_geo.feature WHERE id = NEW.feature_id;
            IF feat_type IS NULL THEN
                RAISE EXCEPTION 'indicator_result.feature_id % not found', NEW.feature_id;
            END IF;

            SELECT slug, applies_to, valid_min, valid_max
              INTO catalog_slug, catalog_applies, catalog_valid_min, catalog_valid_max
            FROM curated_geo.indicator_catalog WHERE id = NEW.indicator_id;
            IF catalog_slug IS NULL THEN
                RAISE EXCEPTION 'indicator_result.indicator_id % not found', NEW.indicator_id;
            END IF;

            SELECT scenario_id, code_version INTO run_scenario, run_code_version
            FROM jobs_runtime.job_run WHERE id = NEW.job_run_id;
            IF run_code_version IS NULL THEN
                RAISE EXCEPTION 'indicator_result.job_run_id % not found', NEW.job_run_id;
            END IF;

            IF TG_OP = 'INSERT' THEN
                NEW.feature_type  := feat_type;
                NEW.indicator_slug := catalog_slug;
                IF NEW.model_version IS NULL THEN
                    NEW.model_version := run_code_version;
                END IF;
            ELSE
                IF NEW.feature_type    <> OLD.feature_type    THEN
                    RAISE EXCEPTION 'feature_type is immutable after insert';
                END IF;
                IF NEW.indicator_slug  <> OLD.indicator_slug  THEN
                    RAISE EXCEPTION 'indicator_slug is immutable after insert';
                END IF;
                NEW.updated_at := now();
            END IF;

            IF NEW.scenario_id IS DISTINCT FROM run_scenario THEN
                RAISE EXCEPTION 'indicator_result.scenario_id (%) must match job_run.scenario_id (%)',
                    NEW.scenario_id, run_scenario;
            END IF;

            IF NEW.quality_flag <> 'not_applicable' AND NOT (feat_type = ANY(catalog_applies)) THEN
                RAISE EXCEPTION
                    'feature type % is not in indicator applies_to (%); write quality_flag=''not_applicable'' to suppress',
                    feat_type, catalog_applies;
            END IF;

            values_set := (CASE WHEN NEW.value_numeric IS NOT NULL THEN 1 ELSE 0 END)
                        + (CASE WHEN NEW.value_text    IS NOT NULL THEN 1 ELSE 0 END)
                        + (CASE WHEN NEW.value_json    IS NOT NULL THEN 1 ELSE 0 END);

            IF NEW.quality_flag IN ('measured','estimated','imputed') AND NEW.status <> 'failed' THEN
                IF values_set <> 1 THEN
                    RAISE EXCEPTION 'indicator_result: exactly one value column must be set for quality_flag=%', NEW.quality_flag;
                END IF;
            ELSE
                IF values_set <> 0 THEN
                    RAISE EXCEPTION 'indicator_result: all value columns must be NULL for quality_flag=% / status=%',
                        NEW.quality_flag, NEW.status;
                END IF;
            END IF;

            IF NEW.quality_flag = 'missing' AND (NEW.missing_reason IS NULL OR NEW.missing_reason = '') THEN
                RAISE EXCEPTION 'indicator_result.missing_reason is required when quality_flag=''missing''';
            END IF;

            IF NEW.value_numeric IS NOT NULL THEN
                IF catalog_valid_min IS NOT NULL AND NEW.value_numeric < catalog_valid_min THEN
                    RAISE EXCEPTION 'indicator_result.value_numeric % below valid_min %', NEW.value_numeric, catalog_valid_min;
                END IF;
                IF catalog_valid_max IS NOT NULL AND NEW.value_numeric > catalog_valid_max THEN
                    RAISE EXCEPTION 'indicator_result.value_numeric % above valid_max %', NEW.value_numeric, catalog_valid_max;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER indicator_result_before_write_trigger
        BEFORE INSERT OR UPDATE ON curated_geo.indicator_result
        FOR EACH ROW EXECUTE FUNCTION curated_geo.indicator_result_before_write()
    """)

    # ── serving view ───────────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.current_indicator_v AS
        SELECT
            ir.id,
            ir.feature_id,
            ir.feature_type,
            ir.scenario_id,
            ir.indicator_id,
            ir.indicator_slug,
            ic.name               AS indicator_name,
            ic.unit,
            ic.value_type,
            ic.computation_kind,
            ir.job_run_id,
            ir.model_version,
            ir.value_numeric,
            ir.value_text,
            ir.value_json,
            ir.quality_flag,
            ir.confidence,
            ir.missing_reason,
            ir.computed_at
        FROM curated_geo.indicator_result ir
        JOIN curated_geo.indicator_catalog ic ON ic.id = ir.indicator_id
        WHERE ir.status = 'current'
    """)

    # ── register new read surfaces in explore.layer_catalog ────────────────
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            "INSERT INTO explore.layer_catalog "
            "(schema_name, object_name, logical_name, layer_kind, description, "
            "primary_geometry_column, intended_use, display_order) VALUES "
            f"('{row[0]}', '{row[1]}', '{row[2]}', '{row[3]}', '{row[4]}', "
            f"{_sql_null_or(row[5])}, '{row[6]}', {row[7]})"
        )


_NEW_CATALOG_ROWS = [
    # (schema_name, object_name, logical_name, layer_kind, description,
    #  primary_geometry_column, intended_use, display_order)
    (
        "curated_geo",
        "indicator_catalog",
        "indicator_catalog",
        "canonical",
        "Registry of every indicator computed by the platform",
        None,
        "See what indicators are defined and how they aggregate",
        60,
    ),
    (
        "curated_geo",
        "indicator_result",
        "indicator_results",
        "canonical",
        "Long-format computed indicator values per (feature, scenario, indicator)",
        None,
        "Inspect raw indicator outputs and lineage",
        70,
    ),
    (
        "serving_geo",
        "current_indicator_v",
        "current_indicators",
        "serving",
        "App-facing indicator read contract (status = current only) joined with catalog metadata",
        None,
        "Inspect the read path API and workers use for indicator values",
        120,
    ),
]


def _sql_null_or(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def downgrade() -> None:
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            f"DELETE FROM explore.layer_catalog "
            f"WHERE schema_name = '{row[0]}' AND object_name = '{row[1]}'"
        )
    op.execute("DROP VIEW IF EXISTS serving_geo.current_indicator_v")
    op.execute(
        "DROP TRIGGER IF EXISTS indicator_result_before_write_trigger ON curated_geo.indicator_result"
    )
    op.execute("DROP FUNCTION IF EXISTS curated_geo.indicator_result_before_write")
    op.execute(
        "DROP TRIGGER IF EXISTS indicator_dependency_validate_trigger ON curated_geo.indicator_dependency"
    )
    op.execute("DROP FUNCTION IF EXISTS curated_geo.indicator_dependency_validate")
    op.execute("DROP TABLE IF EXISTS curated_geo.indicator_result CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.indicator_dependency CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.indicator_catalog CASCADE")
    op.execute("DROP TABLE IF EXISTS jobs_runtime.job_run CASCADE")
