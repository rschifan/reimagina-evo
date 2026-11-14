"""Publish the routing graph's edges as `global:road` features.

The platform loads a street network for routing — `curated_geo.road_edge`,
with endpoints and a cost per edge — and the accessibility indicators walk
it. Nothing ever turned it into *features*, so nothing could draw it.

That left a hole with no geometry in it. `curated_geo.feature_type_catalog`
has held `global:road` since the first seed (`geometry_family: line`,
`role: infrastructure`); `serving_geo.roads` has existed as
`feature_v WHERE type_slug = 'road'` and returned zero rows;
`serving_geo.get_tile` has served `role = 'infrastructure'` from z12 through
its role gate. Every piece was in place except the step that connects the
graph to the feature table, and the visible consequence was a map with no
street context at all — a block floating in black, with no way to tell which
neighbourhood it was in or which way is north.

**A projection, not an ingest.** This reads one curated table and writes
another, in the same way `feature_overlap` is derived rather than loaded.
The alternative — teaching `osm_network.load_graph` to write features —
couples the OSM loader to the feature table and leaves every other producer
of a graph (the synthetic territory generator, a future GTFS or cadastral
import) to reimplement it. One function, called after any load, keeps the
routing topology as the single source of truth for the network.

**Why it also opens an ingestion run.** `osm_network` says of itself that it
"opens no ``ingestion_run`` and publishes no version", and lists that as a
known gap. Writing features forces the issue: `feature_before_write`
rejects a row whose `source_id` disagrees with its run's, so there has to be
a run — and therefore a registered source, and therefore a licence recorded
in the column `data_source_catalog` keeps for it. The network finally has
provenance because the feature table would not accept it without.

**Attribution is not optional here.** An OSM-derived street layer is ODbL,
and the reason this repository has no external basemap is licence hygiene —
`main.ts`: "the prototype avoids external tile dependencies and licensing
drift". Adding OSM geometry without saying so would spend exactly the thing
that decision was protecting. The source row carries `ODbL-1.0`, and the
map's street source carries the attribution string, so a deployment showing
streets cannot forget to credit them and one without streets makes no claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from reimagina.data import catalog as cat
from reimagina.data import feature as feat

#: What the OSM network is called in `data_source_catalog`. Matched on by
#: `register_or_get_source`, so re-running a load reuses the row rather than
#: minting a second source for the same network.
OSM_SOURCE_NAME = "OpenStreetMap street network"
OSM_PROVIDER = "openstreetmap"

#: ⚠ OpenStreetMap is ODbL-1.0, and this is where the delivery records it.
#: Article 4.1 turns on being able to say what the recipient is taking on;
#: for code that is `docs/THIRD_PARTY_LICENSES.md`, and for data it is this
#: column.
OSM_LICENCE = "ODbL-1.0"

#: Shown on the map whenever the street source is in the style. ODbL's
#: attribution requirement, discharged by the renderer rather than by a
#: sentence in a document nobody opens.
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


@dataclass(frozen=True)
class PublishResult:
    """What one publish did."""

    features_written: int
    features_removed: int
    source_id: str
    ingestion_run_id: str


def register_osm_network_source(conn, *, version: str) -> str:
    """Register (or fetch) the OSM street network's catalog row."""
    return cat.register_source(
        conn,
        name=OSM_SOURCE_NAME,
        provider=OSM_PROVIDER,
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="osm://overpass/walk-network",
        version=version,
        license=OSM_LICENCE,
        notes=(
            "Pedestrian network fetched with osmnx and loaded into "
            "curated_geo.road_edge; published as global:road features by "
            "reimagina.data.roads.publish_road_features."
        ),
    )


def publish_road_features(
    conn,
    *,
    source_id: str,
    ingestion_run_id: str,
    dataset_version: str,
) -> PublishResult:
    """Project `curated_geo.road_edge` into `curated_geo.feature`.

    One feature per edge, typed `global:road`.

    ⚠ **Replace, not upsert, and the reason is the id.**

    `load_graph` truncates `road_node CASCADE` and reloads, so `road_edge.id`
    — a sequence — gives the *same* street a *new* id on every load. An
    upsert keyed on `source_record_ref` would therefore insert a fresh row
    per edge each time and leave the previous load's features behind: the
    street layer silently doubling on every ingest, with no error and no
    obvious symptom beyond tiles that get heavier. Deleting this source's
    road features first also matches what the loader already documents about
    itself — "loading replaces the whole network".

    The delete is scoped to `source_id` and `type_slug`, so a second
    network loaded under a different source is untouched.

    `name` is left NULL. `road_edge` carries `osm_id`, endpoints and cost;
    it does not carry the way's name or its highway class, and deriving a
    label from a length would be invention.
    """
    type_id = feat.get_type_id(conn, "global", "road")
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM curated_geo.feature
            WHERE source_id = %s AND type_slug = 'road'
            """,
            (source_id,),
        )
        removed = cur.rowcount or 0
        cur.execute(
            """
            INSERT INTO curated_geo.feature (
                type_id, geometry, attributes,
                source_id, source_record_ref, ingestion_run_id, dataset_version
            )
            SELECT
                %s::uuid,
                e.geometry,
                jsonb_strip_nulls(jsonb_build_object(
                    'osm_id',   e.osm_id,
                    'length_m', round(e.cost::numeric, 2)
                )),
                %s::uuid,
                -- Stable only within one load, which is why the delete above
                -- is unconditional rather than an ON CONFLICT clause.
                'edge:' || e.id::text,
                %s::uuid,
                %s
            FROM curated_geo.road_edge e
            -- A zero-length or null edge is routable (pgRouting tolerates
            -- it) and not drawable: ST_AsMVTGeom would emit nothing and the
            -- feature would be a row that can never appear on a map.
            WHERE e.geometry IS NOT NULL
              AND ST_NumPoints(e.geometry) > 1
            """,
            (type_id, source_id, ingestion_run_id, dataset_version),
        )
        written = cur.rowcount or 0
    return PublishResult(
        features_written=written,
        features_removed=removed,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
    )


def publish_from_network(
    conn,
    *,
    version: str,
    source_id: str | None = None,
    code_version: str = "roads.publish_from_network",
) -> PublishResult:
    """Register, open a run, and publish — the whole step for a caller.

    ``source_id`` lets a caller that already owns a source (the synthetic
    territory generator, whose roads are not OSM and must not claim to be)
    publish under its own provenance instead of registering the OSM row.
    """
    resolved = source_id or register_osm_network_source(conn, version=version)
    # ⚠ Timestamped, because `ingestion_run` is UNIQUE on
    # (source_id, orchestrator, orchestrator_run_id, transformation_step).
    # A fixed id made the second publish of the same version fail with a
    # constraint violation — which is to say, re-running the loader after a
    # network reload, the exact thing this function exists to support.
    #
    # Accumulating one run per publish is not a leak: each publish really is
    # a distinct ingestion event, and a table of them is what provenance
    # means.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    run_id = cat.start_ingestion_run(
        conn,
        source_id=resolved,
        orchestrator="manual",
        orchestrator_run_id=f"roads-{version}-{stamp}",
        transformation_step="publish_road_features",
        code_version=code_version,
    )
    return publish_road_features(
        conn,
        source_id=resolved,
        ingestion_run_id=run_id,
        dataset_version=version,
    )
