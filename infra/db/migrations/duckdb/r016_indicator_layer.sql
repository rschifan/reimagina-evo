-- r016: indicator layer + jobs_runtime.job_run (DuckDB mirror of Postgres r016).
-- DuckDB has no triggers and no partial indexes, so:
--   * Uniqueness constraints from the Postgres partial indexes (one 'current'
--     row per (feature, scenario, indicator)) are enforced by the repository
--     layer (DuckDBIndicatorResultRepository) through read-modify-write upserts.
--   * The before-write trigger (slug/type denormalisation, value-column-count
--     check, valid_min/max bounds, applies_to membership) is enforced the same
--     way in DuckDBIndicatorResultRepository.write_result.
--   * The deferred acyclicity + weight-sum check for indicator_dependency
--     moves into a Python helper in reimagina.indicators.dependencies.
-- The table shapes are otherwise identical so cross-backend integration tests
-- can share fixtures.

CREATE SCHEMA IF NOT EXISTS jobs_runtime;

CREATE TABLE IF NOT EXISTS jobs_runtime.job_run (
    id            UUID        PRIMARY KEY DEFAULT uuid(),
    scenario_id   UUID,
    model_family  VARCHAR     NOT NULL,
    status        VARCHAR     NOT NULL
                              CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    code_version  VARCHAR     NOT NULL,
    queued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    error_message VARCHAR,
    CHECK (started_at IS NULL OR started_at >= queued_at),
    CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE INDEX IF NOT EXISTS job_run_scenario_idx ON jobs_runtime.job_run (scenario_id);
CREATE INDEX IF NOT EXISTS job_run_status_idx   ON jobs_runtime.job_run (status);
CREATE INDEX IF NOT EXISTS job_run_family_idx   ON jobs_runtime.job_run (model_family, status);


CREATE TABLE IF NOT EXISTS curated_geo.indicator_catalog (
    id                 UUID             PRIMARY KEY DEFAULT uuid(),
    slug               VARCHAR          NOT NULL UNIQUE,
    name               VARCHAR          NOT NULL,
    description        VARCHAR,
    model_family       VARCHAR,
    computation_kind   VARCHAR          NOT NULL
                                        CHECK (computation_kind IN ('direct','composite')),
    value_type         VARCHAR          NOT NULL
                                        CHECK (value_type IN ('numeric','text','json')),
    unit               VARCHAR,
    valid_min          DOUBLE,
    valid_max          DOUBLE,
    aggregation_method VARCHAR          NOT NULL
                                        CHECK (aggregation_method IN
                                          ('mean','weighted_mean','sum','min','max','none')),
    applies_to         VARCHAR[]        NOT NULL,
    created_at         TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CHECK (len(applies_to) > 0),
    CHECK (valid_min IS NULL OR valid_max IS NULL OR valid_min <= valid_max),
    CHECK (value_type = 'numeric' OR (valid_min IS NULL AND valid_max IS NULL)),
    CHECK (
        (computation_kind = 'direct'    AND model_family IS NOT NULL) OR
        (computation_kind = 'composite' AND model_family IS NULL)
    )
);


CREATE TABLE IF NOT EXISTS curated_geo.indicator_dependency (
    indicator_id   UUID   NOT NULL,
    depends_on_id  UUID   NOT NULL,
    weight         DOUBLE,
    PRIMARY KEY (indicator_id, depends_on_id),
    CHECK (indicator_id <> depends_on_id),
    CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1))
);


CREATE TABLE IF NOT EXISTS curated_geo.indicator_result (
    id              UUID             PRIMARY KEY DEFAULT uuid(),
    feature_id      UUID             NOT NULL,
    feature_type    VARCHAR          NOT NULL,
    scenario_id     UUID,
    indicator_id    UUID             NOT NULL,
    indicator_slug  VARCHAR          NOT NULL,
    job_run_id      UUID             NOT NULL,
    model_version   VARCHAR,
    value_numeric   DOUBLE,
    value_text      VARCHAR,
    value_json      JSON,
    quality_flag    VARCHAR          NOT NULL DEFAULT 'measured'
                                     CHECK (quality_flag IN
                                       ('measured','estimated','imputed','missing','not_applicable')),
    confidence      DOUBLE           CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
    missing_reason  VARCHAR,
    status          VARCHAR          NOT NULL DEFAULT 'current'
                                     CHECK (status IN ('current','stale','failed')),
    computed_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Read-side covering indexes. Uniqueness enforced in repository code.
CREATE INDEX IF NOT EXISTS ir_feature_scenario_idx
    ON curated_geo.indicator_result (feature_id, scenario_id, indicator_id, status);
CREATE INDEX IF NOT EXISTS ir_scenario_slug_idx
    ON curated_geo.indicator_result (scenario_id, indicator_slug, status);
CREATE INDEX IF NOT EXISTS ir_job_run_idx
    ON curated_geo.indicator_result (job_run_id);


CREATE OR REPLACE VIEW serving_geo.current_indicator_v AS
SELECT
    CAST(ir.id AS VARCHAR)           AS id,
    CAST(ir.feature_id AS VARCHAR)   AS feature_id,
    ir.feature_type,
    CAST(ir.scenario_id AS VARCHAR)  AS scenario_id,
    CAST(ir.indicator_id AS VARCHAR) AS indicator_id,
    ir.indicator_slug,
    ic.name                          AS indicator_name,
    ic.unit,
    ic.value_type,
    ic.computation_kind,
    CAST(ir.job_run_id AS VARCHAR)   AS job_run_id,
    ir.model_version,
    ir.value_numeric,
    ir.value_text,
    CAST(ir.value_json AS VARCHAR)   AS value_json,
    ir.quality_flag,
    ir.confidence,
    ir.missing_reason,
    ir.computed_at
FROM curated_geo.indicator_result ir
JOIN curated_geo.indicator_catalog ic ON ic.id = ir.indicator_id
WHERE ir.status = 'current';


INSERT OR IGNORE INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
    ('curated_geo', 'indicator_catalog', 'indicator_catalog', 'canonical',
     'Registry of every indicator computed by the platform', NULL,
     'See what indicators are defined and how they aggregate', 60),
    ('curated_geo', 'indicator_result', 'indicator_results', 'canonical',
     'Long-format computed indicator values per (feature, scenario, indicator)', NULL,
     'Inspect raw indicator outputs and lineage', 70),
    ('serving_geo', 'current_indicator_v', 'current_indicators', 'serving',
     'App-facing indicator read contract (status = current only) joined with catalog metadata', NULL,
     'Inspect the read path API and workers use for indicator values', 120);
