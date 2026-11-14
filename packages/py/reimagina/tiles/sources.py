"""Resolve which tile layers exist, for the API's source manifest.

Read-only: queries Postgres and formats URLs. The PMTiles export half, which
shells out to GDAL, lives in ``tiles.export``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import quote, urlparse

import psycopg2

from reimagina.storage import backend_for_connection
from reimagina.tiles.functions import DEFAULT_TILE_FUNCTION, TILE_FUNCTIONS

from .defaults import (
    tile_live_base_url,
    tile_raster_base_url,
    tile_snapshot_base_url,
    to_host_path,
    to_titiler_url,
)
from .manifest import load_tile_layers


logger = logging.getLogger(__name__)


#: Kept as an alias rather than a second list. The names live in
#: ``tiles.functions``, which the API's own MVT route reads as its allowlist:
#: a function this resolver advertises but that route refuses to serve is a
#: 404 in a map, and two frozensets in two modules is how that happens.
_KNOWN_MARTIN_FUNCTION_TAILS = TILE_FUNCTIONS


def _martin_base_and_default_function(live_tilejson_base_url: str) -> tuple[str, str]:
    """Split a legacy `{base}/{function}` URL into its components.

    Older callers pass live_tilejson_base_url = `http://localhost:3000/get_tile`;
    newer ones will pass `http://localhost:3000` or, behind a reverse
    proxy, `https://host/martin`. To avoid a breaking change AND to
    work behind path-prefixed deployments, only treat the final URL
    segment as a Martin function name when it actually matches a known
    function. Otherwise the whole URL is treated as the base, and the
    default function name (`get_tile`) is used.

    Earlier behaviour split `https://host/martin` into base `https://host`
    + function `martin` — which generated tile URLs that 404'd in
    proxy-prefixed deployments. The whitelist below makes that
    impossible while keeping `http://localhost:3000/get_tile` working.
    """
    parsed = urlparse(live_tilejson_base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        # Pre-URL string (e.g. tests passing `localhost:3000`); fall
        # back to the simple rsplit logic for back-compat.
        trimmed = live_tilejson_base_url.rstrip("/")
        try:
            base, tail = trimmed.rsplit("/", 1)
        except ValueError:
            return trimmed, DEFAULT_TILE_FUNCTION
        if tail in _KNOWN_MARTIN_FUNCTION_TAILS:
            return base, tail
        return trimmed, DEFAULT_TILE_FUNCTION

    path = parsed.path or ""
    if not path:
        return live_tilejson_base_url.rstrip("/"), DEFAULT_TILE_FUNCTION

    head, _, tail = path.rpartition("/")
    if tail in _KNOWN_MARTIN_FUNCTION_TAILS:
        new_base = f"{parsed.scheme}://{parsed.netloc}{head}"
        return new_base.rstrip("/"), tail
    # Anything else (`/martin`, `/api/v1/`, custom path prefix) is
    # part of the base URL; default to `get_tile` for the function.
    return live_tilejson_base_url.rstrip("/"), DEFAULT_TILE_FUNCTION


def resolve_tile_sources(
    conn,
    *,
    live_tilejson_base_url: str | None = None,
    snapshot_base_url: str | None = None,
    raster_base_url: str | None = None,
    manifest_path: str | Path | None = None,
) -> list[dict[str, object]]:
    """Resolve every manifest layer into a frontend-ready tile source.

    Each `live_mvt` layer is turned into `{martin_base}/{function_name}/{z}/{x}/{y}`,
    where `function_name` is derived from `source_relation` (last dotted
    segment — e.g. `serving_geo.indicator_overlay_tile` → `indicator_overlay_tile`),
    falling back to the legacy URL's path component (`get_tile` by default).
    `tile_layer_name` is the MVT layer name inside each tile blob — it's
    what the frontend sets as `source-layer` when adding a style layer, and
    is deliberately NOT conflated with the Martin URL path.

    `query_params` from the manifest are forwarded under
    `source_config.query_params` as schema hints (e.g. `{"indicator_slug":
    "REQUIRED"}`) rather than being baked into the URL — the frontend picks
    real values at render time and assembles the final URL itself.
    """
    live_url = (
        live_tilejson_base_url
        if live_tilejson_base_url is not None
        else tile_live_base_url()
    )
    snapshot_url = (
        snapshot_base_url if snapshot_base_url is not None else tile_snapshot_base_url()
    )
    raster_url = (
        raster_base_url if raster_base_url is not None else tile_raster_base_url()
    )
    martin_base, default_function = _martin_base_and_default_function(live_url)
    backend = backend_for_connection(conn)
    repo = backend.tile_repo()
    resolved: list[dict[str, object]] = []
    for layer in load_tile_layers(manifest_path):
        if layer.source_mode == "live_mvt":
            function_name = (
                layer.source_relation.split(".")[-1]
                if layer.source_relation
                else default_function
            )
            tilejson_url = f"{martin_base}/{function_name}"
            tiles_url_template = f"{martin_base}/{function_name}/{{z}}/{{x}}/{{y}}"
            resolved.append(
                {
                    "layer_id": layer.layer_id,
                    "logical_name": layer.logical_name,
                    "description": layer.description,
                    "source_mode": layer.source_mode,
                    "implemented": layer.implemented,
                    "available": layer.implemented,
                    "source_config": {
                        "kind": "vector",
                        "tilejson_url": tilejson_url,
                        "tiles_url_template": tiles_url_template,
                        "source_layer": layer.tile_layer_name,
                        "query_params": dict(layer.query_params),
                        "minzoom": layer.minzoom,
                        "maxzoom": layer.maxzoom,
                    },
                }
            )
            continue

        latest = repo.get_latest_snapshot(layer_id=layer.layer_id)
        artifact_uri = None
        if latest is not None:
            artifact_uri = latest["artifact_uri"]
            if snapshot_url:
                artifact_uri = (
                    f"{snapshot_url.rstrip('/')}/{Path(latest['artifact_path']).name}"
                )
        resolved.append(
            {
                "layer_id": layer.layer_id,
                "logical_name": layer.logical_name,
                "description": layer.description,
                "source_mode": layer.source_mode,
                "implemented": layer.implemented,
                "available": latest is not None and latest["status"] == "ready",
                "latest_snapshot": latest,
                "source_config": {
                    "kind": "pmtiles",
                    "url": artifact_uri,
                },
            }
        )

    resolved.extend(_resolve_raster_layers(backend, raster_url))
    resolved.extend(_resolve_aggregation_overlays(backend, martin_base))
    resolved.extend(_resolve_source_observation_overlays(backend, martin_base))
    return resolved


def _resolve_raster_layers(backend, raster_base_url: str) -> list[dict[str, object]]:
    """Turn every published ``raster_layer`` row into a frontend source.

    The tile URL follows titiler's standard shape:
    ``{raster_base_url}/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=<cog>``
    with colormap + rescale forwarded as query params when set on the
    layer registry row. ``available=false`` when the COG file is not
    on disk — operators get a clear "the raster is registered but the
    artifact is missing" signal instead of a broken map tile.
    """
    base = raster_base_url.rstrip("/")
    resolved: list[dict[str, object]] = []
    for row in backend.raster_layer_repo().list_published_layers():
        storage_uri = row.get("storage_uri")
        if not storage_uri:
            # Row exists but the silver artifact never landed. Surface
            # it as unavailable rather than emitting a tile URL that
            # will 404 on every request.
            resolved.append(
                _raster_tile_entry(row, tiles_url_template=None, available=False)
            )
            continue
        host_path = to_host_path(storage_uri)
        available = host_path.exists() and host_path.stat().st_size > 0
        cog_url = to_titiler_url(storage_uri)
        # Percent-encode every value: ``url=`` already is (Path.as_uri()
        # escapes path chars), but colormap_name and rescale come straight
        # from the registry, which accepts arbitrary strings. Leaving '&'
        # or '#' un-escaped would silently truncate the query string.
        # ``safe=""`` so that ',' in rescale is also encoded consistently.
        query = [f"url={quote(cog_url, safe=':/')}"]
        if row.get("colormap_name"):
            query.append(f"colormap_name={quote(str(row['colormap_name']), safe='')}")
        if row.get("rescale"):
            query.append(f"rescale={quote(str(row['rescale']), safe='')}")
        qs = "&".join(query)
        tiles_url_template = f"{base}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?{qs}"
        resolved.append(
            _raster_tile_entry(
                row,
                tiles_url_template=tiles_url_template,
                available=available,
                cog_url=cog_url,
            )
        )
    return resolved


def _resolve_aggregation_overlays(backend, martin_base: str) -> list[dict[str, object]]:
    """Turn every enabled aggregation recipe into a tile-sources entry.

    Each enabled recipe → one MVT choropleth source keyed on
    ``(attribute_key, feature_type_slug)``. The ``attribute_key`` is
    derived from the recipe's ``attribute_key_prefix`` + the method's
    canonical suffix (``_sum``, ``_mean``, …); if the recipe overrides
    via ``params['stats']`` we pick the first entry.

    The frontend nests each overlay under its parent raster via
    ``parent_raster_layer_slug``. Recipes whose input is an observation
    (rather than a raster) still produce an overlay entry but don't
    nest — they're "derived layers" with no pixel parent.

    Value range (min/p05/p95/max) is computed on the fly from
    ``curated_geo.observation`` so the frontend can pick a colour
    ramp without a second round-trip. A recipe that's been registered
    but never run returns ``available=false`` and a null value_range,
    so the UI can show the toggle as "not yet computed".
    """
    base = martin_base.rstrip("/")
    resolved: list[dict[str, object]] = []

    recipes = backend.aggregation_recipe_repo().list_recipes(enabled_only=True)

    # Build a chain-walking index so observation-input recipes inherit
    # their transitive raster parent. Otherwise a block→AGEB rollup of
    # WorldPop observations would appear under "no parent raster" even
    # though it's derived from the same WorldPop pixels.
    # Key: the prefix the recipe *writes*; value: the recipe.
    by_output_prefix = {r["attribute_key_prefix"]: r for r in recipes}

    # Pre-aggregate value range + version for ALL keys in one round
    # trip each. The earlier per-recipe loop hit the DB twice (range
    # + version) per recipe — with 13 keys that's 26 queries totalling
    # ~1.5 s. The grouped versions run two GROUP BY scans regardless
    # of key count.
    value_ranges_by_key = _aggregation_value_ranges_all(backend)
    versions_by_key = _latest_observation_versions_all(backend)

    for recipe in recipes:
        attribute_key = _attribute_key_for_recipe(recipe, backend=backend)
        value_range = value_ranges_by_key.get(attribute_key)
        available = value_range is not None and int(value_range["count"]) > 0  # type: ignore[call-overload]
        parent_id, parent_slug = _resolve_transitive_raster_parent(
            recipe,
            by_output_prefix,
        )
        # Cache-buster: latest observation-write timestamp keyed by
        # attribute_key. MapLibre's in-memory tile cache otherwise
        # masks fresh data with stale empty tiles fetched pre-recompute.
        version = versions_by_key.get(attribute_key)
        query_parts = [
            f"attribute_key={quote(attribute_key, safe='')}",
            f"feature_type_slug={quote(recipe['target_feature_type_slug'], safe='')}",
        ]
        if version is not None:
            query_parts.append(f"v={quote(version, safe='')}")
        query = "&".join(query_parts)
        tiles_url_template = (
            f"{base}/aggregation_overlay_tile/{{z}}/{{x}}/{{y}}?{query}"
        )
        resolved.append(
            {
                "layer_id": f"aggregation_overlay::{recipe['id']}",
                "logical_name": _logical_name_for_recipe(recipe, parent_slug),
                "description": _description_for_recipe(recipe),
                "source_mode": "aggregation_overlay",
                "implemented": True,
                "available": available,
                "source_config": {
                    "kind": "vector",
                    "tiles_url_template": tiles_url_template,
                    "source_layer": "aggregation_overlay",
                    "attribute_key": attribute_key,
                    "target_feature_type_slug": recipe["target_feature_type_slug"],
                    "method": recipe["method"],
                    "input_kind": recipe["input_kind"],
                    "parent_raster_layer_id": parent_id,
                    "parent_raster_layer_slug": parent_slug,
                    "value_range": value_range,
                    # z<10 returns empty tiles server-side; surface the floor
                    # so the frontend can set the layer's minzoom to match.
                    "minzoom": 10,
                    "maxzoom": 16,
                },
            }
        )
    return resolved


def _resolve_source_observation_overlays(
    backend,
    martin_base: str,
) -> list[dict[str, object]]:
    """Surface raw (non-recipe) observations as choropleth overlays.

    The aggregation_overlay resolver only emits entries for declared
    ``aggregation_recipe`` rows — i.e. derived rollups. The raw source
    observations (e.g. INEGI 2010 census at ``block`` grain) sit in
    ``curated_geo.observation`` but have no recipe of their own (they
    *are* the input to the rollup recipes). Without this resolver
    operators only see the ageb / municipality summaries in the picker
    and have no way to inspect the per-manzana values they were rolled
    up from.

    For every distinct ``(attribute_key, feature.type_slug)`` pair that
    has at least one current observation **and** isn't already an
    aggregation-recipe output, emit one overlay entry pointing at the
    same ``serving_geo.aggregation_overlay_tile`` function — the tile
    function is recipe-agnostic, so the same query path works for raw
    obs.
    """
    base = martin_base.rstrip("/")
    # AOI affinity per (attribute_key, feature_type_slug): the set of
    # AOI uuids whose features have at least one current observation
    # under this key. Doubles as the pair-list source — the keys of
    # this dict are exactly the (attribute_key, feature_type_slug)
    # tuples we used to fetch via a separate DISTINCT scan. Folding
    # the two queries into one cuts /tile-sources by ~500 ms.
    aoi_affinity = _list_observation_aoi_affinity(backend)
    pairs = sorted(aoi_affinity.keys())
    if not pairs:
        return []

    recipe_outputs: set[str] = set()
    for r in backend.aggregation_recipe_repo().list_recipes(enabled_only=True):
        recipe_outputs.add(_attribute_key_for_recipe(r, backend=backend))

    # Pre-aggregate value-range + cache-buster timestamp for every
    # key — same batched-aggregation trick as the recipe resolver.
    value_ranges_by_key = _aggregation_value_ranges_all(backend)
    versions_by_key = _latest_observation_versions_all(backend)

    # Per-AOI display tokens — used to strip AOI hints from labels so
    # ``pop_worldpop_2020_cdmx_sum`` reads as ``pop_worldpop_2020_sum``
    # in the picker. Operators sometimes embed an AOI shortcode in the
    # ``--attribute-key-prefix`` to dodge SCD prefix collisions when
    # the same metric is computed per-AOI from different source rows;
    # the picker label shouldn't carry that bookkeeping artefact.
    aoi_tokens = _aoi_display_tokens(backend)

    resolved: list[dict[str, object]] = []
    for attribute_key, feature_type_slug in pairs:
        if attribute_key in recipe_outputs:
            continue
        value_range = value_ranges_by_key.get(attribute_key)
        if value_range is None or value_range["count"] == 0:
            continue
        aois = sorted(aoi_affinity.get((attribute_key, feature_type_slug), set()))
        # Strip any AOI-shortcode hint from the displayed key so the
        # picker shows the same logical name regardless of which AOI's
        # observations actually back this key. Single-AOI keys are the
        # only candidates (multi-AOI keys like censo_* are by design
        # AOI-agnostic and shouldn't be stripped).
        display_key = (
            _strip_aoi_token(attribute_key, aoi_tokens.get(aois[0], ()))
            if len(aois) == 1
            else attribute_key
        )
        version = versions_by_key.get(attribute_key)
        query_parts = [
            f"attribute_key={quote(attribute_key, safe='')}",
            f"feature_type_slug={quote(feature_type_slug, safe='')}",
        ]
        if version is not None:
            query_parts.append(f"v={quote(version, safe='')}")
        query = "&".join(query_parts)
        tiles_url_template = (
            f"{base}/aggregation_overlay_tile/{{z}}/{{x}}/{{y}}?{query}"
        )
        resolved.append(
            {
                # Stable layer_id so the frontend's source-cache hits across
                # /tile-sources refreshes. Format mirrors aggregation_overlay::*
                # for visual symmetry but uses ``observation::`` as the kind tag.
                "layer_id": f"observation::{attribute_key}::{feature_type_slug}",
                "logical_name": f"{display_key} ({feature_type_slug})",
                "description": (
                    f"Raw observation values for ``{attribute_key}`` at "
                    f"{feature_type_slug} grain (no rollup applied)."
                ),
                "source_mode": "aggregation_overlay",
                "implemented": True,
                "available": True,
                "source_config": {
                    "kind": "vector",
                    "tiles_url_template": tiles_url_template,
                    "source_layer": "aggregation_overlay",
                    "attribute_key": attribute_key,
                    "target_feature_type_slug": feature_type_slug,
                    "method": "identity",
                    "input_kind": "observation",
                    "parent_raster_layer_id": None,
                    "parent_raster_layer_slug": None,
                    "value_range": value_range,
                    # Frontend reads this to filter the picker by active
                    # AOI. Empty list means "no AOI affinity recorded" —
                    # treat as available for all AOIs (defensive default
                    # for any future code path that writes observations
                    # without going through aoi_feature).
                    "aois": aois,
                    "minzoom": 10,
                    "maxzoom": 16,
                },
            }
        )
    return resolved


def _aoi_display_tokens(backend) -> dict[str, tuple[str, ...]]:
    """``aoi_id → tuple of lowercase tokens`` derived from each AOI's
    name, used by :func:`_strip_aoi_token` to detect and remove an
    AOI-shortcode hint from observation attribute_keys when rendering
    picker labels.

    Tokens are extracted by splitting on whitespace + non-alphanumeric
    characters (so ``"CDMX (all)"`` yields ``("cdmx", "all")``). Names
    that lower-case to a single short token (≤6 chars) get an
    additional 3-char prefix entry so common shortenings still match
    — ``Mexicali`` produces ``("mexicali", "mex")`` so a key ending
    in ``_mex_sum`` strips correctly.
    """

    out: dict[str, tuple[str, ...]] = {}
    conn = backend.connection
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, name FROM curated_geo.area_of_interest")
        rows = cur.fetchall()
    for aoi_id, name in rows:
        if not name:
            continue
        raw_tokens = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", name) if t]
        # Long single-word names (e.g. "Mexicali") get a 3-letter
        # prefix added so ``_mex`` matches as well as ``_mexicali``.
        # Short tokens already look like shortcodes, leave alone.
        expanded: list[str] = []
        for t in raw_tokens:
            expanded.append(t)
            if len(t) >= 6:
                expanded.append(t[:3])
        # De-dup while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for t in expanded:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        out[str(aoi_id)] = tuple(unique)
    return out


# Conventional stat suffixes the worker writes after the operator's
# attribute_key_prefix. Used to spot the AOI-shortcode segment in a
# key like ``pop_worldpop_2020_cdmx_sum`` (segments
# ``[pop, worldpop, 2020, cdmx, sum]`` — last is a stat → previous is
# a candidate AOI hint).


# Conventional stat suffixes the worker writes after the operator's
# attribute_key_prefix. Used to spot the AOI-shortcode segment in a
# key like ``pop_worldpop_2020_cdmx_sum`` (segments
# ``[pop, worldpop, 2020, cdmx, sum]`` — last is a stat → previous is
# a candidate AOI hint).
_STAT_SUFFIXES = frozenset(
    {
        "sum",
        "count",
        "mean",
        "median",
        "min",
        "max",
        "stdev",
        "std",
        "p05",
        "p50",
        "p95",
        "quantile_5",
        "quantile_50",
        "quantile_95",
    }
)


def _strip_aoi_token(attribute_key: str, aoi_tokens: tuple[str, ...]) -> str:
    """Return ``attribute_key`` with its AOI-shortcode segment removed.

    Heuristic: split on ``_`` ; if the second-to-last segment matches
    any token in ``aoi_tokens`` (case-insensitive), drop it. Returns
    the input unchanged when no match is found, or when removing the
    segment would collapse the key to <2 segments (defensive, avoids
    pathological short prefixes like a hand-crafted ``cdmx_sum``).
    """
    if not aoi_tokens:
        return attribute_key
    parts = attribute_key.split("_")
    if len(parts) < 3:
        return attribute_key
    last = parts[-1].lower()
    candidate_idx = -2 if last in _STAT_SUFFIXES else -1
    candidate = parts[candidate_idx].lower()
    if candidate in {t.lower() for t in aoi_tokens}:
        return "_".join(parts[:candidate_idx] + parts[candidate_idx + 1 :])
    return attribute_key


def _list_observation_aoi_affinity(
    backend,
) -> dict[tuple[str, str], set[str]]:
    """Map ``(attribute_key, feature.type_slug) → {aoi_id, ...}`` for
    every current observation. The AOI list comes from
    :class:`curated_geo.aoi_feature` joined through the observation's
    feature — so an attribute computed against only-CDMX features
    surfaces a single-element AOI set, while one computed against
    both AOIs (e.g. the INEGI censo block-level prefixes) surfaces a
    two-element set."""
    conn = backend.connection
    sql = """
        SELECT DISTINCT o.attribute_key, f.type_slug, af.aoi_id::text
        FROM curated_geo.observation o
        JOIN curated_geo.feature f ON f.id = o.feature_id
        JOIN curated_geo.aoi_feature af ON af.feature_id = f.id
        WHERE o.is_current
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    affinity: dict[tuple[str, str], set[str]] = {}
    for attr_key, type_slug, aoi_id in rows:
        affinity.setdefault((str(attr_key), str(type_slug)), set()).add(str(aoi_id))
    return affinity


