"""PMTiles snapshot export and frontend-facing layer source resolution."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from reimagina.db import connect
from reimagina.storage import backend_for_connection, backend_name_for_dsn

from .manifest import TileLayerDefinition, get_tile_layer, load_tile_layers


def default_pmtiles_output_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "tiles" / "pmtiles"


def _postgres_ogr_connection_string(dsn: str) -> str:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("PMTiles export requires a PostgreSQL/PostGIS DSN.")
    if not parsed.hostname or not parsed.path:
        raise ValueError(f"Unsupported PostgreSQL DSN for ogr2ogr: {dsn!r}")
    parts = [
        f"host='{parsed.hostname}'",
        f"dbname='{parsed.path.lstrip('/')}'",
    ]
    if parsed.port:
        parts.append(f"port='{parsed.port}'")
    if parsed.username:
        parts.append(f"user='{parsed.username}'")
    if parsed.password:
        parts.append(f"password='{parsed.password}'")
    return "PG:" + " ".join(parts)


def _layer_query(layer: TileLayerDefinition) -> str:
    if not layer.source_relation:
        raise ValueError(f"Tile layer {layer.layer_id!r} does not define a source relation.")
    return (
        "SELECT "
        "CAST(id AS text) AS feature_id, "
        "type_slug, "
        "role, "
        "name, "
        "CAST(attributes AS text) AS attributes, "
        "geometry "
        f"FROM {layer.source_relation}"
    )


class _BenignGdalQuirk:
    """Substrings in ogr2ogr stderr that indicate a benign, post-write failure.

    The PMTiles driver sometimes invokes GDAL's SQLite dialect for an optional
    post-write step; macOS GDAL builds frequently ship without that dialect
    compiled in, so the subprocess exits non-zero even though the artifact is
    fully written. We don't want that to fail the whole export when we can see
    the artifact on disk and the only complaint is this known-benign message.
    """

    SQLITE_DIALECT_MISSING = "OGR2SQLITE_Setup"


def _run_export_command(command: list[str]) -> None:
    """Run ogr2ogr and tolerate known-benign post-write quirks.

    If ogr2ogr exits non-zero but the artifact file exists with a non-trivial
    size AND the stderr matches a known-benign pattern, treat it as success
    and move on. Any other non-zero exit re-raises the CalledProcessError.
    """
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return
    except subprocess.CalledProcessError as exc:
        # Command layout: [..., "-f", "PMTiles", <artifact_path>, ...]. Pull the
        # artifact path out by index to stay driver-agnostic on argument order.
        artifact_path: Path | None = None
        if "-f" in command:
            fmt_idx = command.index("-f")
            if fmt_idx + 2 < len(command):
                artifact_path = Path(command[fmt_idx + 2])
        stderr = exc.stderr or ""
        benign = _BenignGdalQuirk.SQLITE_DIALECT_MISSING in stderr
        has_artifact = artifact_path is not None and artifact_path.exists() and artifact_path.stat().st_size > 0
        if benign and has_artifact:
            return
        raise


def _latest_lineage_marker(conn) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              MAX(completed_at)::text AS latest_successful_completed_at,
              (
                SELECT id::text
                FROM curated_geo.ingestion_run
                WHERE status = 'succeeded'
                ORDER BY completed_at DESC NULLS LAST, started_at DESC
                LIMIT 1
              ) AS latest_successful_ingestion_run_id
            FROM curated_geo.ingestion_run
            WHERE status = 'succeeded'
            """
        )
        row = cur.fetchone()
    return {
        "latest_successful_completed_at": row[0] if row else None,
        "latest_successful_ingestion_run_id": row[1] if row else None,
    }


