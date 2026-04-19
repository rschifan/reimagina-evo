"""INEGI Marco Geoestadístico + CDMX open-data fetch helpers.

Downloads and caches the per-state INEGI Marco Geoestadístico release plus
the CDMX government's colonias layer into ``data/sources/cdmx/``. Re-runs are
idempotent: a cached archive is reused if present. When a portal URL moves or
rate-limits, the caller is instructed to download manually and re-run with
``--from-path`` rather than retrying blindly.

This module is deliberately portal-agnostic above the URL constants: any
zipped-shapefile source with a similar structure can be plugged in.
"""
from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCES_DIR = Path(os.environ.get("REIMAGINA_DATA_DIR", str(_REPO_ROOT / "data"))) / "sources"
CDMX_SOURCES_DIR = SOURCES_DIR / "cdmx"

INEGI_CDMX_STATE_CODE = "09"

# INEGI Marco Geoestadístico 2020 CDMX-only archive. This URL pointer is
# documented on https://www.inegi.org.mx/temas/mg/ under "Marco Geoestadístico
# Integrado" — filename convention is `<state_code>_<state_slug>.zip`. INEGI
# periodically republishes; if the URL 404s, download manually from the portal
# and pass --from-path.
INEGI_MARCO_CDMX_URL = (
    "https://www.inegi.org.mx/contenidos/productos/prod_serv/contenidos/"
    "espanol/bvinegi/productos/geografia/marcogeo/889463807469/"
    "09_ciudaddemexico.zip"
)
INEGI_PORTAL_URL = "https://www.inegi.org.mx/temas/mg/"

# CDMX colonias from the government open-data portal. The ArcGIS REST endpoint
# serves a GeoJSON dump of the official "Límite de las colonias" layer.
CDMX_COLONIAS_URL = (
    "https://sig.cdmx.gob.mx/arcgis/rest/services/hosted/"
    "ColoniasCDMX/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson"
)
CDMX_COLONIAS_PORTAL_URL = "https://datos.cdmx.gob.mx/dataset/colonias"

# HTTP timeout + user-agent: INEGI's CDN has been observed to drop anonymous
# `python-urllib/*` requests silently.
_REQUEST_TIMEOUT_SECONDS = 60
_USER_AGENT = "reimagina-cli/0.1 (+github.com/reimagina)"


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a fetch. ``path`` is always present when ``ok`` is True."""

    ok: bool
    path: Path
    bytes_written: int
    cached: bool
    portal_url: str | None = None
    error: str | None = None


class InegiFetchError(RuntimeError):
    """Raised when a cached download cannot be produced and no manual file was supplied."""


def cached_download(
    url: str,
    *,
    dest_dir: Path,
    filename: str,
    portal_url: str | None = None,
    overwrite: bool = False,
) -> DownloadResult:
    """Download `url` to `dest_dir/filename` unless already cached.

    Returns a DownloadResult. If the HTTP request fails or the response is
    non-200, the function raises InegiFetchError with a clear manual-download
    hint rather than silently falling back, so the caller sees one obvious path
    forward: `--from-path <zip>`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    if dest.exists() and not overwrite:
        return DownloadResult(
            ok=True,
            path=dest,
            bytes_written=dest.stat().st_size,
            cached=True,
            portal_url=portal_url,
        )

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise InegiFetchError(
                    _manual_download_hint(url, portal_url, dest, status=response.status)
                )
            with open(dest, "wb") as out:
                shutil.copyfileobj(response, out)
    except urllib.error.URLError as exc:
        # Clean up a partial file so a retry starts fresh.
        if dest.exists():
            dest.unlink()
        raise InegiFetchError(
            _manual_download_hint(url, portal_url, dest, reason=str(exc))
        ) from exc

    return DownloadResult(
        ok=True,
        path=dest,
        bytes_written=dest.stat().st_size,
        cached=False,
        portal_url=portal_url,
    )


def _manual_download_hint(
    url: str,
    portal_url: str | None,
    dest: Path,
    *,
    status: int | None = None,
    reason: str | None = None,
) -> str:
    lines = [
        "INEGI / CDMX download failed automatically.",
        f"  attempted URL: {url}",
    ]
    if status is not None:
        lines.append(f"  HTTP status:   {status}")
    if reason is not None:
        lines.append(f"  reason:        {reason}")
    if portal_url is not None:
        lines.append(f"  portal:        {portal_url}")
    lines.append("")
    lines.append("Download the archive manually from the portal above, then re-run with:")
    lines.append(f"  --from-path {dest}")
    return "\n".join(lines)


def extract_zip(archive_path: Path, extract_dir: Path) -> Path:
    """Extract a zip archive into `extract_dir`. Returns extract_dir. Idempotent."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def find_shapefile(root: Path, basename: str) -> Path:
    """Return the first shapefile whose filename exactly equals `basename`.

    INEGI archives contain many .shp files per state (e.g. `09ent.shp`,
    `09mun.shp`, `09a.shp` for AGEBs, `09m.shp` for manzanas, `09fm.shp`
    for frentes de manzana). Exact-basename matching avoids ambiguity
    (e.g. `*m.shp` would otherwise match both `09m.shp` and `09fm.shp`).
    """
    matches = sorted(p for p in root.rglob(basename) if p.is_file() and p.name == basename)
    if not matches:
        available = sorted({p.name for p in root.rglob("*.shp")})
        raise FileNotFoundError(
            f"No shapefile named {basename!r} found under {root}. "
            f"Available .shp files: {available}"
        )
    return matches[0]
