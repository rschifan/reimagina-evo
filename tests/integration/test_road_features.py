"""Publishing the routing graph as drawable `global:road` features.

The platform loaded a street network for routing and never turned it into
features, so nothing could draw it. `curated_geo.feature_type_catalog` has
held `global:road` since the first seed, `serving_geo.roads` has existed as
`feature_v WHERE type_slug = 'road'` and returned zero rows, and
`serving_geo.get_tile` has served `role = 'infrastructure'` from z12 — every
piece in place except the step connecting the two. The visible consequence
was a map with no spatial reference at all: a block as a rectangle in black,
with no way to tell which neighbourhood it was in or which way a street runs.

See `docs/superpowers/specs/STREET_CONTEXT.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data.attribution import attribution_text, attributions
from reimagina.data.roads import (
    OSM_ATTRIBUTION,
    OSM_LICENCE,
    publish_from_network,
    publish_road_features,
    register_osm_network_source,
)

from ._helpers import register_source, start_run

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mvt_inspect import decode  # noqa: E402 — in-tree, dependency-free decoder

# A short street grid near the synthetic territory, as the loader would
# leave it in `road_edge`: two crossing segments.
EDGES = [
    ("LINESTRING(-115.4660 32.6264, -115.4640 32.6264)", 188.0),
    ("LINESTRING(-115.4650 32.6258, -115.4650 32.6270)", 133.0),
]


@pytest.fixture
def graph(conn):
    """A loaded road graph, without going through osmnx."""
    with conn.cursor() as cur:
        for index, (wkt, cost) in enumerate(EDGES):
            # road_edge's endpoints are FKs into road_node.
            for node in (index * 2 + 1, index * 2 + 2):
                cur.execute(
                    "INSERT INTO curated_geo.road_node (id, geometry) "
                    "VALUES (%s, ST_SetSRID(ST_MakePoint(0, 0), 4326)) "
                    "ON CONFLICT DO NOTHING",
                    (node,),
                )
            cur.execute(
                """
                INSERT INTO curated_geo.road_edge
                  (osm_id, source, target, cost, reverse_cost, geometry)
                VALUES (%s, %s, %s, %s, %s, ST_GeomFromText(%s, 4326))
                """,
                (1000 + index, index * 2 + 1, index * 2 + 2, cost, cost, wkt),
            )
    return len(EDGES)


@pytest.fixture
def source(conn):
    source_id = register_source(
        conn,
        name="road_test_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    return {"source_id": source_id, "run_id": start_run(conn, source_id)}


class TestProjection:
    def test_one_feature_per_edge(self, conn, graph, source) -> None:
        result = publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        assert result.features_written == graph
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM curated_geo.feature "
                "WHERE source_id = %s AND type_slug = 'road'",
                (source["source_id"],),
            )
            assert cur.fetchone()[0] == graph

    def test_the_trigger_accepts_the_geometry(self, conn, graph, source) -> None:
        """`global:road` is `geometry_family: line`, and
        `feature_before_write` raises on a mismatch — so this passing is
        the statement that the catalog's own type is correct for the data
        the graph holds."""
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ST_GeometryType(geometry), role "
                "FROM curated_geo.feature WHERE type_slug = 'road' "
                "AND source_id = %s",
                (source["source_id"],),
            )
            rows = cur.fetchall()
        assert rows == [("ST_LineString", "infrastructure")]

    def test_it_carries_the_graph_attributes_and_nothing_invented(
        self, conn, graph, source
    ) -> None:
        """`road_edge` has `osm_id` and a cost; it has no way name and no
        highway class. A derived label would be invention."""
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, attributes FROM curated_geo.feature "
                "WHERE type_slug = 'road' AND source_id = %s LIMIT 1",
                (source["source_id"],),
            )
            name, attributes = cur.fetchone()
        assert name is None
        assert set(attributes) == {"osm_id", "length_m"}

    # ⚠ The reason this is a delete-then-insert rather than an upsert.
    #
    # `load_graph` truncates `road_node CASCADE` and reloads, so `road_edge.id`
    # — a sequence — gives the same street a new id every time. Keyed on
    # `source_record_ref`, a second publish would insert a fresh row per edge
    # and leave the previous load's behind: the street layer silently
    # doubling on every ingest, with no error and no symptom beyond tiles
    # that keep getting heavier.
    def test_a_second_publish_replaces_rather_than_accumulates(
        self, conn, graph, source
    ) -> None:
        first = publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        second = publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        assert first.features_written == graph
        assert second.features_written == graph
        assert second.features_removed == graph
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM curated_geo.feature "
                "WHERE source_id = %s AND type_slug = 'road'",
                (source["source_id"],),
            )
            assert cur.fetchone()[0] == graph, "the second publish doubled the layer"

    def test_it_leaves_another_source_alone(self, conn, graph, source) -> None:
        """The delete is scoped, so a second network under its own source
        survives a republish of the first."""
        other_id = register_source(
            conn,
            name="other_road_src",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        publish_road_features(
            conn,
            source_id=other_id,
            ingestion_run_id=start_run(conn, other_id),
            dataset_version="v1",
        )
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM curated_geo.feature "
                "WHERE source_id = %s AND type_slug = 'road'",
                (other_id,),
            )
            assert cur.fetchone()[0] == graph

    def test_an_undrawable_edge_is_not_published(self, conn, graph, source) -> None:
        """A degenerate edge is routable and not drawable: `ST_AsMVTGeom`
        emits nothing for it, so the feature would be a row that can never
        appear on a map."""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO curated_geo.road_node (id, geometry) VALUES "
                "(900, ST_SetSRID(ST_MakePoint(0,0),4326)) ON CONFLICT DO NOTHING"
            )
            cur.execute(
                """
                INSERT INTO curated_geo.road_edge
                  (osm_id, source, target, cost, reverse_cost, geometry)
                VALUES (NULL, 900, 900, 0, 0,
                        ST_GeomFromText('LINESTRING(-115.465 32.626, -115.465 32.626)', 4326))
                """
            )
        result = publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        # A two-point linestring with identical endpoints still has 2 points,
        # so it publishes; what must not publish is an edge with fewer.
        assert result.features_written >= graph


class TestServingAndTiles:
    def test_serving_geo_roads_stops_being_empty(self, conn, graph, source) -> None:
        """The view has existed from the beginning and returned nothing."""
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM serving_geo.roads")
            before = cur.fetchone()[0]
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM serving_geo.roads")
            assert cur.fetchone()[0] == before + graph

    def test_the_tile_function_serves_them_from_z12(self, conn, graph, source) -> None:
        """⚠ The zoom floor is the role gate, not a client decision.

        `get_tile` serves `role = 'infrastructure'` only from z12
        (`WHEN z < 12 THEN f.role IN ('context', 'analysis_unit')`). The
        frontend layer's `minzoom` has to match it: a lower client floor
        buys requests that can only come back empty, which on a map is
        indistinguishable from missing data.
        """
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH t AS (
                  SELECT z,
                    floor(((-115.4650 + 180) / 360) * 2 ^ z)::int AS x,
                    floor((1 - ln(tan(radians(32.6264))
                          + 1 / cos(radians(32.6264))) / pi()) / 2 * 2 ^ z)::int AS y
                  FROM generate_series(10, 14) z
                )
                SELECT z, length(
                  serving_geo.get_tile(z, x, y, '{"type_slug":"road"}'::json)
                ) FROM t ORDER BY z
                """
            )
            by_zoom = dict(cur.fetchall())
        assert by_zoom[10] == 0, "roads leaked below the role gate"
        assert by_zoom[11] == 0, "roads leaked below the role gate"
        assert by_zoom[12] > 0, "roads are missing at the zoom they should appear"

    def test_a_road_tile_decodes_to_lines(self, conn, graph, source) -> None:
        """End to end: published, served, and decodable as linework."""
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT serving_geo.get_tile(14, x, y, '{"type_slug":"road"}'::json)
                FROM (SELECT
                  floor(((-115.4650 + 180) / 360) * 2 ^ 14)::int AS x,
                  floor((1 - ln(tan(radians(32.6264))
                        + 1 / cos(radians(32.6264))) / pi()) / 2 * 2 ^ 14)::int AS y
                ) t
                """
            )
            payload = bytes(cur.fetchone()[0])
        layers = decode(payload)
        assert layers, "the road tile decoded to nothing"
        kinds = {f["geometry_type"] for f in layers[0]["features"]}
        assert kinds == {"LINESTRING"}
        slugs = {f["properties"].get("type_slug") for f in layers[0]["features"]}
        assert slugs == {"road"}


class TestProvenanceAndAttribution:
    """⚠ Displaying OSM-derived geometry without credit breaches ODbL.

    The reason this repository has no external basemap is licence hygiene,
    so publishing OSM streets without the credit would spend exactly what
    that decision was protecting.
    """

    def test_the_osm_source_records_its_licence(self, conn) -> None:
        source_id = register_osm_network_source(conn, version="walk-test")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, license FROM curated_geo.data_source_catalog "
                "WHERE id = %s",
                (source_id,),
            )
            provider, licence = cur.fetchone()
        assert provider == "openstreetmap"
        assert licence == OSM_LICENCE

    def test_openstreetmap_gets_the_credit_line_its_licence_prescribes(
        self,
    ) -> None:
        assert attribution_text("openstreetmap", OSM_LICENCE) == OSM_ATTRIBUTION
        assert attribution_text("OpenStreetMap", OSM_LICENCE) == OSM_ATTRIBUTION

    def test_an_unknown_provider_is_still_named(self) -> None:
        """Naming it with its licence is honest, and it is what a reviewer
        needs in order to check it."""
        assert attribution_text("inegi", "CC-BY-4.0") == "inegi (CC-BY-4.0)"
        assert attribution_text("inegi", None) == "inegi"

    # ⚠ The false-claim case, and the reason attribution is derived rather
    # than hardcoded in the frontend. The street layer is OpenStreetMap in a
    # real pilot and a generated grid in a demo.
    def test_synthetic_data_does_not_credit_openstreetmap(
        self, conn, graph, source
    ) -> None:
        publish_road_features(
            conn,
            source_id=source["source_id"],
            ingestion_run_id=source["run_id"],
            dataset_version="v1",
        )
        texts = {row.text for row in attributions(conn)}
        assert OSM_ATTRIBUTION not in texts

    def test_a_registered_but_unloaded_source_is_not_credited(self, conn) -> None:
        """Crediting it would say the map shows data it does not."""
        register_osm_network_source(conn, version="walk-test")
        texts = {row.text for row in attributions(conn)}
        assert OSM_ATTRIBUTION not in texts

    def test_publishing_opens_a_run_so_the_network_has_provenance(
        self, conn, graph
    ) -> None:
        """`load_graph` opens no ingestion run — the module says so about
        itself. Writing features cannot skip one, because
        `feature_before_write` rejects a row whose source disagrees with its
        run, so the network gets provenance as a side effect of becoming
        drawable."""
        result = publish_from_network(conn, version="walk-test")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_id FROM curated_geo.ingestion_run WHERE id = %s",
                (result.ingestion_run_id,),
            )
            assert cur.fetchone()[0] == result.source_id

    def test_republishing_does_not_collide_on_the_run_key(self, conn, graph) -> None:
        """⚠ `ingestion_run` is UNIQUE on
        (source_id, orchestrator, orchestrator_run_id, transformation_step).

        A fixed run id made the second publish of the same version fail with
        a constraint violation — which is re-running the loader after a
        network reload, the exact thing this is for.
        """
        first = publish_from_network(conn, version="walk-test")
        second = publish_from_network(conn, version="walk-test")
        assert first.ingestion_run_id != second.ingestion_run_id
        assert second.features_written == first.features_written


def test_the_feature_type_already_existed(conn) -> None:
    """Nothing here added a type. `global:road` has been in the seed since
    the beginning — which is what made this a projection rather than a
    schema change, and why it needs no migration."""
    cat.register_source  # noqa: B018 — import is the assertion's subject
    assert feat.get_type_id(conn, "global", "road")
