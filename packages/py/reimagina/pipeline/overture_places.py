"""Ingest park, recreation and cultural destinations from Overture ``places``.

**The gap this fills.** ``accessibility_block_park_network_m`` measures metres
from each block to the nearest park or recreation site. On the Mexicali pilot it
reported 1,500 m — the cutoff — for all 19,058 blocks, because the IMIP amenity
layer holds only ``Consultorio medico``, ``Farmacia``, ``Hospital``,
``Guarderia`` and ``Cine``. There was nothing park-like to route to, so the
multi-source Dijkstra had no start vertices and every block reported the cap.
The map drew that as a flat choropleth, which reads as a measurement.

Overture's ``places`` theme carries the missing destinations in the *same* S3
release as the buildings this repository already ingests, under the same
licence: no new credential, no new network allowlist entry, no new mirror on
disk. Which of its 221 categories count is
:mod:`reimagina.data.amenities` — deliberately a neutral module, because the
same vocabulary is needed by the indicator catalog, and ``pipeline`` and
``indicators`` do not otherwise import each other.

**Places land as ``global:establishment``**, the same feature type as the IMIP
amenities, with ``attributes->>'category'`` carrying the Overture
``basic_category`` verbatim. That is what
``reimagina.indicators.accessibility`` matches on, so a loaded place is
immediately a destination with no further wiring.

⚠ **The schema moved.** Overture places no longer has the ``categories``
struct with a ``primary`` field that most documentation and example code still
shows. Release 2026-10-21.0 has a flat ``basic_category`` string and a
``taxonomy`` struct. Reading the old path does not error — it yields ``None``
for every row, so the filter matches nothing and the ingest cheerfully loads
zero places. That is how the first version of this module reported "0 park
destinations in Mexicali", which was wrong and looked authoritative.
:func:`_require_expected_columns` makes that failure loud.

⚠ **The bbox subset is NOT clipped to the AOI**, unlike the buildings. A park
200 m outside the municipal boundary is a park the adjacent blocks walk to, and
clipping it away would bias every edge block's distance upward. The pedestrian
network decides what is actually reachable. :func:`summarize_places_layer`
reports the inside/outside split so the choice stays visible.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import geopandas as gpd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as pads
import shapely
from pyarrow import fs as pafs

from reimagina.data import amenities as amen
from reimagina.data import catalog as cat
from reimagina.data import feature as feat

from . import bronze, silver
from .loader import load_vector_source
from .overture import (
    OVERTURE_DEFAULT_RELEASE,
    OVERTURE_LICENSE,
    OVERTURE_PROVIDER,
    OVERTURE_S3_REGION,
    available_overture_releases,
    overture_theme_type_glob,
)

logger = logging.getLogger(__name__)

#: Overture theme/type holding point-geometry places.
PLACES_THEME = "places"
PLACES_TYPE = "place"

#: Feature type places land as — the same one the IMIP amenities use, so the
#: accessibility indicators pick them up without a catalog change.
PLACES_FEATURE_TYPE_CONTEXT = "global"
PLACES_FEATURE_TYPE_SLUG = "establishment"

#: Columns read from the release. Explicit because the theme is wide and the
#: geometry is the expensive column.
_SELECT_COLUMNS = (
    "id",
    "basic_category",
    "names",
    "confidence",
    "addresses",
    "bbox",
)

#: Columns whose absence means the schema moved under us. ``basic_category``
#: replaced the old ``categories.primary``; see the module docstring.
_REQUIRED_COLUMNS = ("id", "basic_category", "geometry", "bbox")

PLACES_SOURCE_NAME = "Overture Places — park, recreation and cultural sites"
PLACES_BRONZE_FILENAME = "overture_places.parquet"
PLACES_NOTES = (
    "Park, recreation and cultural destinations extracted from the Overture "
    "places theme for one bbox, filtered to the category allowlist in "
    "reimagina.data.amenities. Feeds the accessibility network indicators."
)
PLACES_PENDING_STORAGE_URI = "pending://overture/places"


class OverturePlacesSchemaChanged(RuntimeError):
    """The release does not carry the columns this module reads.

    Its own error, rather than a ``KeyError`` from deep inside pyarrow, because
    the remedy is specific: look at what the release actually has and update
    ``_SELECT_COLUMNS``. A silent zero-row ingest is the failure this prevents.
    """


def _require_expected_columns(available: list[str]) -> None:
    missing = [c for c in _REQUIRED_COLUMNS if c not in available]
    if not missing:
        return
    raise OverturePlacesSchemaChanged(
        f"Overture places is missing {missing}. Present: {sorted(available)}. "
        "This theme's category field has already been renamed once — "
        "`categories.primary` became `basic_category` — and reading the old "
        "path yields None for every row rather than raising, so the ingest "
        "loads zero places and looks successful. Update _SELECT_COLUMNS / "
        "_REQUIRED_COLUMNS in pipeline/overture_places.py to match."
    )


def extract_places_subset(
    *,
    output_path: str | Path = PLACES_BRONZE_FILENAME,
    bbox: tuple[float, float, float, float],
    release: str = OVERTURE_DEFAULT_RELEASE,
    categories: tuple[str, ...] | None = None,
    country: str | None = None,
) -> Path:
    """Write the bbox's allowlisted places to a local GeoParquet.

    Filters on the ``bbox`` struct for row-group pruning *and* on
    ``basic_category`` so only destinations an indicator reads come back —
    1,303 rows of the Mexicali bbox's 43,037. Both predicates are pushed into
    pyarrow, so the memory ceiling the buildings extract needs
    ``low_memory`` for does not arise here.

    ``country`` keeps only places whose first address carries that ISO code.

    ⚠ **Pass it for any bbox that crosses a national border.** Mexicali's does:
    a bbox wide enough to hold the municipio also holds Calexico, El Centro and
    Heber, and the release returns 156 US destinations among the 1,303 — 36 of
    them parks. The pedestrian network crosses the border too, at the ports of
    entry, so without this filter a block in northern Mexicali can be reported
    700 m from a park in California that no resident walks to. The field is
    fully populated here (1,147 MX / 156 US, no nulls) and splits exactly at the
    border: MX tops out at 32.6702 N and US starts at 32.6649 N.

    Applied after the scan rather than pushed down, because ``addresses`` is a
    list of structs and the subset is already small; the cost is fetching 156
    rows that are then dropped.
    """
    categories = categories or amen.overture_categories_to_load()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    glob = overture_theme_type_glob(
        release=release, theme=PLACES_THEME, feature_type=PLACES_TYPE
    )
    prefix = glob.removeprefix("s3://").removesuffix("/*.parquet")
    s3 = pafs.S3FileSystem(anonymous=True, region=OVERTURE_S3_REGION)
    try:
        dataset = pads.dataset(prefix, filesystem=s3, format="parquet")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Overture release not found: {exc}. Overture keeps only its most "
            f"recent release, so a pin older than a few weeks stops resolving. "
            f"Available now: {', '.join(available_overture_releases()) or '(none)'}. "
            f"Set REIMAGINA_OVERTURE_RELEASE, or bump OVERTURE_DEFAULT_RELEASE "
            f"in pipeline/overture.py."
        ) from exc

    _require_expected_columns(list(dataset.schema.names))

    min_lon, min_lat, max_lon, max_lat = bbox
    flt = (
        (pc.field("bbox", "xmax") >= min_lon)
        & (pc.field("bbox", "xmin") <= max_lon)
        & (pc.field("bbox", "ymax") >= min_lat)
        & (pc.field("bbox", "ymin") <= max_lat)
        & pc.is_in(
            pc.field("basic_category"),
            value_set=pa.array(categories, type=pa.string()),
        )
    )
    table = dataset.to_table(filter=flt, columns=[*_SELECT_COLUMNS, "geometry"])
    logger.info(
        "Overture places: %s rows in bbox across %s allowlisted categories",
        f"{table.num_rows:,}",
        len(categories),
    )

    gdf = _to_geodataframe(table)
    if country is not None:
        before = len(gdf)
        gdf = gdf[gdf["country"] == country].copy()
        logger.info(
            "Overture places: kept %s of %s rows in country=%s",
            f"{len(gdf):,}",
            f"{before:,}",
            country,
        )
    gdf = _drop_excluded_by_name(gdf)
    gdf = gdf.drop(columns=["country"])
    gdf.to_parquet(output_path)
    return output_path


def _drop_excluded_by_name(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Remove places whose name disqualifies them, and say what went.

    The reasons are logged individually rather than counted, because an
    exclusion rule that starts deleting real parks is only visible if the names
    it removes are on screen. See :data:`reimagina.data.amenities.
    PLACE_NAME_EXCLUSIONS` for why the rule exists at all.
    """
    reasons = gdf["name"].map(amen.excluded_by_name)
    keep = reasons.isna()
    for reason, group in gdf[~keep].groupby(reasons[~keep]):
        logger.info(
            "Overture places: dropped %s (%s): %s",
            len(group),
            reason,
            ", ".join(sorted(group["name"].astype(str))[:6])
            + (" ..." if len(group) > 6 else ""),
        )
    return gdf[keep].copy()


