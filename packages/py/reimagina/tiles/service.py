"""PMTiles snapshot export and frontend-facing layer source resolution."""
from __future__ import annotations

import json
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


def _run_export_command(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


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


def resolve_tile_sources(
    conn,
    *,
    live_tilejson_base_url: str = "http://localhost:3000/get_tile",
    snapshot_base_url: str | None = None,
    manifest_path: str | Path | None = None,
) -> list[dict[str, object]]:
    repo = backend_for_connection(conn).tile_repo()
    resolved: list[dict[str, object]] = []
    for layer in load_tile_layers(manifest_path):
        if layer.source_mode == "live_mvt":
            tilejson_url = _with_querystring(live_tilejson_base_url, layer.query_params)
            tiles_url_template = _with_querystring(
                f"{live_tilejson_base_url.rstrip('/')}/{{z}}/{{x}}/{{y}}",
                layer.query_params,
            )
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
