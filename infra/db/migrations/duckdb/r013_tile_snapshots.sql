CREATE TABLE IF NOT EXISTS curated_geo.tile_snapshot_registry (
    id UUID PRIMARY KEY DEFAULT uuid(),
    layer_id VARCHAR NOT NULL,
    artifact_path VARCHAR NOT NULL,
    artifact_uri VARCHAR,
    snapshot_version VARCHAR NOT NULL,
    source_relation VARCHAR NOT NULL,
    source_lineage_ref JSON NOT NULL DEFAULT '{}',
    status VARCHAR NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (layer_id, snapshot_version),
    CHECK (status IN ('ready', 'failed', 'superseded'))
);

CREATE INDEX IF NOT EXISTS tile_snapshot_layer_created_idx
ON curated_geo.tile_snapshot_registry (layer_id, created_at);