def export_pmtiles_snapshot(
    *,
    dsn: str,
    layer_id: str,
    output_dir: str | Path | None = None,
    snapshot_base_url: str | None = None,
    manifest_path: str | Path | None = None,
    snapshot_version: str | None = None,
) -> dict[str, object]:
    if backend_name_for_dsn(dsn) != "postgres":
        raise ValueError("PMTiles export is Postgres/PostGIS-only in v1.")

    layer = get_tile_layer(layer_id, manifest_path)
    if layer.source_mode != "pmtiles_snapshot":
        raise ValueError(f"Layer {layer_id!r} is not configured for PMTiles snapshots.")
    if not layer.implemented:
        raise ValueError(f"Layer {layer_id!r} is reserved and cannot be exported yet.")

    target_dir = Path(output_dir) if output_dir else default_pmtiles_output_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    version = snapshot_version or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = target_dir / f"{layer.layer_id}-{version}.pmtiles"

    command = [
        "ogr2ogr",
        "-f",
        "PMTiles",
        str(output_path),
        _postgres_ogr_connection_string(dsn),
        "-sql",
        _layer_query(layer),
        "-nln",
        layer.tile_layer_name or layer.layer_id,
        "-dsco",
        f"NAME={layer.logical_name}",
        "-dsco",
        f"DESCRIPTION={layer.description}",
        "-dsco",
        f"TYPE={layer.tileset_type}",
        "-dsco",
        f"MINZOOM={layer.minzoom or 0}",
        "-dsco",
        f"MAXZOOM={layer.maxzoom or 8}",
        "-lco",
        f"NAME={layer.tile_layer_name or layer.layer_id}",
    ]
    if layer.minzoom is not None:
        command.extend(["-lco", f"MINZOOM={layer.minzoom}"])
    if layer.maxzoom is not None:
        command.extend(["-lco", f"MAXZOOM={layer.maxzoom}"])

    _run_export_command(command)

    artifact_uri = (
        f"{snapshot_base_url.rstrip('/')}/{output_path.name}"
        if snapshot_base_url
        else output_path.resolve().as_uri()
    )

    with connect(dsn) as conn:
        lineage = {
            "source_relation": layer.source_relation,
            **_latest_lineage_marker(conn),
        }
        snapshot_id = backend_for_connection(conn).tile_repo().register_snapshot(
            layer_id=layer.layer_id,
            artifact_path=str(output_path.resolve()),
            artifact_uri=artifact_uri,
            snapshot_version=version,
            source_relation=layer.source_relation or "",
            source_lineage_ref=lineage,
            status="ready",
        )

    return {
        "layer_id": layer.layer_id,
        "snapshot_id": snapshot_id,
        "snapshot_version": version,
        "artifact_path": str(output_path.resolve()),
        "artifact_uri": artifact_uri,
        "source_mode": layer.source_mode,
        "source_relation": layer.source_relation,
        "command": command,
    }


def list_tile_snapshot_registry(conn, *, layer_id: str | None = None) -> list[dict]:
    return backend_for_connection(conn).tile_repo().list_snapshots(layer_id=layer_id)


def _with_querystring(base_url: str, params: dict[str, object]) -> str:
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params, doseq=True)}"


def _martin_base_and_default_function(live_tilejson_base_url: str) -> tuple[str, str]:
    """Split a legacy `{base}/{function}` URL into its components.

    Older callers pass live_tilejson_base_url = `http://localhost:3000/get_tile`;
    newer ones will pass `http://localhost:3000`. To avoid a breaking change we
    tolerate both: the final URL segment is treated as the default Martin
    function name, falling back to `get_tile` if the URL is already a bare
    host.
    """
    trimmed = live_tilejson_base_url.rstrip("/")
    try:
        base, tail = trimmed.rsplit("/", 1)
    except ValueError:
        return trimmed, "get_tile"
    if "://" not in tail and tail:
        return base, tail
    return trimmed, "get_tile"


def resolve_tile_sources(
    conn,
    *,
    live_tilejson_base_url: str = "http://localhost:3000/get_tile",
    snapshot_base_url: str | None = None,
    manifest_path: str | Path | None = None,
) -> list[dict[str, object]]:
    """Resolve every manifest layer into a frontend-ready tile source.

    Each `live_mvt` layer is turned into `{martin_base}/{function_name}/{z}/{x}/{y}`,
    where `function_name` is the layer's `tile_layer_name`, the last path
    segment of `source_relation`, or `get_tile` as a final fallback.

    `query_params` from the manifest are forwarded under
    `source_config.query_params` as schema hints (e.g. `{"indicator_slug":
    "REQUIRED"}`) rather than being baked into the URL — the frontend picks
    real values at render time and assembles the final URL itself.
    """
    martin_base, default_function = _martin_base_and_default_function(live_tilejson_base_url)
    repo = backend_for_connection(conn).tile_repo()
    resolved: list[dict[str, object]] = []
    for layer in load_tile_layers(manifest_path):
        if layer.source_mode == "live_mvt":
            function_name = (
                layer.tile_layer_name
                or (layer.source_relation.split(".")[-1] if layer.source_relation else default_function)
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
            if snapshot_base_url:
                artifact_uri = f"{snapshot_base_url.rstrip('/')}/{Path(latest['artifact_path']).name}"
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
    return resolved
