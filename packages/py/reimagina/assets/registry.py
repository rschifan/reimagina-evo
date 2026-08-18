"""The shipped assets, and idempotent seeding of them.

Mirrors ``reimagina.data.feature.FEATURE_TYPE_ROWS`` / ``seed_feature_types``:
one Python constant is the source of truth and a seed function upserts it. The
registry is seeded from **code**, never from migration bodies — ``r006``/``r015``/
``r016``/``r019``/``r020`` each insert their own ``explore.layer_catalog`` rows
and that drifted far enough to need ``scripts/repair_layer_catalog.sql``. One
seeder cannot drift from itself.

Only source and external assets live here. Indicator assets are *projected* from
``SHIPPED_INDICATORS`` rather than re-declared, so the two cannot disagree about
what an indicator is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reimagina.storage import backend_for_connection


@dataclass(frozen=True)
class AssetSpec:
    """One registered asset.

    ``backing_ref`` is interpreted per ``backing_kind``: a ``context:type_slug``
    for a feature type, an attribute-key prefix for an observation family, a
    relation name for a Postgres table the graph does not own.
    """

    asset_key: str
    kind: str
    backing_kind: str
    backing_ref: str | None = None
    version_strategy: str | None = None
    probe_sql: str | None = None
    code_version: str | None = None
    producer: str | None = None
    params_digest: str | None = None
    partition_dimensions: tuple[str, ...] = field(default_factory=tuple)
    description: str | None = None


def _feature_layer(
    asset_key: str, context: str, type_slug: str, description: str
) -> AssetSpec:
    return AssetSpec(
        asset_key=asset_key,
        kind="source",
        backing_kind="feature_type",
        backing_ref=f"{context}:{type_slug}",
        version_strategy="commit",
        description=description,
    )


#: Every dataset an indicator can depend on. Feature layers are `commit`: the
#: pipeline that loads them publishes a version on a successful ingest.
#:
#: `global.road_network` is `external`, not `source`, and that distinction is the
#: point of the third kind. `pipeline/osm_network.load_graph` opens no
#: ingestion_run at all, TRUNCATEs and reloads the whole graph, and fetches from
#: Overpass — a rate-limited upstream that changes continuously and cannot be
#: replayed. Nothing in this platform produces it, so nothing here can publish a
#: version for it; it can only be observed.
SHIPPED_ASSETS: tuple[AssetSpec, ...] = (
    _feature_layer("mex.state", "MEX", "state", "INEGI state boundaries"),
    _feature_layer(
        "mex.municipality", "MEX", "municipality", "INEGI municipal boundaries"
    ),
    _feature_layer("mex.neighborhood", "MEX", "neighborhood", "CDMX colonias"),
    _feature_layer("mex.ageb", "MEX", "ageb", "INEGI AGEB analysis units"),
    _feature_layer("mex.block", "MEX", "block", "INEGI manzanas — the base grain"),
    _feature_layer("mex.lot", "MEX", "lot", "Cadastral lots"),
    _feature_layer(
        "global.building", "global", "building", "Overture building footprints"
    ),
    _feature_layer(
        "global.poi", "global", "establishment", "Amenity and establishment points"
    ),
    AssetSpec(
        asset_key="global.road_network",
        kind="external",
        backing_kind="relation",
        backing_ref="curated_geo.road_edge",
        version_strategy="observed",
        # Cheap by construction: road_edge holds one AOI's walkable graph, not a
        # planet extract. Counting is the whole content signal available — the
        # table carries no ingestion_run, no source row and no load timestamp.
        probe_sql=(
            "SELECT count(*)::text || ':' || "
            "COALESCE(max(osm_id)::text, '') FROM curated_geo.road_edge"
        ),
        description="OSM pedestrian network powering network accessibility",
    ),
    AssetSpec(
        asset_key="censo.2020",
        kind="source",
        backing_kind="observation_prefix",
        backing_ref="censo_2020",
        version_strategy="commit",
        description="INEGI 2020 census observations joined to AGEB/block features",
    ),
)


def seed_assets(
    conn: Any, specs: tuple[AssetSpec, ...] = SHIPPED_ASSETS
) -> list[dict[str, Any]]:
    """Idempotently register every spec. Returns ``{asset_key, id}`` in order.

    Safe to re-run: an existing row is updated in place, so an asset keeps its id
    and therefore its version history across a re-seed.
    """
    if backend_for_connection(conn).name != "postgres":
        raise RuntimeError("the asset graph requires the Postgres backend")

    result: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for spec in specs:
            feature_type_id = None
            if spec.backing_kind == "feature_type" and spec.backing_ref:
                context, _, type_slug = spec.backing_ref.partition(":")
                cur.execute(
                    "SELECT id FROM curated_geo.feature_type_catalog "
                    "WHERE context = %s AND type_slug = %s",
                    (context, type_slug),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(
                        f"asset {spec.asset_key!r} references unknown feature type "
                        f"{spec.backing_ref!r} — run `feat seed-types` first"
                    )
                feature_type_id = row[0]

            cur.execute(
                """
                INSERT INTO curated_geo.asset
                  (asset_key, kind, producer, backing_kind, backing_ref,
                   feature_type_id, partition_dimensions, code_version,
                   params_digest, version_strategy, probe_sql, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (asset_key) DO UPDATE SET
                  kind                 = EXCLUDED.kind,
                  producer             = EXCLUDED.producer,
                  backing_kind         = EXCLUDED.backing_kind,
                  backing_ref          = EXCLUDED.backing_ref,
                  feature_type_id      = EXCLUDED.feature_type_id,
                  partition_dimensions = EXCLUDED.partition_dimensions,
                  code_version         = EXCLUDED.code_version,
                  params_digest        = EXCLUDED.params_digest,
                  version_strategy     = EXCLUDED.version_strategy,
                  probe_sql            = EXCLUDED.probe_sql,
                  description          = EXCLUDED.description
                RETURNING id
                """,
                (
                    spec.asset_key,
                    spec.kind,
                    spec.producer,
                    spec.backing_kind,
                    spec.backing_ref,
                    feature_type_id,
                    list(spec.partition_dimensions),
                    spec.code_version,
                    spec.params_digest,
                    spec.version_strategy,
                    spec.probe_sql,
                    spec.description,
                ),
            )
            result.append({"asset_key": spec.asset_key, "id": cur.fetchone()[0]})
    return result
