"""WorldPop public-API ingest helpers.

Fetches per-country gridded population rasters (the ``wpgp`` product
by default) from `<https://www.worldpop.org/rest/data/>`_, optionally
clips them to an AOI polygon, and produces a local GeoTIFF that can be
fed straight into the existing ``raster ingest`` pipeline. No new
dependencies — uses ``urllib.request`` (matching the INEGI fetch
pattern in :mod:`reimagina.pipeline.inegi`) and the already-installed
``rasterio``/``shapely`` for the optional clip step.

Two-step contract intended for the CLI:

1. :func:`fetch_country_raster` resolves the metadata endpoint, picks
   the GeoTIFF URL for the requested year, and downloads it (cached
   per ``(dataset, country, year)`` so re-runs against different AOIs
   don't re-fetch the country file).
2. :func:`clip_to_aoi_polygon` produces a smaller AOI-scoped GeoTIFF
   ready for ``raster ingest``.

Why a sibling module to ``inegi.py``: the failure modes are different
(WorldPop's REST surface, FTP fallback, JSON-keyed-by-year metadata)
and bundling unrelated providers into one ``http.py`` module would
hide the per-source quirks. Keeping them flat under ``pipeline/`` is
consistent with the rest of the codebase.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORLDPOP_REST_BASE = "https://www.worldpop.org/rest/data"
DEFAULT_DATASET = "wpgp"
# WorldPop's CDN occasionally takes a few seconds to start streaming a
# country-scale GeoTIFF (Mexico is ~893 MB). 60 s mirrors the INEGI
# fetch's connect timeout; the actual transfer can take much longer
# but urllib doesn't enforce a per-byte stall on read once the socket
# is open.
_REQUEST_TIMEOUT_SECONDS = 60
_USER_AGENT = "reimagina-cli/0.1 (+github.com/reimagina) worldpop-fetch"


class WorldPopFetchError(RuntimeError):
    """Raised when the WorldPop API cannot be queried or the GeoTIFF
    download fails. Carries a human-readable message that mentions the
    URL the caller can hit manually as a fallback."""


@dataclass(frozen=True)
class WorldPopMetadata:
    """A single ``(country, year)`` row from the WorldPop catalogue.

    The REST endpoint returns one entry per (country, year, dataset)
    combination. We surface the bits the ingest needs: the FTP URL of
    the GeoTIFF, plus a couple of fields the operator will want in
    the audit trail (``id``, ``title``).
    """

    country: str
    year: int
    dataset: str
    url: str
    title: str | None = None
    id: str | None = None


# ─── Metadata discovery ───────────────────────────────────────────────


def _http_get(url: str) -> bytes:
    """GET ``url`` with our standard User-Agent + timeout. Returns the
    response body. Raises :class:`WorldPopFetchError` on any failure
    so callers don't have to re-derive the manual-fallback hint."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                raise WorldPopFetchError(
                    f"WorldPop API returned status {response.status} for {url}. "
                    f"Confirm the dataset / country code, or fetch the metadata "
                    f"manually from {url} and re-run with --output-path pointing "
                    f"at a local mirror."
                )
            return response.read()
    except urllib.error.URLError as exc:
        raise WorldPopFetchError(
            f"WorldPop API unreachable at {url}: {exc}. "
            f"If the network is restricted, mirror the file manually and skip "
            f"`raster fetch-worldpop` — call `raster ingest` directly on the "
            f"local copy."
        ) from exc


def discover_geotiff_url(
    country: str,
    year: int,
    *,
    dataset: str = DEFAULT_DATASET,
) -> WorldPopMetadata:
    """Resolve the GeoTIFF URL for one ``(country, year, dataset)``.

    The REST catalogue (``/pop/<dataset>?iso3=<C>``) returns a JSON
    payload keyed on ``data.popyear``. We pick the matching year and
    extract the GeoTIFF URL from ``files[0]`` — the FTP URL the
    catalogue surfaces is the canonical artefact.

    Raises :class:`WorldPopFetchError` if no row matches the requested
    year (the operator likely typoed it or asked for a year the
    dataset doesn't cover).
    """
    iso3 = country.upper()
    url = f"{WORLDPOP_REST_BASE}/pop/{dataset}?iso3={iso3}"
    payload = json.loads(_http_get(url).decode("utf-8"))
    rows = payload.get("data") or []
    for row in rows:
        # popyear is sometimes a string ("2020"), sometimes an int.
        # Coerce both sides to int for a stable comparison.
        try:
            row_year = int(row.get("popyear"))
        except (TypeError, ValueError):
            continue
        if row_year != year:
            continue
        files = row.get("files") or []
        if not files:
            raise WorldPopFetchError(
                f"WorldPop catalogue has no `files` entry for "
                f"{dataset}/{iso3}/{year}. The metadata row exists but "
                f"the download URL is missing — the catalogue may be "
                f"mid-update; retry in a few minutes."
            )
        return WorldPopMetadata(
            country=iso3,
            year=year,
            dataset=dataset,
            url=str(files[0]),
            title=row.get("title"),
            id=row.get("id"),
        )
    available = sorted({int(r["popyear"]) for r in rows if r.get("popyear")})
    raise WorldPopFetchError(
        f"WorldPop dataset {dataset!r} for {iso3} has no row for year {year}. "
        f"Available years: {available or '(none returned)'}"
    )


# ─── Country raster fetch ─────────────────────────────────────────────


def _local_cache_name(meta: WorldPopMetadata) -> str:
    """Filename used inside the cache dir. Includes dataset/year/country
    so two parallel ingests don't collide on a single ``raw.tif``."""
    return f"{meta.dataset}_{meta.country}_{meta.year}.tif"


