-- r015: AOI-level indicator summary cache.
-- The row stores a single aggregated value per (aoi_id, scenario_id, indicator_id).
-- scenario_id is nullable to represent "base city" (Diagnosis mode).
-- indicator_id has no FK yet because curated_geo.indicator_catalog lands with
-- Layer 2 indicators; the column is a plain UUID until then.

CREATE TABLE IF NOT EXISTS curated_geo.aoi_indicator_summary (
    id                UUID    PRIMARY KEY DEFAULT uuid(),
    aoi_id            UUID    NOT NULL,
    scenario_id       UUID,
    indicator_id      UUID    NOT NULL,
    value             DOUBLE,
    aggregation_note  VARCHAR,
    status            VARCHAR NOT NULL DEFAULT 'current',
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN ('current', 'stale', 'missing'))
);

-- DuckDB does not support partial indexes (unlike Postgres r015, which uses
-- two partial unique indexes). Uniqueness is enforced repo-side via
-- DuckDBAOISummaryRepository.upsert_summary (lookup-then-update); a read
-- lookup index covers the common (aoi_id, scenario_id) access pattern.
CREATE INDEX IF NOT EXISTS aoi_indicator_summary_aoi_idx
    ON curated_geo.aoi_indicator_summary (aoi_id, scenario_id, indicator_id);

-- Serving view — a minimal projection until indicator_catalog arrives in
-- Layer 2; at that point the view will be extended to join slug/name/unit.
CREATE OR REPLACE VIEW serving_geo.aoi_summary_v AS
SELECT
    CAST(s.id AS VARCHAR)           AS id,
    CAST(s.aoi_id AS VARCHAR)       AS aoi_id,
    CAST(s.scenario_id AS VARCHAR)  AS scenario_id,
    CAST(s.indicator_id AS VARCHAR) AS indicator_id,
    s.value,
    s.aggregation_note,
    s.status,
    s.computed_at
FROM curated_geo.aoi_indicator_summary s
WHERE s.status = 'current';

-- Register the new read surfaces in explore.layer_catalog. The view over
-- that table (created in r006) picks them up automatically.
INSERT OR IGNORE INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
    ('curated_geo', 'aoi_indicator_summary', 'aoi_summaries', 'canonical',
     'Cached AOI-level aggregated indicator values', NULL,
     'Inspect cached summary rows by AOI', 50),
    ('serving_geo', 'aoi_summary_v', 'aoi_summaries_serving', 'serving',
     'App-facing AOI-level summaries (status = current only)', NULL,
     'Inspect the read contract for the AOI summary cache', 110);