def _first_country(row: dict[str, Any]) -> str | None:
    """ISO country code from the place's first address, if it has one."""
    addresses = row.get("addresses") or []
    return addresses[0].get("country") if addresses else None


def _to_geodataframe(table) -> gpd.GeoDataFrame:
    """Flatten the arrow table into the columns the curated load expects.

    ``names`` is a nested struct and ``geometry`` is WKB, neither of which
    ``load_vector_source`` can read. Flattening here rather than letting
    GeoPandas round-trip the nested schema keeps the Silver artifact a plain
    GeoParquet that QGIS and DBeaver open without complaint.
    """
    rows = table.to_pylist()
    names = []
    for row in rows:
        primary = (row.get("names") or {}).get("primary")
        # Fall back to the category: `name_col` becomes the map label, and an
        # unnamed park is still a park. An empty label would render as a blank
        # popup and read as missing data.
        names.append(primary or row.get("basic_category") or "unnamed place")

    gdf = gpd.GeoDataFrame(
        {
            "id": [row["id"] for row in rows],
            "name": names,
            "category": [row.get("basic_category") for row in rows],
            "confidence": [row.get("confidence") for row in rows],
            # Dropped before the Silver write; it exists only to filter on.
            "country": [_first_country(row) for row in rows],
        },
        geometry=shapely.from_wkb([row["geometry"] for row in rows]),
        crs="EPSG:4326",
    )
    return gdf