def fetch_country_raster(
    country: str,
    year: int,
    *,
    cache_dir: Path,
    dataset: str = DEFAULT_DATASET,
    force: bool = False,
) -> tuple[Path, WorldPopMetadata]:
    """Download (or reuse cached) the country-scale GeoTIFF.

    ``force=False`` reuses an existing cached file regardless of size —
    we trust the cache because WorldPop releases are immutable per
    ``(dataset, country, year)``. Pass ``force=True`` for an explicit
    re-download (e.g. operator suspects partial-write corruption).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta = discover_geotiff_url(country, year, dataset=dataset)
    cached = cache_dir / _local_cache_name(meta)

    if cached.exists() and not force:
        return cached, meta

    # WorldPop publishes both ftp:// and https:// URLs under ``files``;
    # urlopen handles both. The request below intentionally doesn't
    # set the User-Agent for ftp:// because the FTP layer ignores it
    # — but Python's urllib doesn't error on the extra header either.
    request = urllib.request.Request(meta.url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            # Stream to a sibling .partial so a crash mid-download
            # leaves the cache intact rather than producing a truncated
            # file that future runs would silently consume.
            partial = cached.with_suffix(cached.suffix + ".partial")
            with open(partial, "wb") as out:
                shutil.copyfileobj(response, out)
            partial.replace(cached)
    except urllib.error.URLError as exc:
        if cached.exists() and not force:
            # A partial file from a previous failure shouldn't survive
            # — caller passed force=False so we shouldn't delete a
            # legitimate cached copy though. The .partial cleanup is
            # the only safe thing to do here.
            pass
        partial_path = cached.with_suffix(cached.suffix + ".partial")
        if partial_path.exists():
            partial_path.unlink()
        raise WorldPopFetchError(
            f"Failed to download {meta.url}: {exc}. "
            f"Try `raster fetch-worldpop --force` once connectivity returns, "
            f"or download manually and skip this step."
        ) from exc
    return cached, meta


# ─── AOI clipping ─────────────────────────────────────────────────────


def clip_to_aoi_polygon(
    raster_path: Path | str,
    aoi_geometry_wkt: str,
    output_path: Path | str,
) -> Path:
    """Clip ``raster_path`` to the AOI polygon and write a single-band
    GeoTIFF to ``output_path``.

    Asserts the input is single-band so the downstream ``raster ingest``
    pipeline (which only handles single-band rasters) won't reject the
    clipped artefact at a later, more confusing stage.
    """
    # Local imports keep this module importable in environments without
    # rasterio installed (e.g. unit-test discovery on a thin CI image).
    import rasterio
    from rasterio.mask import mask
    from shapely import wkt as shapely_wkt
    from shapely.geometry import mapping

    src_path = Path(raster_path)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    geometry = shapely_wkt.loads(aoi_geometry_wkt)

    with rasterio.open(src_path) as src:
        if src.count != 1:
            raise WorldPopFetchError(
                f"WorldPop raster {src_path} has {src.count} bands; "
                f"the ingest pipeline only handles single-band rasters. "
                f"Pick a different dataset or pre-process to a single band."
            )
        clipped, transform = mask(src, [mapping(geometry)], crop=True)
        profile = src.profile.copy()

    profile.update(
        {
            "height": clipped.shape[1],
            "width": clipped.shape[2],
            "transform": transform,
            # GeoTIFF (not COG) at this stage — the COG re-encode happens
            # inside ``ingest_raster_source`` so we don't need to do it
            # twice. Using GTiff also keeps the output universally
            # readable by tools that don't speak the COG profile yet.
            "driver": "GTiff",
        }
    )
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(clipped)
    return out_path


# ─── High-level orchestrator (used by the CLI) ────────────────────────


def fetch(
    *,
    country: str,
    year: int,
    output_path: Path | str,
    cache_dir: Path | str | None = None,
    dataset: str = DEFAULT_DATASET,
    aoi_geometry_wkt: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Discover → download → (optional clip) → output.

    Returns a dict of operator-facing summary fields the CLI prints as
    JSON. Keys: ``output_path``, ``country_raster_path``, ``url``,
    ``cached`` (whether the country fetch was a cache hit), ``clipped``
    (boolean), ``meta`` (sub-dict with WorldPop's id/title for the
    audit trail).
    """
    cache_dir_path = Path(cache_dir) if cache_dir else Path("data/cache/worldpop")
    output_path_p = Path(output_path)
    output_path_p.parent.mkdir(parents=True, exist_ok=True)

    cached_existed = (
        cache_dir_path / f"{dataset}_{country.upper()}_{year}.tif"
    ).exists()
    country_raster, meta = fetch_country_raster(
        country=country,
        year=year,
        cache_dir=cache_dir_path,
        dataset=dataset,
        force=force,
    )

    if aoi_geometry_wkt is not None:
        clip_to_aoi_polygon(country_raster, aoi_geometry_wkt, output_path_p)
        clipped = True
    else:
        # Copy (not move) so the cached country file remains available
        # for future AOI clips. shutil.copyfile preserves bytes; no
        # raster reprojection or re-encoding happens here.
        shutil.copyfile(country_raster, output_path_p)
        clipped = False

    return {
        "output_path": str(output_path_p),
        "country_raster_path": str(country_raster),
        "url": meta.url,
        "cached": cached_existed and not force,
        "clipped": clipped,
        "meta": {
            "id": meta.id,
            "title": meta.title,
            "dataset": meta.dataset,
            "country": meta.country,
            "year": meta.year,
        },
    }
