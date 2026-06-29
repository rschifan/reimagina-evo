"""Integration tests for reimagina.pipeline.inegi_censo.bootstrap.

Builds a tiny synthetic 2020 census parquet that mirrors the data
lake's lowercase ``entidad=N/`` partition shape (no nested ``mun``),
seeds matching municipality + block features so the (nom_mun → CVE_MUN)
lookup and the CVEGEO join both hit, runs ``bootstrap()`` against the
migrated test DB, and asserts:

- the data_source_catalog row is registered as a tabular source,
- ``ingestion_run`` reaches ``status='succeeded'`` with useful counters,
- one observation per (feature × headline column) lands in
  ``curated_geo.observation`` with the canonical ``censo_2020_*`` key,
- unmatched CVEGEOs are skipped (counted) rather than raising.

Test isolation: ``bootstrap()`` commits source rows + observations.
Each test patches ``CENSO_VERSION`` to a unique string so it lives in
its own ``data_source_catalog`` row.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from reimagina.data import feature as feat
from reimagina.pipeline import inegi_censo

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)


HEADLINE_COL_COUNT = 6  # pobtot, pobmas, pobfem, p_15ymas, p_18ymas, vivtot


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_source_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """Force a unique CENSO_VERSION per test."""
    version = f"test-{uuid4().hex[:8]}"
    monkeypatch.setattr(inegi_censo, "CENSO_VERSION", version)
    return version


@pytest.fixture
def fake_censo_root(tmp_path: Path) -> Path:
    """Two manzanas in CDMX-Cuauhtémoc, deterministic values."""
    root = tmp_path / "manzanas_2020.parquet"
    part = root / "entidad=9"
    part.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "nom_mun": pa.array(["Cuauhtémoc", "Cuauhtémoc"], type=pa.string()),
            "loc": pa.array([1, 1], type=pa.int64()),
            "ageb": pa.array(["0019", "0019"], type=pa.string()),
            "mza": pa.array([1, 2], type=pa.int64()),
            "pobtot": pa.array([100, 200], type=pa.int64()),
            "pobmas": pa.array([45.0, 95.0], type=pa.float64()),
            "pobfem": pa.array([55.0, 105.0], type=pa.float64()),
            "p_15ymas": pa.array([80.0, 160.0], type=pa.float64()),
            "p_18ymas": pa.array([70.0, 150.0], type=pa.float64()),
            "vivtot": pa.array([40, 80], type=pa.int64()),
        }
    )
    pq.write_table(table, part / "part-0.parquet")
    return root


@pytest.fixture
def cuauhtemoc_features(conn) -> tuple[str, str]:
    """Seed: one municipality feature (so the nom_mun → CVE_MUN lookup
    in :func:`inegi_censo._build_muni_lookup` finds 'Cuauhtémoc' →
    '015'), plus two block features whose CVEGEOs match the census
    fixture, plus one extra block whose CVEGEO is intentionally
    unmatched so the unmatched-CVEGEO branch can be asserted too."""
    src = _register_source(
        conn,
        name=f"censo_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run = _start_run(conn, src)

    muni_type = feat.get_type_id(conn, "MEX", "municipality")
    _insert_feature(
        conn,
        type_id=muni_type,
        source_id=src,
        ingestion_run_id=run,
        dataset_version="v1",
        source_record_ref="09015",
        geometry_wkt="POLYGON((-99.2 19.4,-99.1 19.4,-99.1 19.5,-99.2 19.5,-99.2 19.4))",
        attributes={"CVE_ENT": "09", "CVE_MUN": "015", "NOMGEO": "Cuauhtémoc"},
    )

    block_type = feat.get_type_id(conn, "MEX", "block")
    cvegeos = ["0901500010019001", "0901500010019002", "0901500010019999"]
    feature_ids = []
    for cv in cvegeos:
        fid = _insert_feature(
            conn,
            type_id=block_type,
            source_id=src,
            ingestion_run_id=run,
            dataset_version="v1",
            source_record_ref=cv,
            geometry_wkt="POLYGON((-99.135 19.43,-99.134 19.43,-99.134 19.431,-99.135 19.431,-99.135 19.43))",
        )
        feature_ids.append(fid)
    yield feature_ids[0], feature_ids[1]

    # Tests using this fixture call bootstrap(), which COMMITS — so the
    # rollback in the shared `conn` fixture no longer unwinds these seed
    # rows. Remove them explicitly (FK order: observation → feature → run
    # → source) so a committed municipality feature doesn't leak into
    # test_bootstrap_raises_when_no_municipalities_loaded.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM curated_geo.observation WHERE feature_id IN "
            "(SELECT id FROM curated_geo.feature WHERE source_id = %s)",
            (src,),
        )
        cur.execute("DELETE FROM curated_geo.feature WHERE source_id = %s", (src,))
        cur.execute(
            "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s", (src,)
        )
        cur.execute("DELETE FROM curated_geo.data_source_catalog WHERE id = %s", (src,))
    conn.commit()


# ─── Helpers ──────────────────────────────────────────────────────────


def _fetch_obs_for(conn, *, source_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT feature_id::text, attribute_key, value_numeric, value_type
            FROM curated_geo.observation
            WHERE source_id = %s
              AND is_current
            ORDER BY feature_id, attribute_key
            """,
            (source_id,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─── Tests ────────────────────────────────────────────────────────────


def test_bootstrap_writes_one_observation_per_feature_and_column(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    """6 headline columns × 2 matched features = 12 observations."""
    result = inegi_censo.bootstrap(
        conn,
        cve_ent_filter=["9"],
        source_root=fake_censo_root,
    )
    assert result.rows_read == 2
    assert result.features_matched == 2
    assert result.observations_written == 2 * HEADLINE_COL_COUNT

    obs_rows = _fetch_obs_for(conn, source_id=result.source_id)
    keys = {row["attribute_key"] for row in obs_rows}
    expected = {f"censo_2020_{c}" for c in inegi_censo.HEADLINE_COLUMNS}
    assert keys == expected


def test_bootstrap_round_trips_population_value(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    """pobtot = 100 in the fixture must land verbatim as value_numeric."""
    feat_a, _ = cuauhtemoc_features
    result = inegi_censo.bootstrap(
        conn,
        cve_ent_filter=["9"],
        source_root=fake_censo_root,
    )
    obs_rows = _fetch_obs_for(conn, source_id=result.source_id)
    by_key = {(r["feature_id"], r["attribute_key"]): r for r in obs_rows}
    pobtot_a = by_key[(feat_a, "censo_2020_pobtot")]
    assert pobtot_a["value_numeric"] == 100.0
    assert pobtot_a["value_type"] == "numeric"


def test_bootstrap_skips_unmatched_cvegeo(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    """Census rows whose CVEGEO doesn't match a feature are dropped
    silently (counted) — happens when manzanas were renumbered between
    censo and marco vintages."""
    extra = fake_censo_root / "entidad=9" / "part-1.parquet"
    pq.write_table(
        pa.table(
            {
                "nom_mun": pa.array(["Cuauhtémoc"], type=pa.string()),
                "loc": pa.array([1], type=pa.int64()),
                "ageb": pa.array(["9999"], type=pa.string()),
                "mza": pa.array([42], type=pa.int64()),
                "pobtot": pa.array([10], type=pa.int64()),
                "pobmas": pa.array([5.0], type=pa.float64()),
                "pobfem": pa.array([5.0], type=pa.float64()),
                "p_15ymas": pa.array([8.0], type=pa.float64()),
                "p_18ymas": pa.array([7.0], type=pa.float64()),
                "vivtot": pa.array([3], type=pa.int64()),
            }
        ),
        extra,
    )

    result = inegi_censo.bootstrap(
        conn,
        cve_ent_filter=["9"],
        source_root=fake_censo_root,
    )
    assert result.rows_read == 3
    assert result.features_matched == 2
    assert result.rows_skipped_unmatched_cvegeo == 1
    assert result.observations_written == 2 * HEADLINE_COL_COUNT


def test_bootstrap_records_lineage_via_storage_uri(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    result = inegi_censo.bootstrap(
        conn,
        cve_ent_filter=["9"],
        source_root=fake_censo_root,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT storage_uri, storage_type, integration_role "
            "FROM curated_geo.data_source_catalog WHERE id = %s",
            (result.source_id,),
        )
        storage_uri, storage_type, integration_role = cur.fetchone()
    assert storage_uri == inegi_censo.AZURE_PREFIX
    assert storage_type == "tabular"
    assert integration_role == "enrichment"


def test_bootstrap_marks_ingestion_run_succeeded(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    result = inegi_censo.bootstrap(
        conn,
        cve_ent_filter=["9"],
        source_root=fake_censo_root,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, details FROM curated_geo.ingestion_run WHERE id = %s",
            (result.ingestion_run_id,),
        )
        status, details = cur.fetchone()
    assert status == "succeeded"
    assert details["features_matched"] == 2
    assert details["observations_written"] == 2 * HEADLINE_COL_COUNT
    assert details["rows_skipped_unmatched_cvegeo"] == 0


def test_bootstrap_subset_columns_writes_only_those_keys(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    """Caller can ingest a strict subset of headline columns."""
    result = inegi_censo.bootstrap(
        conn,
        cve_ent_filter=["9"],
        columns=("pobtot", "vivtot"),
        source_root=fake_censo_root,
    )
    assert result.observations_written == 4  # 2 features × 2 cols

    obs_rows = _fetch_obs_for(conn, source_id=result.source_id)
    keys = {row["attribute_key"] for row in obs_rows}
    assert keys == {"censo_2020_pobtot", "censo_2020_vivtot"}


def test_bootstrap_raises_when_source_missing(conn, tmp_path):
    with pytest.raises(FileNotFoundError, match="2020 census parquet not found"):
        inegi_censo.bootstrap(
            conn,
            cve_ent_filter=["9"],
            source_root=tmp_path / "nope",
        )


def test_bootstrap_raises_when_filter_matches_no_partitions(
    conn,
    fake_censo_root: Path,
    cuauhtemoc_features,
):
    with pytest.raises(ValueError, match="No census rows"):
        inegi_censo.bootstrap(
            conn,
            cve_ent_filter=["99"],
            source_root=fake_censo_root,
        )


def test_bootstrap_raises_when_no_municipalities_loaded(
    conn,
    fake_censo_root: Path,
):
    """The (nom_mun → CVE_MUN) lookup is built from ``feature`` rows of
    type 'municipality'. Running the census ingest before any munis are
    loaded leaves the lookup empty and CVEGEOs would be malformed —
    bail loudly so the operator runs ``inegi-marco bootstrap`` first."""
    with pytest.raises(RuntimeError, match="No municipality features loaded"):
        inegi_censo.bootstrap(
            conn,
            cve_ent_filter=["9"],
            source_root=fake_censo_root,
        )