def _list_source_observation_pairs(backend) -> list[tuple[str, str]]:
    """Distinct (attribute_key, feature.type_slug) pairs for current obs.

    A single SQL query returns the full pair list; the caller filters
    out recipe outputs in Python.
    """
    conn = backend.connection
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT o.attribute_key, f.type_slug
            FROM curated_geo.observation o
            JOIN curated_geo.feature f ON f.id = o.feature_id
            WHERE o.is_current
            ORDER BY o.attribute_key, f.type_slug
            """
        )
        rows = cur.fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _resolve_transitive_raster_parent(
    recipe: dict[str, object],
    by_output_prefix: dict[str, dict[str, object]],
) -> tuple[object, object]:
    """Walk the recipe chain backward until we hit a raster input.

    A recipe with ``input_kind='raster'`` is its own parent: return
    (input_raster_layer_id, input_raster_layer_slug) directly.

    A recipe with ``input_kind='observation'`` points at another
    recipe's output prefix via ``input_attribute_key_prefix``. Look it
    up, recurse. Stops when the chain hits a raster or when the next
    step is missing / non-raster-derived — in that case there's no
    meaningful raster parent (e.g. the input prefix was written by a
    non-recipe worker like a hand-imported CSV).

    Cycle-safe: a ``seen`` set guards against self-referential chains,
    which the DB's UNIQUE (target, attribute_key_prefix) already makes
    hard to create but not impossible (an operator could point a recipe
    at its own output prefix via SQL).
    """
    seen: set[str] = set()
    cursor = recipe
    while True:
        recipe_id = str(cursor["id"])
        if recipe_id in seen:
            return None, None
        seen.add(recipe_id)
        if cursor["input_kind"] == "raster":
            return cursor.get("input_raster_layer_id"), cursor.get(
                "input_raster_layer_slug"
            )
        # Observation input — step up to the recipe that produced this prefix.
        up_prefix = cursor.get("input_attribute_key_prefix")
        if not up_prefix:
            return None, None
        upstream = by_output_prefix.get(str(up_prefix))
        if upstream is None:
            # Input observations exist (e.g. hand-imported) but no recipe
            # produced them — no raster lineage.
            return None, None
        cursor = upstream


def _attribute_key_for_recipe(
    recipe: dict[str, object],
    backend=None,
) -> str:
    """Resolve the attribute_key the tile function should render.

    Order of precedence:
      1. ``params.stats[0]`` — explicit override (e.g. recipe emits
         both ``_sum`` and ``_count`` but only one is plot-worthy).
      2. Observation rollups whose input was a bare prefix (no
         method suffix) produce a bare output prefix too. Probe the
         observation table for the bare key first; if present, use it.
         Census rollups (``censo_2020_pobtot`` → ``censo_2020_pobtot_ageb``)
         land here.
      3. ``<prefix>_<method>`` — the raster default and the
         observation-arm default when the input had a method suffix
         (e.g. ``pop_worldpop_2020_sum`` → ``..._ageb_sum``).
    """
    prefix = str(recipe["attribute_key_prefix"])
    params = recipe.get("params") or {}
    stats = params.get("stats") if isinstance(params, dict) else None
    if stats:
        return f"{prefix}_{stats[0]}"

    if backend is not None and recipe.get("input_kind") == "observation":
        if _observation_key_exists(backend, prefix):
            return prefix

    return f"{prefix}_{recipe['method']}"


def _observation_key_exists(backend, attribute_key: str) -> bool:
    """True iff at least one current observation row uses the exact key.

    Used by the resolver to detect the bare-prefix output convention
    that ``observation_rollup`` writes when the input has no method
    suffix. One indexed point lookup; result is callsite-cached by
    being invoked once per recipe.

    On a transient psycopg2 error (closed connection mid-request,
    server restart) we log + fall back to ``False`` so /tile-sources
    still serves SOMETHING — but we DON'T swallow programmer errors
    like a missing column. A bare ``except Exception`` would mask
    genuine bugs as silent fallback to the suffixed convention.
    """
    try:
        with backend.connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM curated_geo.observation "
                "WHERE attribute_key = %s AND is_current LIMIT 1",
                (attribute_key,),
            )
            return cur.fetchone() is not None
    except psycopg2.Error as exc:
        logger.warning(
            "tile resolver: observation-key probe failed for %r — "
            "falling back to suffixed convention. Error: %s",
            attribute_key,
            exc,
        )
        return False


def _logical_name_for_recipe(
    recipe: dict[str, object],
    parent_slug: object | None = None,
) -> str:
    """Human-readable label shown under each raster in the sidebar.

    When ``parent_slug`` is resolved (the transitive raster ancestor),
    the label focuses on the grain + method the operator cares about
    and drops the redundant raster slug — the UI already groups every
    recipe under that raster's name, so repeating it in every child row
    is just noise.

    For orphan observation chains (the census rollup case where every
    recipe shares the same input_feature_type + method) the label
    surfaces the input attribute prefix so seven pobtot/pobmas/…
    recipes don't all collapse to the same string in the picker.
    """
    target = recipe["target_feature_type_slug"]
    method = recipe["method"]
    if parent_slug:
        if recipe["input_kind"] == "observation":
            via = recipe.get("input_feature_type_slug")
            return f"Aggregated to {target} ({method}, via {via})"
        return f"Aggregated to {target} ({method})"

    # No raster ancestor — fully-orphan observation chain. Lead with
    # the input attribute family so identical (target, method) tuples
    # stay distinguishable.
    if recipe["input_kind"] == "observation":
        input_prefix = recipe.get("input_attribute_key_prefix")
        if input_prefix:
            return f"{input_prefix} → {target} ({method})"
    return f"rollup → {target} ({recipe.get('input_feature_type_slug')} · {method})"


def _description_for_recipe(recipe: dict[str, object]) -> str:
    if recipe["input_kind"] == "raster":
        parent = recipe.get("input_raster_layer_slug") or "unknown raster"
        return (
            f"Zonal {recipe['method']} of {parent} per "
            f"{recipe['target_feature_type_slug']}"
        )
    return (
        f"Rollup of {recipe.get('input_attribute_key_prefix')} from "
        f"{recipe.get('input_feature_type_slug')} to "
        f"{recipe['target_feature_type_slug']} via {recipe['method']}"
    )


def _latest_observation_version(backend, attribute_key: str) -> str | None:
    """Return a cache-busting token for the current observation set under
    ``attribute_key``. Uses ``max(updated_at)`` — a monotonic proxy for
    "these observations were last rewritten at time X". Safe ignoring of
    sub-second precision: two re-runs of the same recipe within the same
    second would yield the same token, but in practice the recipe writes
    take longer than a second and operators reload the page after.

    The Martin tile function ignores unknown query params, so passing
    ``?v=<epoch>`` doesn't break the SQL — it just makes the URL unique.
    """
    conn = backend.connection
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM max(updated_at))::bigint
            FROM curated_geo.observation
            WHERE is_current AND attribute_key = %s
            """,
            (attribute_key,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return str(row[0])


def _aggregation_value_ranges_all(backend) -> dict[str, dict[str, object]]:
    """Compute count/min/p05/p50/p95/max for every current attribute_key
    in one round-trip. ``GROUP BY attribute_key`` lets Postgres do the
    full-table scan once and bucket per key, instead of the resolver
    calling :func:`_aggregation_value_range` once per key (each
    triggering its own scan + percentile sort).

    Cuts /tile-sources from ~2 s to ~150 ms when there are >10 keys
    in the catalog.
    """
    conn = backend.connection
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attribute_key,
                   count(*),
                   min(value_numeric),
                   percentile_cont(0.05) WITHIN GROUP (ORDER BY value_numeric),
                   percentile_cont(0.50) WITHIN GROUP (ORDER BY value_numeric),
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY value_numeric),
                   max(value_numeric)
            FROM curated_geo.observation
            WHERE is_current AND value_numeric IS NOT NULL
            GROUP BY attribute_key
            """
        )
        rows = cur.fetchall()
    out: dict[str, dict[str, object]] = {}
    for key, count, vmin, p05, p50, p95, vmax in rows:
        if not count:
            continue
        out[str(key)] = {
            "count": int(count),
            "min": float(vmin) if vmin is not None else None,
            "p05": float(p05) if p05 is not None else None,
            "p50": float(p50) if p50 is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "max": float(vmax) if vmax is not None else None,
        }
    return out


def _latest_observation_versions_all(backend) -> dict[str, str]:
    """Cache-bust tokens for every current attribute_key in one query.
    Same rationale as :func:`_aggregation_value_ranges_all` — one
    GROUP BY pass instead of N point lookups."""
    conn = backend.connection
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attribute_key,
                   EXTRACT(EPOCH FROM max(updated_at))::bigint
            FROM curated_geo.observation
            WHERE is_current
            GROUP BY attribute_key
            """
        )
        rows = cur.fetchall()
    return {str(key): str(epoch) for key, epoch in rows if epoch is not None}


