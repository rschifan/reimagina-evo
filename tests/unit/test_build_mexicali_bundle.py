"""The Mexicali source bundle builder: subset, layout, checksums.

Built from tiny fixtures shaped like the INEGI parquet copies, so what is
checked is the builder's own logic: only Baja California survives, only the
columns the loaders read survive, the loaders find the files where the
bundle puts them, the same inputs give the same bytes, and ``verify`` catches
a changed, missing or unlisted file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from shapely.geometry import LineString, Point, box

from reimagina.pipeline import inegi_censo, inegi_marco, points
from reimagina.pipeline.osm_network import save_graph

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_mexicali_bundle as bb  # noqa: E402


def _marco(root: Path) -> Path:
    src = root / "marco"
    wkb = shapely.to_wkb(box(-115.5, 32.6, -115.4, 32.7))
    for ent in ("02", "09"):
        state = src / "areas_estatales.parquet" / f"CVE_ENT={ent}"
        state.mkdir(parents=True)
        pq.write_table(
            pa.table({"CVEGEO": [ent], "NOMGEO": ["E"], "geometry": [wkb]}),
            state / "a.parquet",
        )
        for layer, extra in (
            ("areas_municipales", {"NOMGEO": ["Mexicali"]}),
            ("agebs_urbanas", {"CVE_LOC": ["0001"], "CVE_AGEB": ["0010"]}),
            (
                "manzanas_urbanas",
                {
                    "CVE_LOC": ["0001"],
                    "CVE_AGEB": ["0010"],
                    "CVE_MZA": ["001"],
                    "AMBITO": ["Urbana"],
                    "TIPOMZA": ["Típica"],
                    "UNUSED": ["dropped"],
                },
            ),
        ):
            d = src / f"{layer}.parquet" / f"CVE_ENT={ent}" / "CVE_MUN=002"
            d.mkdir(parents=True, exist_ok=True)
            pq.write_table(
                pa.table({"CVEGEO": [f"{ent}002"], **extra, "geometry": [wkb]}),
                d / "a.parquet",
            )
    return src


def _censo(root: Path) -> Path:
    src = root / "censo"
    for ent in ("2", "9"):
        d = src / f"entidad={ent}" / "mun=2"
        d.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "nom_ent": ["X"],
                    "nom_mun": ["Mexicali"],
                    "loc": [1],
                    "ageb": ["0010"],
                    "mza": [1],
                    **{c: [1.0] for c in inegi_censo.HEADLINE_COLUMNS},
                    "prom_ocup": [3.1],
                    "p_0a2": [9.0],
                }
            ),
            d / "a.parquet",
        )
    return src


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, str]:
    denue = tmp_path / "equipamientos.geoparquet"
    gpd.GeoDataFrame(
        {"id": [1], "category": ["Cine"]}, geometry=[Point(-115.4, 32.6)], crs=4326
    ).to_parquet(denue)
    buildings = tmp_path / "buildings.parquet"
    places = tmp_path / "places.parquet"
    pq.write_table(pa.table({"id": ["b1", "b2"]}), buildings)
    pq.write_table(pa.table({"id": ["p1"]}), places)
    net = tmp_path / "net"
    save_graph(
        pd.DataFrame({"x": [0.0, 1.0], "y": [0.0, 1.0]}, index=[1, 2]),
        pd.DataFrame(
            {
                "u": [1],
                "v": [2],
                "osmid": [7],
                "length": [1.5],
                "geometry": [LineString([(0, 0), (1, 1)])],
            }
        ),
        net,
        fetched="2026-11-10",
    )
    return {
        "inegi_marco": str(_marco(tmp_path)),
        "inegi_censo": str(_censo(tmp_path)),
        "denue_amenities": str(denue),
        "overture_buildings": str(buildings),
        "overture_places": str(places),
        "overture_release": "2026-10-21.0",
        "osm_network": str(net),
    }


def _build(inputs: dict[str, str], out: Path) -> Path:
    argv = ["build", "--out", str(out)]
    for key, value in inputs.items():
        argv += [f"--{key.replace('_', '-')}", value]
    assert bb.main(argv) == 0
    return out


def test_bundle_keeps_one_entidad_and_the_loader_columns(inputs, tmp_path):
    out = _build(inputs, tmp_path / "bundle")

    blocks = inegi_marco.read_layer_to_geodataframe(
        out / bb.MARCO_DIR / "manzanas_urbanas.parquet",
        cve_ent_filter=None,
        partition_cols=("CVE_ENT", "CVE_MUN"),
    )
    assert list(blocks["CVE_ENT"]) == ["02"]
    assert list(blocks["CVE_MUN"]) == ["002"]
    assert "UNUSED" not in blocks.columns

    census = inegi_censo.read_layer_to_dataframe(
        out / bb.CENSO_DIR, cve_ent_filter=None, columns=("pobtot", "prom_ocup")
    )
    assert list(census["entidad"]) == ["2"]
    assert pq.read_schema(next((out / bb.CENSO_DIR).rglob("*.parquet"))).names == [
        "nom_mun",
        "loc",
        "ageb",
        "mza",
        *inegi_censo.HEADLINE_COLUMNS,
        "prom_ocup",
    ]

    manifest = json.loads((out / "MANIFEST.json").read_text())
    files = {e["file"]: e for e in manifest["files"]}
    assert files["overture/buildings_2026-10-21.0.parquet"]["rows"] == 2
    assert files["osm/walk_network_2026-11-10/edges.parquet"]["licence"] == "ODbL-1.0"
    assert all(len(e["sha256"]) == 64 for e in manifest["files"])
    attribution = (out / "ATTRIBUTION.md").read_text()
    assert "Fuente: INEGI, Marco Geoestadístico 2024" in attribution
    assert "not reviewed, approved or endorsed" in attribution


def test_the_amenity_loader_finds_the_bundled_denue_layer():
    assert bb.DENUE_FILE == points.MEXICALI_AMENITIES.geoparquet_relative


def test_same_inputs_give_the_same_bytes(inputs, tmp_path):
    a = _build(inputs, tmp_path / "a")
    b = _build(inputs, tmp_path / "b")
    assert (a / bb.SUMS).read_text() == (b / bb.SUMS).read_text()


def test_verify_catches_changed_missing_and_unlisted_files(inputs, tmp_path):
    out = _build(inputs, tmp_path / "bundle")
    assert bb.verify(out) == []

    (out / "overture/places_2026-10-21.0.parquet").write_bytes(b"tampered")
    (out / "ATTRIBUTION.md").unlink()
    (out / "extra.txt").write_text("x")
    problems = bb.verify(out)

    assert "checksum mismatch: overture/places_2026-10-21.0.parquet" in problems
    assert "missing: ATTRIBUTION.md" in problems
    assert "not in SHA256SUMS: extra.txt" in problems
    assert bb.main(["verify", str(out)]) == 1


def test_build_refuses_a_non_empty_directory(inputs, tmp_path):
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "old").write_text("x")
    with pytest.raises(SystemExit):
        _build(inputs, out)
