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

from reimagina.assets.keys import indicator_asset_key
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


#: The code version every indicator asset starts at.
#:
#: Per-asset and bumped by hand when that indicator's own logic changes — NOT
#: ``provenance.code_version()``. That is a git SHA, so feeding it into the
#: fingerprint would make a README commit invalidate every derived asset in the
#: platform. Dagster's per-asset code_version works the same way, and SQLMesh
#: goes further by classifying a change as breaking or not; this is the floor of
#: that idea, not the ceiling.
_INITIAL_INDICATOR_CODE_VERSION = "1"


def indicator_assets() -> tuple[AssetSpec, ...]:
    """Project one asset per shipped indicator.

    Derived from ``SHIPPED_INDICATORS`` rather than declared a second time here,
    so the asset graph and the indicator catalog cannot disagree about which
    indicators exist or what they consume.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    return tuple(
        AssetSpec(
            asset_key=indicator_asset_key(spec.slug),
            kind="derived",
            backing_kind="indicator",
            backing_ref=spec.slug,
            producer=spec.slug,
            code_version=_INITIAL_INDICATOR_CODE_VERSION,
            # Results are partitioned by scenario: a what-if writes its own rows
            # and must not stale the baseline's.
            partition_dimensions=("scenario",),
            description=spec.name,
        )
        for spec in SHIPPED_INDICATORS
    )


def seed_asset_edges(conn: Any) -> int:
    """Write one ``asset_dependency`` row per declared indicator input.

    ⚠ Must run inside the same transaction as ``seed_assets``, and not on an
    autocommit connection. ``asset_dependency_validate`` is a DEFERRABLE
    INITIALLY DEFERRED constraint trigger that checks acyclicity and that
    ``derived_from`` weights sum to 1.0; under autocommit each INSERT commits
    alone, so the first row of a 0.5/0.5 composite fails on its own. Same
    constraint, and same reason, as ``seed_catalog``.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    written = 0
    with conn.cursor() as cur:
        cur.execute("SELECT asset_key, id FROM curated_geo.asset")
        ids = dict(cur.fetchall())
        for spec in SHIPPED_INDICATORS:
            consumer = ids[indicator_asset_key(spec.slug)]
            # Replace rather than merge, so removing an input from a declaration
            # actually removes the edge instead of leaving a phantom dependency
            # that keeps invalidating the indicator forever.
            cur.execute(
                "DELETE FROM curated_geo.asset_dependency WHERE consumer_asset_id = %s",
                (consumer,),
            )
            for edge in spec.inputs:
                if edge.asset_key not in ids:
                    raise RuntimeError(
                        f"indicator {spec.slug!r} declares input {edge.asset_key!r}, "
                        "which is not a registered asset"
                    )
                cur.execute(
                    """
                    INSERT INTO curated_geo.asset_dependency
                      (consumer_asset_id, producer_asset_id, relation, attributes,
                       radius_m, weight, partition_mapping)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        consumer,
                        ids[edge.asset_key],
                        edge.relation,
                        list(edge.attributes),
                        edge.radius_m,
                        edge.weight,
                        edge.partition_mapping,
                    ),
                )
                written += 1
    return written


def seed_assets(
    conn: Any, specs: tuple[AssetSpec, ...] | None = None
) -> list[dict[str, Any]]:
    """Idempotently register every asset and its declared edges.

    Returns ``{asset_key, id}`` in order. Safe to re-run: an existing row is
    updated in place, so an asset keeps its id and therefore its version history
    across a re-seed.
    """
    if specs is None:
        specs = SHIPPED_ASSETS + indicator_assets()
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
    seed_asset_edges(conn)
    return result
