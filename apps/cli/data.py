"""``data`` CLI subcommands — currently just the verify-sources smoke test.

Why a separate ``data`` group rather than tucking this under each ingest
subcommand: the operator running it hasn't ingested anything yet. They
want a single command they can run on a fresh machine that confirms
*all* upstream sources (Azure data lake mirror + Overture S3 + WorldPop
REST API) are reachable before committing to an hour of ingest work.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from reimagina.pipeline.overture import (
    OVERTURE_DEFAULT_RELEASE,
    OVERTURE_S3_REGION,
    overture_theme_type_glob,
)
from reimagina.pipeline.worldpop import (
    DEFAULT_DATASET as WORLDPOP_DEFAULT_DATASET,
    WORLDPOP_REST_BASE,
)


# Manifest of expected azure data-lake mirror paths. Keep this short
# (only the paths the runbook actually consumes) — extending it with
# every staged file would dilute the smoke-test value.
AZURE_LAKE_MANIFEST: tuple[tuple[str, str], ...] = (
    (
        "INEGI marco geoestadístico 2024 (block / AGEB / municipality)",
        "data/sources/azure/inegi/marco_geostadistico/2024",
    ),
    (
        "INEGI 2020 census (manzanas)",
        "data/sources/azure/inegi/censo/manzanas_2020.parquet",
    ),
    (
        "Tlalpan amenities (SIGCDMX)",
        "data/sources/azure/sigcdmx/amenities/exe_amenities_tlalpan_complete.geoparquet",
    ),
    (
        "Mexicali amenities (EXE)",
        "data/sources/azure/exe/002_mexicali/equipamientos.geoparquet",
    ),
)


def _check_path(label: str, path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"label": label, "path": path, "ok": False, "reason": "missing"}
    # For directories, require at least one parquet file beneath them.
    # Hive-partitioned parquet shows up as a directory; a single-file
    # geoparquet shows up as a file ending in ``.parquet`` /
    # ``.geoparquet``. Both are acceptable.
    if p.is_dir():
        children = list(p.rglob("*.parquet"))
        if not children:
            return {"label": label, "path": path, "ok": False, "reason": "empty_dir"}
        return {"label": label, "path": path, "ok": True, "files": len(children)}
    return {"label": label, "path": path, "ok": True, "files": 1}


def _check_overture_release(release: str) -> dict[str, Any]:
    """HEAD-style check: ask DuckDB to ``glob()`` the Overture S3 release
    and report whether at least one parquet matches. Cheap (no data
    transferred) but proves the bucket is reachable + the release exists."""
    glob = overture_theme_type_glob(release=release)
    try:
        import duckdb  # local import: cli should still load if duckdb is missing
    except ImportError as exc:  # pragma: no cover — dev env always has duckdb
        return {
            "label": f"Overture release {release}",
            "path": glob,
            "ok": False,
            "reason": f"duckdb not installed ({exc})",
        }
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        conn.execute(f"SET s3_region='{OVERTURE_S3_REGION}';")
        rows = conn.execute(f"SELECT count(*) FROM glob('{glob}')").fetchone()
        count = int(rows[0]) if rows else 0
        if count == 0:
            return {
                "label": f"Overture release {release}",
                "path": glob,
                "ok": False,
                "reason": "no_parquet_files_match — release likely not published",
            }
        return {
            "label": f"Overture release {release}",
            "path": glob,
            "ok": True,
            "files": count,
        }
    except Exception as exc:  # pragma: no cover — network failures
        return {
            "label": f"Overture release {release}",
            "path": glob,
            "ok": False,
            "reason": f"glob_failed: {exc.__class__.__name__}: {exc}",
        }
    finally:
        conn.close()


def _check_worldpop_rest(
    *,
    country: str = "MEX",
    dataset: str = WORLDPOP_DEFAULT_DATASET,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Probe the WorldPop REST catalogue. Cheap GET (a few KB JSON).

    A 200 with a non-empty ``data`` array means the API is up *and* the
    requested ``(dataset, country)`` slug is valid. Anything else is
    surfaced as a failure so the operator can fix it before kicking
    off `raster fetch-worldpop`.
    """
    url = f"{WORLDPOP_REST_BASE}/pop/{dataset}?iso3={country.upper()}"
    label = f"WorldPop REST · {dataset}/{country.upper()}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "reimagina-cli/0.1 verify-sources"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return {
                    "label": label,
                    "path": url,
                    "ok": False,
                    "reason": f"http_{response.status}",
                }
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {
            "label": label,
            "path": url,
            "ok": False,
            "reason": f"unreachable: {exc}",
        }
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"label": label, "path": url, "ok": False, "reason": f"bad_json: {exc}"}

    rows = payload.get("data") or []
    if not rows:
        return {
            "label": label,
            "path": url,
            "ok": False,
            "reason": "no_rows — dataset/country combination unknown to WorldPop",
        }
    return {"label": label, "path": url, "ok": True, "rows": len(rows)}


def verify_sources(
    *,
    release: str = OVERTURE_DEFAULT_RELEASE,
    worldpop_country: str = "MEX",
) -> dict[str, Any]:
    """Smoke-test every upstream source the bootstrap runbook depends on."""
    azure_results = [_check_path(label, path) for label, path in AZURE_LAKE_MANIFEST]
    overture_result = _check_overture_release(release)
    worldpop_result = _check_worldpop_rest(country=worldpop_country)
    all_results = [*azure_results, overture_result, worldpop_result]
    return {
        "release": release,
        "worldpop_country": worldpop_country,
        "all_ok": all(r["ok"] for r in all_results),
        "checks": all_results,
    }