def _aggregation_value_range(backend, attribute_key: str) -> dict[str, object] | None:
    """Cheap single-aggregate scan of observations to seed a colour ramp.

    Returns None if the table hasn't received any rows for this key yet
    (recipe registered but never run) — callers surface that as
    ``available=false``. The query is bounded by the partial index on
    ``(feature_id, attribute_key) WHERE is_current`` so cost is roughly
    O(live observations under this key).
    """
    # Raw SQL: the repo interface doesn't expose an aggregate helper and
    # widening it for one tile-resolver call isn't worth the surface.
    conn = backend.connection
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              count(*),
              min(value_numeric),
              percentile_cont(0.05) WITHIN GROUP (ORDER BY value_numeric),
              percentile_cont(0.50) WITHIN GROUP (ORDER BY value_numeric),
              percentile_cont(0.95) WITHIN GROUP (ORDER BY value_numeric),
              max(value_numeric)
            FROM curated_geo.observation
            WHERE is_current AND attribute_key = %s AND value_numeric IS NOT NULL
            """,
            (attribute_key,),
        )
        count, vmin, p05, p50, p95, vmax = cur.fetchone()
    if not count:
        return None
    return {
        "count": int(count),
        "min": float(vmin) if vmin is not None else None,
        "p05": float(p05) if p05 is not None else None,
        "p50": float(p50) if p50 is not None else None,
        "p95": float(p95) if p95 is not None else None,
        "max": float(vmax) if vmax is not None else None,
    }


def _raster_tile_entry(
    row: dict[str, object],
    *,
    tiles_url_template: str | None,
    available: bool,
    cog_url: str | None = None,
) -> dict[str, object]:
    return {
        "layer_id": row["layer_slug"],
        "logical_name": row.get("logical_name"),
        "description": row.get("description"),
        "source_mode": "raster_cog",
        "implemented": True,
        "available": available,
        "source_config": {
            "kind": "raster",
            "tiles_url_template": tiles_url_template,
            "cog_url": cog_url,
            "colormap_name": row.get("colormap_name"),
            "rescale": row.get("rescale"),
            "minzoom": row.get("minzoom"),
            "maxzoom": row.get("maxzoom"),
        },
    }
