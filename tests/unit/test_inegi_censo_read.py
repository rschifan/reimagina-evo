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
    _normalise_name,
    _normalise_partition_value,
    build_cvegeo,
    read_layer_to_dataframe,
)


def _write_partition(parquet_dir: Path, *, entidad: str, rows: list[dict]) -> None:
    """Mirror the data lake's lowercase, unpadded ``entidad=N/`` partition.

    2020 (unlike 2010) does not nest ``mun=N/`` underneath — the muni
    information lives inside each row as ``nom_mun``.
    """
    part_dir = parquet_dir / f"entidad={entidad}"
    part_dir.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "nom_mun": pa.array([r["nom_mun"] for r in rows], type=pa.string()),
            "loc": pa.array([r["loc"] for r in rows], type=pa.int64()),
            "ageb": pa.array([r["ageb"] for r in rows], type=pa.string()),
            "mza": pa.array([r["mza"] for r in rows], type=pa.int64()),
            "pobtot": pa.array([r.get("pobtot") for r in rows], type=pa.int64()),
            "pobmas": pa.array([r.get("pobmas") for r in rows], type=pa.float64()),
            "pobfem": pa.array([r.get("pobfem") for r in rows], type=pa.float64()),
            "p_15ymas": pa.array([r.get("p_15ymas") for r in rows], type=pa.float64()),
            "p_18ymas": pa.array([r.get("p_18ymas") for r in rows], type=pa.float64()),
            "vivtot": pa.array([r.get("vivtot") for r in rows], type=pa.int64()),
        }
    )
    pq.write_table(table, part_dir / "part-0.parquet")


@pytest.fixture
def fake_censo_root(tmp_path: Path) -> Path:
    """Two states (9, 2), three munis. Partitions are entidad-only —
    matches the real 2020 data lake layout."""
    root = tmp_path / "manzanas_2020.parquet"
    _write_partition(
        root,
        entidad="9",
        rows=[
            {
                "nom_mun": "Cuauhtémoc",
                "loc": 1,
                "ageb": "0019",
                "mza": 1,
                "pobtot": 163,
                "pobmas": 73.0,
                "pobfem": 90.0,
                "p_15ymas": 130.0,
                "p_18ymas": 120.0,
                "vivtot": 66,
            },
            {
                "nom_mun": "Cuauhtémoc",
                "loc": 1,
                "ageb": "0019",
                "mza": 2,
                "pobtot": 308,
                "pobmas": 150.0,
                "pobfem": 158.0,
                "p_15ymas": 240.0,
                "p_18ymas": 220.0,
                "vivtot": 84,
            },
            {
                "nom_mun": "Tlalpan",
                "loc": 1,
                "ageb": "0023",
                "mza": 5,
                "pobtot": 42,
                "pobmas": 20.0,
                "pobfem": 22.0,
                "p_15ymas": 30.0,
                "p_18ymas": 28.0,
                "vivtot": 12,
            },
        ],
    )
    _write_partition(
        root,
        entidad="2",
        rows=[
            {
                "nom_mun": "Mexicali",
                "loc": 1,
                "ageb": "0500",
                "mza": 10,
                "pobtot": 90,
                "pobmas": 45.0,
                "pobfem": 45.0,
                "p_15ymas": 70.0,
                "p_18ymas": 65.0,
                "vivtot": 30,
            },
        ],
    )
    return root


def test_normalise_partition_value_strips_leading_zeros():
    """Census partitions are unpadded ('9', '2'); callers may pass
    padded ('09', '02') from the marco pipeline. Both must resolve to
    the unpadded form so the filter matches the on-disk directory."""
    assert _normalise_partition_value("09") == "9"
    assert _normalise_partition_value("9") == "9"
    assert _normalise_partition_value(9) == "9"
    # Edge: don't collapse '0' to empty string.
    assert _normalise_partition_value("0") == "0"


