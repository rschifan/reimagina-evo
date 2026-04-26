"""Integration tests for reimagina.pipeline.inegi_marco.bootstrap.

Exercises the full Tier 1.2 pipeline against a migrated Postgres+PostGIS
test DB:

- Creates a synthetic hive-partitioned parquet on disk that mirrors the
  on-the-data-lake shape (CVE_ENT partition + WKB BLOB geometry column).
- Runs ``bootstrap()`` filtered by CVE_ENT.
- Asserts a ``data_source_catalog`` row was registered with the
  azure:// storage_uri for lineage, an ``ingestion_run`` reached
  'succeeded', and ``curated_geo.feature`` rows landed with the
  right type_slug, geometry, and attributes.

Test isolation note: the production ``bootstrap()`` commits its source
+ feature inserts (so re-runs are durable). That breaks the per-test
``conn`` rollback we'd normally rely on — a second test using the same
spec would see existing rows and report 0 writes. Each test therefore
patches ``INEGI_MARCO_VERSION`` to a unique string so its source row is
genuinely fresh; the session-scoped ``migrated_db`` fixture's
downgrade-to-base wipes everything at the end.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely.wkb
from shapely.geometry import box

from reimagina.pipeline import inegi_marco


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_source_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """Force a unique INEGI_MARCO_VERSION per test so each test creates its
    own data_source_catalog row (otherwise the per-test ``conn`` rollback
    can't undo the commits inside ``bootstrap()``)."""
    version = f"test-{uuid4().hex[:8]}"
    monkeypatch.setattr(inegi_marco, "INEGI_MARCO_VERSION", version)
    return version


@pytest.fixture
def isolated_silver_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Silver writes into ``tmp_path`` so test runs don't leak files
    into the real ``data/silver/`` tree.

    Mirrors the same trick ``test_raster_ingest::isolated_data_dir`` uses —
    we patch the module-level constant rather than reload the module so
    references held at file-load time still point at the patched value.
    """
    import reimagina.pipeline.silver as silver_mod

    silver_dir = tmp_path / "silver"
    silver_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(silver_mod, "SILVER_DIR", silver_dir)
    return silver_dir


@pytest.fixture
def fake_marco_root(tmp_path: Path) -> Path:
    """Lay down a tiny INEGI marco mirror with three CVE_ENT partitions.

    Layout matches the real data lake:

        <root>/
          areas_estatales.parquet/CVE_ENT=02/part-0.parquet
          areas_estatales.parquet/CVE_ENT=09/part-0.parquet
          areas_estatales.parquet/CVE_ENT=19/part-0.parquet     (excluded)
          areas_municipales.parquet/CVE_ENT=02/part-0.parquet
          areas_municipales.parquet/CVE_ENT=09/part-0.parquet
    """
    root = tmp_path / "marco"

    # --- areas_estatales: one row per partition ---
    _write_partition(
        root / "areas_estatales.parquet", cve_ent="02",
        rows=[{"CVEGEO": "02", "NOMGEO": "Baja California",
               "geom": box(-117.5, 28.0, -113.0, 33.0)}],
        with_cve_mun=False,
    )
    _write_partition(
        root / "areas_estatales.parquet", cve_ent="09",
        rows=[{"CVEGEO": "09", "NOMGEO": "Ciudad de México",
               "geom": box(-99.4, 19.0, -98.9, 19.6)}],
        with_cve_mun=False,
    )
    _write_partition(
        root / "areas_estatales.parquet", cve_ent="19",
        rows=[{"CVEGEO": "19", "NOMGEO": "Nuevo León",
               "geom": box(-101.0, 23.0, -98.0, 27.0)}],
        with_cve_mun=False,
    )

    # --- areas_municipales: BC has 1 muni, CDMX has 2 munis ---
    _write_partition(
        root / "areas_municipales.parquet", cve_ent="02",
        rows=[{"CVEGEO": "02002", "NOMGEO": "Mexicali", "CVE_MUN": 2,
               "geom": box(-115.5, 32.0, -115.0, 32.5)}],
        with_cve_mun=True,
    )
    _write_partition(
        root / "areas_municipales.parquet", cve_ent="09",
        rows=[
            {"CVEGEO": "09015", "NOMGEO": "Cuauhtémoc", "CVE_MUN": 15,
             "geom": box(-99.2, 19.4, -99.1, 19.5)},
            {"CVEGEO": "09012", "NOMGEO": "Tlalpan", "CVE_MUN": 12,
             "geom": box(-99.3, 19.2, -99.0, 19.4)},
        ],
        with_cve_mun=True,
    )
    return root


# ─── Helpers ──────────────────────────────────────────────────────────


def _write_partition(parquet_dir: Path, *, cve_ent: str,
                     rows: list[dict], with_cve_mun: bool) -> None:
    """Write one partition (multiple rows) so callers can populate a single
    CVE_ENT directory with several features in one go (mirrors the real
    INEGI data lake which has many municipalities under each CVE_ENT)."""
    part = parquet_dir / f"CVE_ENT={cve_ent}"
    part.mkdir(parents=True, exist_ok=True)
    columns = {
        "CVEGEO": pa.array([r["CVEGEO"] for r in rows], type=pa.string()),
        "NOMGEO": pa.array([r["NOMGEO"] for r in rows], type=pa.string()),
        "geometry": pa.array(
            [shapely.wkb.dumps(r["geom"]) for r in rows], type=pa.binary(),
        ),
    }
    if with_cve_mun:
        columns["CVE_MUN"] = pa.array([r["CVE_MUN"] for r in rows], type=pa.int32())
    pq.write_table(pa.table(columns), part / "part-0.parquet")


def _fetch_features_by_type(conn, type_slug: str, *, dataset_version: str) -> list[dict]:
    """Scope by ``dataset_version`` so accumulated rows from earlier tests
    in the same session don't leak into the assertions."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.name,
                   f.attributes,
                   f.dataset_version,
                   ST_Area(f.geometry) AS geom_area,
                   ftc.type_slug
            FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog ftc ON ftc.id = f.type_id
            WHERE ftc.type_slug = %s AND f.dataset_version = %s
            ORDER BY f.source_record_ref
            """,
            (type_slug, dataset_version),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─── Tests ────────────────────────────────────────────────────────────


def test_bootstrap_filters_to_pilot_states(
    conn, fake_marco_root: Path, isolated_silver_dir: Path, isolated_source_version: str,
):
    """End-to-end: stage + load areas_estatales for CVE_ENT IN ('02','09')
    only — Nuevo León (19) must be excluded."""
    result = inegi_marco.bootstrap(
        conn,
        layers=["areas_estatales"],
        cve_ent_filter=["02", "09"],
        source_root=fake_marco_root,
    )

    assert result["layers_loaded"] == ["areas_estatales"]
    layer_result = result["layers"][0]
    assert layer_result["type_slug"] == "state"
    assert layer_result["records_written"] == 2
    assert layer_result["cve_ent_filter"] == ["02", "09"]

    # Silver artifact should live in the isolated dir, not data/silver/.
    silver_path = Path(layer_result["silver_path"])
    assert silver_path.exists()
    assert isolated_silver_dir in silver_path.parents

    rows = _fetch_features_by_type(conn, "state", dataset_version=isolated_source_version)
    assert {r["name"] for r in rows} == {"Baja California", "Ciudad de México"}
    # Nuevo León must be absent — partition pruning filtered it out.
    assert "Nuevo León" not in {r["name"] for r in rows}


def test_bootstrap_loads_attributes_into_jsonb(
    conn, fake_marco_root: Path, isolated_silver_dir: Path, isolated_source_version: str,
):
    """Spec attributes_cols flow through into curated_geo.feature.attributes."""
    inegi_marco.bootstrap(
        conn,
        layers=["areas_municipales"],
        cve_ent_filter=["09"],
        source_root=fake_marco_root,
    )

    rows = _fetch_features_by_type(conn, "municipality", dataset_version=isolated_source_version)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Cuauhtémoc", "Tlalpan"}

    cuauhtemoc = by_name["Cuauhtémoc"]
    # attributes is jsonb; psycopg2 returns it as a Python dict.
    assert cuauhtemoc["attributes"]["CVE_ENT"] == "09"
    # CVE_MUN was an int32 in the source; keep the value coherent
    # (loader may serialise as int or string — assert by string repr).
    assert str(cuauhtemoc["attributes"]["CVE_MUN"]) == "15"
    assert cuauhtemoc["attributes"]["NOMGEO"] == "Cuauhtémoc"
    assert cuauhtemoc["dataset_version"] == inegi_marco.INEGI_MARCO_VERSION


def test_bootstrap_geometry_lands_with_real_area(
    conn, fake_marco_root: Path, isolated_silver_dir: Path, isolated_source_version: str,
):
    """WKB-decoded geometry must survive the load with a non-zero area
    (i.e. ``shapely.wkb.loads`` actually ran and the geometry wasn't
    dropped or zero-coerced somewhere)."""
    inegi_marco.bootstrap(
        conn,
        layers=["areas_municipales"],
        cve_ent_filter=["02"],
        source_root=fake_marco_root,
    )

    rows = _fetch_features_by_type(conn, "municipality", dataset_version=isolated_source_version)
    assert len(rows) == 1
    assert rows[0]["name"] == "Mexicali"
    # ST_Area in EPSG:4326 returns square degrees; the box is 0.5×0.5 = 0.25.
    assert rows[0]["geom_area"] == pytest.approx(0.25, rel=1e-6)


def test_bootstrap_registers_source_with_azure_lineage(
    conn, fake_marco_root: Path, isolated_silver_dir: Path,
):
    """data_source_catalog.storage_uri keeps the upstream Azure URL even
    though we read the locally-mirrored copy. This is the lineage signal
    the read surface uses to credit the source correctly."""
    result = inegi_marco.bootstrap(
        conn,
        layers=["areas_estatales"],
        cve_ent_filter=["02"],
        source_root=fake_marco_root,
    )
    source_id = result["layers"][0]["source_id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT storage_uri, provider, version
            FROM curated_geo.data_source_catalog
            WHERE id = %s
            """,
            (source_id,),
        )
        row = cur.fetchone()
    assert row is not None
    storage_uri, provider, version = row
    assert storage_uri.startswith(inegi_marco.INEGI_MARCO_AZURE_PREFIX)
    assert provider == inegi_marco.INEGI_MARCO_PROVIDER
    assert version == inegi_marco.INEGI_MARCO_VERSION


