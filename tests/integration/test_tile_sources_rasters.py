"""Integration tests for raster layer emission on ``GET /tile-sources``
and the in-process titiler mount.

The R4 backend commit wires ``curated_geo.raster_layer`` rows into the
tile-source resolver so ingested rasters appear alongside vector live-MVT
layers and PMTiles snapshots. These tests verify:

- A published raster with its silver COG on disk is emitted as
  ``source_mode='raster_cog'`` with ``available=true`` and a titiler
  tile-URL template that includes the registered colormap + rescale.
- A published raster whose silver COG is missing on disk is emitted
  with ``available=false`` — the frontend gets a clear "registered but
  artifact absent" signal instead of a broken tile URL.
- The in-process titiler mount actually serves a real COG via
  ``GET /cog/info``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from reimagina.pipeline.raster import ingest_raster_source


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import reimagina.pipeline.bronze as bronze_mod
    import reimagina.pipeline.raster as raster_mod
    import reimagina.pipeline.silver as silver_mod

    bronze_dir = tmp_path / "bronze"
    silver_dir = tmp_path / "silver"
    bronze_dir.mkdir(parents=True, exist_ok=True)
    silver_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bronze_mod, "BRONZE_DIR", bronze_dir)
    monkeypatch.setattr(silver_mod, "SILVER_DIR", silver_dir)
    monkeypatch.setattr(raster_mod, "SILVER_DIR", silver_dir)
    monkeypatch.setenv("REIMAGINA_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(conn):
    app = create_app(initialise_pool=False)

    def _override_conn():
        yield conn

    app.dependency_overrides[get_db_conn] = _override_conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _write_utm_raster(path: Path) -> Path:
    data = np.arange(100, dtype="float32").reshape(10, 10)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="float32",
        crs="EPSG:32614",
        transform=from_origin(500_000.0, 2_151_000.0, 30.0, 30.0),
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)
    return path


def test_tile_sources_emits_aggregation_overlay_nested_under_parent_raster(
    client,
    conn,
    isolated_data_dir,
    tmp_path,
):
    """Every enabled aggregation recipe must appear on /tile-sources with
    ``source_mode='aggregation_overlay'`` and its tile-URL keyed on the
    Martin function ``aggregation_overlay_tile``. Raster-input recipes
    carry ``parent_raster_layer_slug`` so the frontend can nest the
    overlay under its parent raster; observation-input recipes leave
    that field null.
    """
    from reimagina.storage import backend_for_connection

    raw = _write_utm_raster(tmp_path / "agg.tif")
    slug = f"r4_agg_{uuid4().hex[:8]}"
    ingest = ingest_raster_source(
        conn,
        raw_path=raw,
        source_name=f"r4-agg-src-{uuid4()}",
        provider="pytest",
        version="v1",
        layer_slug=slug,
        aggregation_kind="extensive",
    )
    conn.commit()

    backend = backend_for_connection(conn)
    layer = backend.raster_layer_repo().get_by_source(ingest.source_id)
    prefix = f"r4_overlay_{uuid4().hex[:8]}"
    recipe_id = backend.aggregation_recipe_repo().register_recipe(
        input_kind="raster",
        target_feature_type_slug="block",
        attribute_key_prefix=prefix,
        method="sum",
        input_raster_layer_id=layer["id"],
    )
    conn.commit()

    try:
        response = client.get("/tile-sources")
        assert response.status_code == 200
        overlays = [
            r for r in response.json() if r["source_mode"] == "aggregation_overlay"
        ]
        entry = next(
            r
            for r in overlays
            if r["source_config"]["attribute_key"] == f"{prefix}_sum"
        )
        cfg = entry["source_config"]
        # layer_id is namespaced with the recipe UUID so frontends don't
        # collide with raster layer slugs.
        assert entry["layer_id"] == f"aggregation_overlay::{recipe_id}"
        # Nesting key — the frontend groups by this to render children
        # under their parent raster.
        assert cfg["parent_raster_layer_slug"] == slug
        assert cfg["target_feature_type_slug"] == "block"
        assert cfg["method"] == "sum"
        assert cfg["source_layer"] == "aggregation_overlay"
        # Tile URL must point at Martin's aggregation_overlay_tile
        # function with the two required query params.
        tmpl = cfg["tiles_url_template"]
        assert "/aggregation_overlay_tile/{z}/{x}/{y}" in tmpl
        assert f"attribute_key={prefix}_sum" in tmpl
        assert "feature_type_slug=block" in tmpl
        # Recipe just registered, never run → no observations → unavailable.
        assert entry["available"] is False
        assert cfg["value_range"] is None
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM curated_geo.aggregation_recipe WHERE id = %s",
                (recipe_id,),
            )
            cur.execute(
                "DELETE FROM curated_geo.raster_layer WHERE source_id = %s",
                (ingest.source_id,),
            )
            cur.execute(
                "DELETE FROM curated_geo.file_asset WHERE source_id = %s",
                (ingest.source_id,),
            )
            cur.execute(
                "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s",
                (ingest.source_id,),
            )
            cur.execute(
                "DELETE FROM curated_geo.data_source_catalog WHERE id = %s",
                (ingest.source_id,),
            )
        conn.commit()


def test_tile_sources_emits_raster_cog_entry_for_published_layer(
    client,
    conn,
    isolated_data_dir,
    tmp_path,
):
    raw = _write_utm_raster(tmp_path / "pub.tif")
    slug = f"r4_pub_{uuid4().hex[:8]}"

    ingest_raster_source(
        conn,
        raw_path=raw,
        source_name=f"r4-tile-source-{uuid4()}",
        provider="pytest",
        version="v1",
        layer_slug=slug,
        aggregation_kind="intensive",
        colormap_name="viridis",
        rescale="0,100",
    )

    response = client.get("/tile-sources")
    assert response.status_code == 200
    rasters = [r for r in response.json() if r["source_mode"] == "raster_cog"]
    entry = next(r for r in rasters if r["layer_id"] == slug)
    assert entry["available"] is True
    cfg = entry["source_config"]
    assert cfg["kind"] == "raster"
    # URL template must carry the registry's colormap + rescale so the
    # frontend doesn't have to re-compute them per request. ',' in rescale
    # is percent-encoded as %2C so any future colormap_name containing
    # special chars (e.g. '#' or '&') can't silently break the query string.
    tmpl = cfg["tiles_url_template"]
    assert tmpl is not None
    assert "colormap_name=viridis" in tmpl
    assert "rescale=0%2C100" in tmpl
    assert "/tiles/WebMercatorQuad/{z}/{x}/{y}.png" in tmpl
    assert cfg["cog_url"].startswith("file://")


def test_tile_sources_escapes_special_chars_in_registry_params(
    client,
    conn,
    isolated_data_dir,
    tmp_path,
):
    """colormap_name / rescale with '&' or '#' must not corrupt the query string."""
    raw = _write_utm_raster(tmp_path / "special.tif")
    slug = f"r4_special_{uuid4().hex[:8]}"

    ingest_raster_source(
        conn,
        raw_path=raw,
        source_name=f"r4-special-{uuid4()}",
        provider="pytest",
        version="v1",
        layer_slug=slug,
        aggregation_kind="intensive",
        # Not a real colormap, but a realistic injection-style value.
        colormap_name="viridis&extra=pwn",
        rescale="0,100",
    )

    response = client.get("/tile-sources")
    entry = next(
        r
        for r in response.json()
        if r.get("source_mode") == "raster_cog" and r["layer_id"] == slug
    )
    tmpl = entry["source_config"]["tiles_url_template"]
    # Raw '&' from the registry value must be percent-encoded so the
    # downstream titiler only sees one colormap_name param.
    assert "colormap_name=viridis%26extra%3Dpwn" in tmpl
    assert tmpl.count("colormap_name=") == 1


def test_tile_sources_marks_raster_unavailable_when_silver_missing(
    client,
    conn,
    isolated_data_dir,
    tmp_path,
):
    raw = _write_utm_raster(tmp_path / "will_vanish.tif")
    slug = f"r4_gone_{uuid4().hex[:8]}"

    result = ingest_raster_source(
        conn,
        raw_path=raw,
        source_name=f"r4-missing-{uuid4()}",
        provider="pytest",
        version="v1",
        layer_slug=slug,
        aggregation_kind="intensive",
    )
    # Operator-side artifact deletion (accidental or intentional) must
    # not break the API response — only flip the availability flag.
    Path(result.silver_path).unlink()

    response = client.get("/tile-sources")
    entry = next(
        r
        for r in response.json()
        if r["source_mode"] == "raster_cog" and r["layer_id"] == slug
    )
    assert entry["available"] is False


def test_cog_router_serves_info_for_real_raster(
    client,
    isolated_data_dir,
    tmp_path,
):
    """Smoke-check the mounted titiler router actually opens a COG.

    Uses ``/cog/info`` (cheap metadata endpoint) rather than a tile
    request so the test stays fast and doesn't pull GDAL warp code.
    """
    cog_path = _write_utm_raster(tmp_path / "info.tif")
    response = client.get(f"/cog/info?url={cog_path.as_uri()}")
    assert response.status_code == 200
    info = response.json()
    assert info["count"] == 1
    # titiler surfaces CRS via the `crs` (or `coordinate_system`) field
    # depending on the version; check whichever the payload carries.
    crs_field = info.get("crs") or info.get("coordinate_system") or ""
    assert "32614" in str(crs_field) or "UTM" in str(crs_field)


def test_cog_router_rejects_url_outside_data_root(client, isolated_data_dir):
    """LFI guard: arbitrary host paths (``/etc/passwd``) must be blocked.

    The default titiler ``DatasetPathParams`` would hand the URL
    straight to rasterio and attempt to open the file — our
    ``validated_cog_path`` path-dependency replaces that default and
    rejects anything resolving outside ``host_data_root()`` with 403.
    """
    response = client.get("/cog/info?url=file:///etc/passwd")
    assert response.status_code == 403
    assert "data root" in response.json()["detail"].lower()


def test_cog_router_rejects_non_file_scheme(client, isolated_data_dir):
    """Remote URL fetching is off by default — prevents server-side
    request forgery via the tile endpoint."""
    response = client.get("/cog/info?url=https://example.com/evil.tif")
    assert response.status_code == 400
    assert "file://" in response.json()["detail"].lower()


def test_cog_router_rejects_path_traversal(client, isolated_data_dir, tmp_path):
    """``..`` segments and URL-encoded ``%2e%2e`` must not escape the
    data root after ``Path.resolve()``."""
    # tmp_path is the data root (set by isolated_data_dir); try to
    # escape to its parent.
    escape = (tmp_path / ".." / ".." / "etc" / "passwd").as_uri()
    response = client.get(f"/cog/info?url={escape}")
    # Either 403 (resolved outside root) or 404 (resolved inside root
    # but file absent) is an acceptable rejection — what we care about
    # is that the request did not succeed + surface sensitive content.
    assert response.status_code in (403, 404)


def test_cog_router_rejects_missing_file_inside_root(
    client,
    isolated_data_dir,
    tmp_path,
):
    """A well-formed URL whose target isn't on disk returns 404 rather
    than a confusing GDAL-level error."""
    phantom = (tmp_path / "not_there.tif").as_uri()
    response = client.get(f"/cog/info?url={phantom}")
    assert response.status_code == 404