def test_normalise_name_strips_diacritics_and_case():
    """nom_mun in 2020 carries Spanish diacritics ('Cuauhtémoc'); the
    marco-derived lookup key must match in normalised form so the join
    survives accidentally accent-stripped or upper-cased copies."""
    assert _normalise_name("Cuauhtémoc") == "cuauhtemoc"
    assert _normalise_name("CUAUHTEMOC") == "cuauhtemoc"
    assert _normalise_name(" Cuauhtemoc  ") == "cuauhtemoc"


def test_read_filters_to_pilot_states(fake_censo_root: Path):
    df = read_layer_to_dataframe(fake_censo_root, cve_ent_filter=["09"])
    assert len(df) == 3
    assert set(df["entidad"].unique()) == {"9"}
    # nom_mun must be readable so build_cvegeo can resolve CVE_MUN.
    assert set(df["nom_mun"].unique()) == {"Cuauhtémoc", "Tlalpan"}


def test_read_returns_empty_when_filter_misses(fake_censo_root: Path):
    df = read_layer_to_dataframe(fake_censo_root, cve_ent_filter=["99"])
    assert df.empty


def test_build_cvegeo_zero_pads_each_component():
    """CVEGEO is the 16-char composite that joins to
    ``feature.source_record_ref``. The 2020 builder takes the
    (entidad, normalised nom_mun) → CVE_MUN lookup that the marco
    municipality features supply."""
    df = pd.DataFrame(
        {
            "entidad": ["9", "2", "9"],
            "nom_mun": ["Cuauhtémoc", "Mexicali", "Tlalpan"],
            "loc": [1, 1, 1],
            "ageb": ["0019", "0500", "0023"],
            "mza": [1, 10, 5],
        }
    )
    muni_lookup = {
        ("9", "cuauhtemoc"): "015",
        ("2", "mexicali"): "002",
        ("9", "tlalpan"): "012",
    }
    cvegeo = build_cvegeo(df, muni_lookup)
    assert cvegeo.tolist() == [
        "0901500010019001",  # CDMX, Cuauhtémoc, manzana 1
        "0200200010500010",  # Mexicali, manzana 10
        "0901200010023005",  # CDMX, Tlalpan, manzana 5
    ]
    assert (cvegeo.str.len() == 16).all()


def test_build_cvegeo_blanks_out_when_muni_lookup_misses():
    """Rows whose nom_mun isn't in the lookup must NOT silently produce
    '000' for CVE_MUN — that would be a real-looking but wrong CVEGEO
    that could collide with another muni's feature. Build_cvegeo
    returns '' instead so the orchestrator filters them by length."""
    df = pd.DataFrame(
        {
            "entidad": ["9"],
            "nom_mun": ["Atlantis"],
            "loc": [1],
            "ageb": ["0001"],
            "mza": [1],
        }
    )
    cvegeo = build_cvegeo(df, muni_lookup={})
    assert cvegeo.tolist() == [""]


def test_headline_columns_default_set_includes_population_and_housing():
    """Headline subset must cover both population and housing variables
    so the prototype's first wave of indicators has data without a
    follow-up ingest."""
    assert "pobtot" in HEADLINE_COLUMNS
    assert "pobfem" in HEADLINE_COLUMNS
    assert "vivtot" in HEADLINE_COLUMNS
    # vivpar_hab is suppressed at manzana grain by INEGI (always zero
    # in both 2010 and 2020), so we deliberately *don't* ingest it.
    # Pin that fact here so a future change has to acknowledge the
    # source quirk before re-adding it.
    assert "vivpar_hab" not in HEADLINE_COLUMNS


def test_attribute_key_prefix_matches_dataset_vintage():
    """attribute_key = '<prefix>_<col>'. Pinning the prefix here means
    a future renaming has to update tests, docs, and any downstream
    indicator definition together — they all read the same constant."""
    assert ATTRIBUTE_KEY_PREFIX == "censo_2020"