def test_bootstrap_marks_ingestion_run_succeeded(
    conn, fake_marco_root: Path, isolated_silver_dir: Path,
):
    """The Silver→curated step opens an ingestion_run; on success it must
    transition to status='succeeded' with a useful records_written count."""
    result = inegi_marco.bootstrap(
        conn,
        layers=["areas_estatales"],
        cve_ent_filter=["09"],
        source_root=fake_marco_root,
    )
    run_id = result["layers"][0]["ingestion_run_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, details FROM curated_geo.ingestion_run WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    assert row is not None
    status, details = row
    assert status == "succeeded"
    assert details["records_written"] == 1


def test_bootstrap_is_idempotent_on_source_registration(
    conn, fake_marco_root: Path, isolated_silver_dir: Path,
):
    """Re-running bootstrap must reuse the existing data_source_catalog
    row instead of erroring on the (provider, name, version) UNIQUE
    constraint. The features themselves dedup on (source_id,
    source_record_ref) inside the loader."""
    first = inegi_marco.bootstrap(
        conn,
        layers=["areas_estatales"],
        cve_ent_filter=["02"],
        source_root=fake_marco_root,
    )
    second = inegi_marco.bootstrap(
        conn,
        layers=["areas_estatales"],
        cve_ent_filter=["02"],
        source_root=fake_marco_root,
    )
    assert first["layers"][0]["source_id"] == second["layers"][0]["source_id"]


def test_bootstrap_rejects_unknown_layer(
    conn, fake_marco_root: Path, isolated_silver_dir: Path,
):
    with pytest.raises(ValueError, match="Unknown INEGI marco layer"):
        inegi_marco.bootstrap(
            conn,
            layers=["areas_estatales", "made_up_layer"],
            cve_ent_filter=["02"],
            source_root=fake_marco_root,
        )


def test_bootstrap_raises_when_filter_matches_nothing(
    conn, fake_marco_root: Path, isolated_silver_dir: Path,
):
    """A CVE_ENT filter that matches no partitions is almost always a
    typo; raise loudly rather than silently writing zero rows."""
    with pytest.raises(ValueError, match="No rows for layer"):
        inegi_marco.bootstrap(
            conn,
            layers=["areas_estatales"],
            cve_ent_filter=["99"],
            source_root=fake_marco_root,
        )


def test_bootstrap_loads_states_then_municipalities_in_order(
    conn, fake_marco_root: Path, isolated_silver_dir: Path,
):
    """When multiple layers are requested they must follow DEFAULT_LOAD_ORDER
    (coarse → fine), so child grains can later reference their parents."""
    result = inegi_marco.bootstrap(
        conn,
        layers=["areas_municipales", "areas_estatales"],  # caller order ignored
        cve_ent_filter=["02", "09"],
        source_root=fake_marco_root,
    )
    assert result["layers_loaded"] == ["areas_estatales", "areas_municipales"]