def ensure_places_source(
    conn,
    *,
    version: str = OVERTURE_DEFAULT_RELEASE,
    source_name: str = PLACES_SOURCE_NAME,
    notes: str = PLACES_NOTES,
) -> str:
    """Idempotently register the ``data_source_catalog`` row for places.

    Idempotent on ``(name, provider, version)`` like the amenity layers in
    ``pipeline.points``, so re-running the ingest for a new bbox reuses the
    release's row rather than accumulating one per run. The licence is read
    back out of this row by ``/tile-sources/attribution`` — which is why the
    map credits Overture rather than a hardcoded string.
    """
    existing = cat.get_source(
        conn, name=source_name, provider=OVERTURE_PROVIDER, version=version
    )
    if existing is not None:
        return str(existing["id"])
    return cat.register_source(
        conn,
        name=source_name,
        provider=OVERTURE_PROVIDER,
        storage_type="vector",
        integration_role="enrichment",
        storage_uri=PLACES_PENDING_STORAGE_URI,
        version=version,
        license=OVERTURE_LICENSE,
        notes=notes,
    )


def stage_places_subset(raw_path: str | Path, *, source_id: str) -> tuple[Path, Path]:
    """Land the extract to Bronze and write the cleaned Silver GeoParquet."""
    bronze_path = bronze.land_file(raw_path, source_id)
    gdf = silver.read_vector(bronze_path)
    silver_path = silver.save_silver_vector(
        gdf, source_id, "overture_places_silver.parquet"
    )
    return bronze_path, silver_path


