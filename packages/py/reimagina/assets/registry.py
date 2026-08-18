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

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from reimagina.assets.keys import indicator_asset_key, scenario_override_asset_key
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


#: Catalog fields that change an indicator's output or which rows survive it.
#:
#: These are *config*, not code — they live in `indicator_catalog` and can be
#: edited with no commit — so a `code_version` bump would never catch them.
#: Folding them into a digest is what makes "config is data" true rather than
#: aspirational: change `valid_max` and that indicator's results go stale on
#: their own.
#:
#: Not hypothetical. `edificability_block_housing_units.valid_max` was 10,000,
#: from an assumption that a CDMX manzana tops out near 4 ha — false of Tlalpan,
#: where the bound rejected 71 of 5,273 blocks and had to be raised to 200,000.
#: Under this digest that edit restages exactly the affected results.
_PARAMS_IN_DIGEST = (
    "computation_kind",
    "value_type",
    "unit",
    "aggregation_method",
    "valid_min",
    "valid_max",
    "applies_to",
)


def _params_digest(spec: Any) -> str:
    """A stable digest of the catalog params that affect this indicator's output.

    Rendered from a fixed field order rather than from `dataclasses.asdict`, so
    adding an unrelated field to `IndicatorSpec` — a description, a label — does
    not silently invalidate every result in the platform.
    """
    import hashlib

    rendered = "|".join(f"{name}={getattr(spec, name)!r}" for name in _PARAMS_IN_DIGEST)
    return hashlib.md5(  # noqa: S324 - identity, not a security primitive
        rendered.encode(), usedforsecurity=False
    ).hexdigest()


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
            params_digest=_params_digest(spec),
            # Results are partitioned by scenario: a what-if writes its own rows
            # and must not stale the baseline's.
            partition_dimensions=("scenario",),
            description=spec.name,
        )
        for spec in SHIPPED_INDICATORS
    )


def overridable_attributes() -> tuple[str, ...]:
    """Every attribute a what-if can edit, sorted.

    Derived from what the indicators declare they read rather than listed here.
    A hand-maintained list is how ``poi_version`` rotted: it named an input
    nothing produced and nothing validated, for six migrations.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    return tuple(sorted({key for spec in SHIPPED_INDICATORS for key in spec.reads}))


def scenario_override_assets() -> tuple[AssetSpec, ...]:
    """One override asset per attribute, each partitioned by scenario.

    Per attribute, not one asset for all of them, and that is the whole point.
    A single ``scenario.override`` made every attribute-reading indicator a
    consumer of every edit: an override on ``floors`` bumped the one version
    they all read, so all thirteen went dirty — the flat-union
    over-approximation ``resolve_change_set`` documents, restated as a graph.

    Splitting the asset moves the granularity into the node, where the partition
    algebra already lives. ``scenario.override.floors@scenario=S`` is a partition
    of a different *asset* from ``scenario.override.num_levels@scenario=S``, so
    an edit publishes a version for the first and not the second, and the
    downstream propagation in ``asset_component_v`` carries the scenario
    partition only to the indicators that declared an edge to that attribute.

    ⚠ The alternative — keeping one asset and teaching the edge to select an
    attribute slice of it — was rejected. It needs a partition mapping resolved
    per *edge* rather than per node, which means new semantics in the recursive
    view, in its Python twin, and in every partition-aware query. This needs
    none: the existing ``identity`` mapping, unchanged, already does it. Two
    implementations of one algebra can afford exactly one set of rules.

    Attributes with no override rows are still registered. An asset with no
    version contributes ``missing`` identically in every partition, so the
    unedited ones cost a row in the registry and nothing in any hash.
    """
    return tuple(
        AssetSpec(
            asset_key=scenario_override_asset_key(attribute),
            kind="source",
            backing_kind="scenario_override",
            backing_ref=attribute,
            version_strategy="commit",
            # Editing scenario S moves the version of S's partition only; the
            # baseline partition never has a version at all, so no edit to any
            # scenario can reach a baseline result.
            partition_dimensions=("scenario",),
            description=f"Per-scenario overrides of {attribute!r} — the what-if input",
        )
        for attribute in overridable_attributes()
    )


def _partition_mapping(partition_dimensions: Sequence[str]) -> str:
    """How a consumer's partition resolves on this producer.

    Read off the producer's own declared dimensions, not its kind. A producer
    that is partitioned by scenario is read at the consumer's scenario —
    ``identity`` — and one that is not has a single shared copy, so a scenario's
    results read its baseline. There is one shared set of blocks, not one per
    what-if.

    Deriving it rather than special-casing is what makes the per-attribute
    override assets work with no new rules: they are sources, like the layers,
    but they are partitioned by scenario, like the indicators. Keying on `kind`
    could not express that and needed the exception hardcoded on the one edge
    that had it.
    """
    return "identity" if "scenario" in partition_dimensions else "baseline"


def _edges_for(spec: Any) -> list[Any]:
    """The declared edges, plus one override edge per attribute they read.

    Generated from the attribute projection the indicator already declares
    rather than written out per indicator: a hand-maintained second list is
    exactly how ``poi_version`` rotted.

    One edge per attribute, so the graph knows *which* attribute reaches this
    indicator. An indicator reading ``num_levels`` and ``unit_size`` is a
    consumer of two override assets, and an edit to a third attribute reaches
    neither.

    Indicators that read no attributes — the composite — get no such edge, and
    correctly so: a what-if reaches them through their components, not directly.
    """
    from reimagina.assets.keys import AssetEdge

    return [
        *spec.inputs,
        *(
            AssetEdge(
                scenario_override_asset_key(attribute),
                relation="self",
                attributes=(attribute,),
            )
            for attribute in spec.reads
        ),
    ]


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
        cur.execute("SELECT asset_key, id, partition_dimensions FROM curated_geo.asset")
        rows = cur.fetchall()
        ids = {r[0]: r[1] for r in rows}
        dimensions = {r[0]: r[2] for r in rows}
        for spec in SHIPPED_INDICATORS:
            consumer_key = indicator_asset_key(spec.slug)
            if consumer_key not in ids:
                # Reachable whenever a caller seeds a subset of the registry:
                # this loop always covers every shipped indicator, so index it
                # and you get a bare KeyError instead of the message below.
                raise RuntimeError(
                    f"indicator {spec.slug!r} has no registered asset "
                    f"({consumer_key!r}) — seed_assets must cover every indicator "
                    "before its edges can be written"
                )
            consumer = ids[consumer_key]
            # Replace rather than merge, so removing an input from a declaration
            # actually removes the edge instead of leaving a phantom dependency
            # that keeps invalidating the indicator forever.
            cur.execute(
                "DELETE FROM curated_geo.asset_dependency WHERE consumer_asset_id = %s",
                (consumer,),
            )
            for edge in _edges_for(spec):
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
                        edge.partition_mapping
                        or _partition_mapping(dimensions[edge.asset_key]),
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
        specs = SHIPPED_ASSETS + scenario_override_assets() + indicator_assets()
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
