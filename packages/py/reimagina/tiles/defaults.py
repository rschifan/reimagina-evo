"""Single source of truth for tile-service URL defaults + path translators.

The API layer, the CLI, and tile-related helpers all need the same
localhost fallbacks and the same host-vs-container path mapping.
Keeping them in one module means a host change at deploy time edits
one line, not four.

Resolved at call time so env-var changes (or test monkeypatches) take
effect without reloading the module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from reimagina.paths import REPO_ROOT


def tile_live_base_url() -> str:
    """Base URL Martin exposes the live tile function at."""
    return os.environ.get(
        "REIMAGINA_TILE_LIVE_BASE_URL",
        "http://localhost:3000/get_tile",
    )


def tile_snapshot_base_url() -> str:
    """Static host serving PMTiles snapshot artifacts."""
    return os.environ.get(
        "REIMAGINA_TILE_SNAPSHOT_BASE_URL",
        "http://localhost:3001",
    )


def tile_raster_base_url() -> str:
    """Base URL the raster tile server (titiler) serves COG tiles at.

    Resolution order:
      1. ``REIMAGINA_TILE_RASTER_BASE_URL`` — explicit override; use
         when titiler runs out-of-process or the API sits behind a
         reverse proxy that rewrites the ``/cog`` prefix.
      2. ``REIMAGINA_API_BASE_URL`` + ``/cog`` — derive from the same
         env var any other API-relative tool uses. Keeps the two in
         sync when a deployment runs on a non-default port / host.
      3. ``http://localhost:8000/cog`` — dev default matching the
         uvicorn default bind. **Misconfig trap**: a deployment that
         binds uvicorn to a non-8000 port and doesn't set either env
         var will silently emit broken raster tile URLs. The fix is
         setting one of the two env vars above at deploy time.
    """
    explicit = os.environ.get("REIMAGINA_TILE_RASTER_BASE_URL")
    if explicit:
        return explicit
    api_base = os.environ.get("REIMAGINA_API_BASE_URL")
    if api_base:
        return f"{api_base.rstrip('/')}/cog"
    return "http://localhost:8000/cog"


def host_data_root() -> Path:
    """Absolute filesystem path of the data root on the host.

    Matches the convention used by ``pipeline/bronze.py`` +
    ``pipeline/silver.py``: read ``REIMAGINA_DATA_DIR`` if set,
    otherwise fall back to ``<repo-root>/data``. Return value is an
    absolute, resolved ``Path`` so callers can use it without worrying
    about the cwd the process started in.
    """
    return Path(os.environ.get("REIMAGINA_DATA_DIR", str(REPO_ROOT / "data"))).resolve()


# ── Internal URI parser ──────────────────────────────────────────────
#
# reimagina stores file paths as ``file://data/<subpath>``. This is a
# **reimagina-internal opaque form, not an RFC-8089 file URI**:
#   * the authority must be the literal token ``data`` (NOT a hostname);
#   * ``<subpath>`` is relative to the configured data root, so the
#     whole URI survives across hosts with different ``REIMAGINA_DATA_DIR``
#     values;
#   * `..` and empty segments are rejected — no directory escape;
#   * RFC-8089 ``file:///data/...`` (empty authority) is explicitly
#     rejected — this parser is for the internal form only. Callers
#     who want a filesystem URI use ``to_host_path(uri).as_uri()``.

_INTERNAL_URI_RE = re.compile(r"^file://data(?:/(.*))?$")


def parse_internal_uri(uri: str) -> PurePosixPath:
    """Parse a reimagina-internal ``file://data/<subpath>`` URI.

    Returns the ``<subpath>`` portion as a data-root-relative
    ``PurePosixPath`` (e.g. ``silver/<source_id>/<name>.cog.tif``).
    Raises ``ValueError`` on any deviation from the grammar so
    malformed inputs fail loudly at the boundary rather than silently
    reaching the filesystem.
    """
    m = _INTERNAL_URI_RE.match(uri)
    if not m:
        raise ValueError(
            f"Not a reimagina-internal URI: {uri!r}. Expected form "
            f"'file://data/<subpath>' (RFC-8089 'file:///...' is NOT accepted)."
        )
    raw_path = m.group(1) or ""
    if not raw_path:
        raise ValueError(
            f"URI {uri!r} has no subpath — data root itself is not addressable."
        )
    segments = raw_path.split("/")
    for seg in segments:
        if seg == "":
            raise ValueError(f"URI {uri!r} contains an empty path segment (//).")
        if seg in (".", ".."):
            raise ValueError(f"URI {uri!r} contains a traversal segment ({seg!r}).")
    return PurePosixPath(*segments)


def to_host_path(uri: str) -> Path:
    """Resolve an internal URI to an absolute host filesystem path.

    Composes ``host_data_root()`` + the URI's parsed subpath. Used by
    the tile-source resolver to ``Path.exists()``-check COG
    availability before emitting a titiler URL, and by the zonal-stats
    worker to open the raster via rasterio.
    """
    return host_data_root() / Path(*parse_internal_uri(uri).parts)


def to_titiler_url(uri: str) -> str:
    """Resolve an internal URI to a ``file://`` URL titiler can open.

    Titiler's ``/cog`` endpoint takes a fully-qualified URL in its
    ``url=`` parameter. With the in-process mount (see
    ``apps/api/app.py``), titiler sees the same filesystem the API
    runs on, so the URL is simply the absolute host path as a
    ``file://`` URI. If titiler is split out into a Docker sidecar
    later, replace this with a container-path variant.
    """
    return to_host_path(uri).as_uri()
