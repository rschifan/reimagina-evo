"""Unit tests for reimagina.pipeline.inegi_censo CVEGEO + partition reader.

DB-free. The full DB-side path (bulk_upsert_observations, source
registration, run lifecycle) is exercised by
``tests/integration/test_inegi_censo_ingest.py``.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from reimagina.pipeline.inegi_censo import (
    HEADLINE_COLUMNS,
    ATTRIBUTE_KEY_PREFIX,
    _normalise_partition_value,
    build_cvegeo,
    read_layer_to_dataframe,
)


def _write_partition(parquet_dir: Path, *, entidad: str, mun: str,
                     rows: list[dict]) -> None:
    """Mirror the data lake's lowercase, unpadded ``entidad=N/mun=N/``
    nested partition shape."""
    part_dir = parquet_dir / f"entidad={entidad}" / f"mun={mun}"
    part_dir.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "loc": pa.array([r["loc"] for r in rows], type=pa.int64()),
        "ageb": pa.array([r["ageb"] for r in rows], type=pa.string()),
        "mza": pa.array([r["mza"] for r in rows], type=pa.int64()),
        "pobtot": pa.array([r.get("pobtot") for r in rows], type=pa.int64()),
        "pobmas": pa.array([r.get("pobmas") for r in rows], type=pa.float64()),
        "pobfem": pa.array([r.get("pobfem") for r in rows], type=pa.float64()),
        "p_15ymas": pa.array([r.get("p_15ymas") for r in rows], type=pa.float64()),
        "p_18ymas": pa.array([r.get("p_18ymas") for r in rows], type=pa.float64()),
        "vivtot": pa.array([r.get("vivtot") for r in rows], type=pa.int64()),
        "vivpar_hab": pa.array([r.get("vivpar_hab") for r in rows], type=pa.int64()),
    })
    pq.write_table(table, part_dir / "part-0.parquet")


@pytest.fixture
def fake_censo_root(tmp_path: Path) -> Path:
    """Two states (9, 2), three munis, mixed row counts. Entidad and
    mun stored unpadded — exactly as the real data lake exposes them."""
    root = tmp_path / "manzanas_2010.parquet"
    _write_partition(root, entidad="9", mun="15", rows=[
        {"loc": 1, "ageb": "0019", "mza": 1, "pobtot": 163,
         "pobmas": 73.0, "pobfem": 90.0, "p_15ymas": 130.0,
         "p_18ymas": 120.0, "vivtot": 66, "vivpar_hab": 60},
        {"loc": 1, "ageb": "0019", "mza": 2, "pobtot": 308,
         "pobmas": 150.0, "pobfem": 158.0, "p_15ymas": 240.0,
         "p_18ymas": 220.0, "vivtot": 84, "vivpar_hab": 80},
    ])
    _write_partition(root, entidad="9", mun="12", rows=[
        {"loc": 1, "ageb": "0023", "mza": 5, "pobtot": 42,
         "pobmas": 20.0, "pobfem": 22.0, "p_15ymas": 30.0,
         "p_18ymas": 28.0, "vivtot": 12, "vivpar_hab": 10},
    ])
    _write_partition(root, entidad="2", mun="2", rows=[
        {"loc": 1, "ageb": "0500", "mza": 10, "pobtot": 90,
         "pobmas": 45.0, "pobfem": 45.0, "p_15ymas": 70.0,
         "p_18ymas": 65.0, "vivtot": 30, "vivpar_hab": 28},
    ])
    return root


def test_normalise_partition_value_strips_leading_zeros():
    """Census partitions are unpadded ('9', '15'); callers may pass
    padded ('09', '015') if they're carrying values from the marco
    pipeline. Both must resolve to the unpadded form so the filter
    matches the on-disk directory names."""
    assert _normalise_partition_value("09") == "9"
    assert _normalise_partition_value("9") == "9"
    assert _normalise_partition_value(9) == "9"
    assert _normalise_partition_value("015") == "15"
    assert _normalise_partition_value(15) == "15"
    # Edge: don't collapse '0' to empty string.
    assert _normalise_partition_value("0") == "0"


def test_read_filters_to_pilot_states(fake_censo_root: Path):
    df = read_layer_to_dataframe(
        fake_censo_root,
        cve_ent_filter=["09"],   # padded input, must normalise to '9'
        cve_mun_filter=None,
    )
    assert len(df) == 3
    assert set(df["entidad"].unique()) == {"9"}


def test_read_combines_state_and_mun_filters(fake_censo_root: Path):
    """``cve_ent_filter`` and ``cve_mun_filter`` AND together — Cuauhtémoc
    only, not all of CDMX."""
    df = read_layer_to_dataframe(
        fake_censo_root,
        cve_ent_filter=["09"],
        cve_mun_filter=["015"],
    )
    assert len(df) == 2  # two manzanas in the fixture for Cuauhtémoc
    assert set(df["mun"].unique()) == {"15"}


def test_read_returns_empty_when_filter_misses(fake_censo_root: Path):
    df = read_layer_to_dataframe(
        fake_censo_root, cve_ent_filter=["99"], cve_mun_filter=None,
    )
    assert df.empty


def test_build_cvegeo_zero_pads_each_component():
    """CVEGEO is the 16-char composite that joins to feature.source_record_ref.
    Each component zero-pads to its canonical width."""
    df = pd.DataFrame({
        "entidad": ["9", "2", "9"],
        "mun": ["15", "2", "12"],
        "loc": [1, 1, 1],
        "ageb": ["0019", "0500", "0023"],
        "mza": [1, 10, 5],
    })
    cvegeo = build_cvegeo(df)
    assert cvegeo.tolist() == [
        "0901500010019001",  # CDMX, Cuauhtémoc, manzana 1
        "0200200010500010",  # Mexicali, manzana 10
        "0901200010023005",  # CDMX, Tlalpan, manzana 5
    ]
    # Every CVEGEO must be exactly 16 chars — that's the canonical INEGI
    # manzana key length and what the marco_geostadistico ingest stored.
    assert (cvegeo.str.len() == 16).all()


def test_headline_columns_default_set_includes_population_and_housing():
    """Headline subset must cover both population and housing variables
    so the prototype's first wave of indicators (density, occupancy)
    has data without a follow-up ingest."""
    assert "pobtot" in HEADLINE_COLUMNS
    assert "pobfem" in HEADLINE_COLUMNS
    assert "vivtot" in HEADLINE_COLUMNS
    assert "vivpar_hab" in HEADLINE_COLUMNS


def test_attribute_key_prefix_matches_dataset_vintage():
    """attribute_key = '<prefix>_<col>'. Pinning the prefix here means a
    future renaming has to update tests, docs, and any downstream
    indicator definition together — they all read the same constant."""
    assert ATTRIBUTE_KEY_PREFIX == "censo_2010"