def load_places_subset(
    conn,
    *,
    source_id: str,
    silver_path: str | Path,
    orchestrator_run_id: str,
    dataset_version: str = OVERTURE_DEFAULT_RELEASE,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Load staged places into ``curated_geo.feature`` as establishments."""
    type_id = feat.get_type_id(
        conn, PLACES_FEATURE_TYPE_CONTEXT, PLACES_FEATURE_TYPE_SLUG
    )
    return load_vector_source(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        type_id=type_id,
        dataset_version=dataset_version,
        source_record_ref_col="id",
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
        name_col="name",
        attributes_cols=["category", "confidence"],
    )


def summarize_places_layer(
    conn, *, source_id: str, aoi_id: str | None = None
) -> dict[str, Any]:
    """Per-category counts for the loaded places, and the AOI split.

    The per-category breakdown is the point: a total tells you the ingest ran,
    while the breakdown tells you whether the layer is mostly parks or mostly
    gyms — which is the difference between two readings of the same indicator.
    See the composition note on ``OVERTURE_PARK_CATEGORIES``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.attributes->>'category' AS category, count(*) AS n
            FROM curated_geo.feature f
            WHERE f.source_id = %s::uuid
            GROUP BY 1 ORDER BY 2 DESC, 1
            """,
            (source_id,),
        )
        by_category = {row[0]: int(row[1]) for row in cur.fetchall()}

        inside: int | None = None
        if aoi_id is not None:
            cur.execute(
                """
                SELECT count(*)
                FROM curated_geo.feature f
                JOIN curated_geo.area_of_interest aoi ON aoi.id = %s::uuid
                WHERE f.source_id = %s::uuid
                  AND ST_Intersects(f.geometry, aoi.geometry)
                """,
                (aoi_id, source_id),
            )
            inside = int(cur.fetchone()[0])

    total = sum(by_category.values())
    per_amenity = {
        key: sum(by_category.get(c, 0) for c in cats)
        for key, cats in amen.OVERTURE_AMENITY_CATEGORIES.items()
    }
    return {
        "places_loaded": total,
        "by_category": by_category,
        "by_amenity_category": per_amenity,
        "inside_aoi": inside,
        "outside_aoi": None if inside is None else total - inside,
    }


def bootstrap_places(
    conn,
    *,
    bbox: tuple[float, float, float, float],
    orchestrator_run_id: str,
    release: str = OVERTURE_DEFAULT_RELEASE,
    categories: tuple[str, ...] | None = None,
    country: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    output_path: str | Path = PLACES_BRONZE_FILENAME,
) -> dict[str, Any]:
    """Extract, stage, load and summarize places for one bbox."""
    feat.seed_feature_types(conn)
    conn.commit()

    source_id = ensure_places_source(conn, version=release)
    conn.commit()

    raw_path = extract_places_subset(
        output_path=output_path,
        bbox=bbox,
        release=release,
        categories=categories,
        country=country,
    )
    bronze_path, silver_path = stage_places_subset(raw_path, source_id=source_id)
    cat.update_source_storage_uri(
        conn, source_id, bronze.bronze_uri(source_id, bronze_path.name)
    )
    conn.commit()

    load_result = load_places_subset(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        orchestrator_run_id=orchestrator_run_id,
        dataset_version=release,
        code_version=code_version,
    )
    summary = summarize_places_layer(conn, source_id=source_id, aoi_id=aoi_id)
    return {
        "source_id": source_id,
        "bronze_path": str(bronze_path),
        "silver_path": str(silver_path),
        **load_result,
        **summary,
    }
