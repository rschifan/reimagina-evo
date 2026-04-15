CREATE OR REPLACE VIEW serving_geo.feature_current_attributes_v AS
SELECT
    f.id AS feature_id,
    COALESCE(obs.current_attributes, '{}'::JSON) AS current_attributes
FROM curated_geo.feature f
LEFT JOIN (
    SELECT
        feature_id,
        json_group_object(
            attribute_key,
            CASE
                WHEN value_numeric IS NOT NULL THEN to_json(value_numeric)
                WHEN value_text IS NOT NULL THEN to_json(value_text)
                ELSE value_json
            END
        ) AS current_attributes
    FROM curated_geo.observation
    WHERE is_current
    GROUP BY feature_id
) obs ON obs.feature_id = f.id;

CREATE OR REPLACE VIEW serving_geo.feature_v AS
SELECT
    f.id,
    f.type_id,
    f.type_slug,
    f.role,
    f.parent_id,
    f.name,
    f.geometry,
    json_merge_patch(COALESCE(f.attributes, '{}'::JSON), COALESCE(fca.current_attributes, '{}'::JSON)) AS attributes,
    f.source_id,
    f.source_record_ref,
    f.dataset_version,
    f.confidence,
    f.model_version,
    f.created_at,
    f.updated_at
FROM curated_geo.feature f
LEFT JOIN serving_geo.feature_current_attributes_v fca ON fca.feature_id = f.id;

CREATE OR REPLACE VIEW serving_geo.feature_aoi_v AS
SELECT
    af.aoi_id,
    af.overlap_basis,
    af.overlap_measure,
    af.feature_measure,
    af.overlap_ratio,
    f.id,
    f.type_id,
    f.type_slug,
    f.role,
    f.parent_id,
    f.name,
    f.geometry,
    f.attributes,
    f.source_id,
    f.source_record_ref,
    f.dataset_version,
    f.confidence,
    f.model_version,
    f.created_at,
    f.updated_at
FROM serving_geo.feature_v f
JOIN curated_geo.aoi_feature af ON af.feature_id = f.id;

CREATE OR REPLACE VIEW serving_geo.analysis_units_v AS
SELECT * FROM serving_geo.feature_v WHERE role = 'analysis_unit';

CREATE OR REPLACE VIEW serving_geo.infrastructure_v AS
SELECT * FROM serving_geo.feature_v WHERE role = 'infrastructure';

CREATE OR REPLACE VIEW serving_geo.pois_v AS
SELECT * FROM serving_geo.feature_v WHERE role = 'poi';

CREATE OR REPLACE VIEW serving_geo.detections_v AS
SELECT * FROM serving_geo.feature_v WHERE role = 'detection';

CREATE OR REPLACE VIEW serving_geo.admin_v AS
SELECT * FROM serving_geo.feature_v WHERE role = 'context';

CREATE OR REPLACE VIEW serving_geo.blocks AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'block';

CREATE OR REPLACE VIEW serving_geo.lots AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'lot';

CREATE OR REPLACE VIEW serving_geo.municipalities AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'municipality';

CREATE OR REPLACE VIEW serving_geo.roads AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'road';

CREATE OR REPLACE VIEW serving_geo.sidewalks AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'sidewalk';

CREATE OR REPLACE VIEW serving_geo.trees AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'tree';

CREATE OR REPLACE VIEW serving_geo.bus_stops AS
SELECT * FROM serving_geo.feature_v WHERE type_slug = 'bus_stop';
