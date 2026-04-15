CREATE INDEX IF NOT EXISTS ingestion_run_source_started_idx
    ON curated_geo.ingestion_run (source_id, started_at);

CREATE INDEX IF NOT EXISTS feature_type_id_idx
    ON curated_geo.feature (type_id);

CREATE INDEX IF NOT EXISTS feature_ingestion_run_idx
    ON curated_geo.feature (ingestion_run_id);

CREATE INDEX IF NOT EXISTS observation_ingestion_run_idx
    ON curated_geo.observation (ingestion_run_id);

CREATE INDEX IF NOT EXISTS file_asset_ingestion_run_idx
    ON curated_geo.file_asset (ingestion_run_id);

CREATE INDEX IF NOT EXISTS area_of_interest_project_idx
    ON curated_geo.area_of_interest (project_id);
