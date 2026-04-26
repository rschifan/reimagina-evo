"""Ingest INEGI 2010 census per-block attributes into curated_geo.observation.

Targets ``data/sources/azure/inegi/censo/manzanas_2010.parquet/``,
hive-partitioned ``entidad=N/mun=N/`` (note: integer-style partition
values without zero padding — different from the marco_geostadistico
convention, which is the reason this pipeline exists separately).

Each input row carries 199 columns of demographic + housing variables
per manzana (city block). We ingest a small headline subset as
``observation`` rows, joined to the existing ``feature`` table by
CVEGEO. Headline coverage is enough for the prototype's
'population_density' indicator family; the rest can be added by
extending ``HEADLINE_COLUMNS`` without code changes.

Why a separate pipeline from ``inegi_marco``: the geometry pipeline
writes ``feature`` rows with ``role='analysis_unit'``; this pipeline
writes ``observation`` rows with ``value_type='numeric'`` against
those features. They share the upstream provider but land in different
tables under different schemas — keeping them in separate modules
makes each one's contract obvious.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads

from reimagina.data import catalog as cat
from reimagina.data import observation as obs


CENSO_PROVIDER = "INEGI"
CENSO_LICENSE = "Libre uso — Términos de Uso de la Información del INEGI"
CENSO_VERSION = "2010-censo"
CENSO_DATASET_NAME = "México · Censo de Población 2010 — manzanas (INEGI)"

# Default location relative to repo root; overridable for tests.
DEFAULT_SOURCE_ROOT = Path("data/sources/azure/inegi/censo/manzanas_2010.parquet")

AZURE_PREFIX = "azure://cfcetlsadls/raw/inegi/censo/manzanas_2010.parquet"


# Headline subset of the 199-column census schema. Names match the INEGI
# columns 1:1 so downstream tooling can grep cross-system. ``attribute_key``
# in the observation table is built as ``censo_2010_<column>``.
#
# Population variables are extensive (sum on rollup); housing too. So all
# of these get ``aggregation_method='sum'`` when rolled up to AGEB or
# municipality grain via the recipe layer.
HEADLINE_COLUMNS: tuple[str, ...] = (
    # Population — total + sex breakdown
    "pobtot", "pobmas", "pobfem",
    # Population — adult-age subsets (proxy for voting / labour pools)
    "p_15ymas", "p_18ymas",
    # Housing — total + inhabited private
    "vivtot", "vivpar_hab",
)

ATTRIBUTE_KEY_PREFIX = "censo_2010"


@dataclass(frozen=True)
class CensoBootstrapResult:
    """Returned by ``bootstrap``. Useful for CLI JSON output and tests."""

    source_id: str
    ingestion_run_id: str
    columns: tuple[str, ...]
    rows_read: int
    features_matched: int
    rows_skipped_unmatched_cvegeo: int
    observations_written: int
    cve_ent_filter: tuple[str, ...] | None
    cve_mun_filter: tuple[str, ...] | None
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ─── Read + CVEGEO build ─────────────────────────────────────────────────


def _normalise_partition_value(v: int | str) -> str:
    """The 2010 census partitions use unpadded ``entidad=9`` / ``mun=15``
    directories — the opposite of marco_geostadistico's zero-padded
    ``CVE_ENT=09``. Coerce any int / padded / unpadded input to the
    canonical unpadded string the partition system expects."""
    text = str(v).lstrip("0") or "0"
    return text


def read_layer_to_dataframe(
    parquet_dir: Path,
    *,
    cve_ent_filter: list[str] | None,
    cve_mun_filter: list[str] | None,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
) -> pd.DataFrame:
    """Read a filtered subset of the partitioned 2010 census parquet.

    ``columns`` is the headline set we want to expose as observations.
    The composite-key columns ``loc/ageb/mza`` plus the partition columns
    ``entidad/mun`` are always pulled too so we can build CVEGEO.

    Returns a pandas DataFrame with all columns as nullable types where
    applicable. Empty when no rows match the filter.
    """
    partitioning = pads.HivePartitioning(
        pa.schema([("entidad", pa.string()), ("mun", pa.string())])
    )
    dataset = pads.dataset(str(parquet_dir), format="parquet", partitioning=partitioning)

    requested_cols = list(dict.fromkeys(
        ["entidad", "mun", "loc", "ageb", "mza", *columns]
    ))

    filters = []
    if cve_ent_filter:
        filters.append(
            pads.field("entidad").isin(
                [_normalise_partition_value(v) for v in cve_ent_filter]
            )
        )
    if cve_mun_filter:
        filters.append(
            pads.field("mun").isin(
                [_normalise_partition_value(v) for v in cve_mun_filter]
            )
        )

    if filters:
        flt = filters[0]
        for extra in filters[1:]:
            flt = flt & extra
        table = dataset.to_table(filter=flt, columns=requested_cols)
    else:
        table = dataset.to_table(columns=requested_cols)
    return table.to_pandas()


def build_cvegeo(df: pd.DataFrame) -> pd.Series:
    """Reconstruct the 16-character CVEGEO from entidad/mun/loc/ageb/mza.

    Format: ``EE + MMM + LLLL + AAAA + ZZZ`` (entidad 2, municipio 3,
    localidad 4, ageb 4, manzana 3). Matches the 16-char CVEGEO that the
    marco_geostadistico ingest stored in ``feature.source_record_ref``,
    so a string equality is enough for the join.
    """
    return (
        df["entidad"].astype(str).str.zfill(2)
        + df["mun"].astype(str).str.zfill(3)
        + df["loc"].astype("Int64").astype(str).str.zfill(4)
        + df["ageb"].astype(str).str.zfill(4)
        + df["mza"].astype("Int64").astype(str).str.zfill(3)
    )


# ─── Source registration ───────────────────────────────────────────────


def ensure_censo_source(conn) -> str:
    """Idempotent data_source_catalog row for the 2010 census ingest."""
    existing = cat.get_source(
        conn, name=CENSO_DATASET_NAME, provider=CENSO_PROVIDER, version=CENSO_VERSION,
    )
    if existing is not None:
        return str(existing["id"])
    return cat.register_source(
        conn,
        name=CENSO_DATASET_NAME,
        provider=CENSO_PROVIDER,
        storage_type="tabular",
        integration_role="enrichment",
        storage_uri=AZURE_PREFIX,
        version=CENSO_VERSION,
        license=CENSO_LICENSE,
        notes=(
            "INEGI Censo 2010 manzana-level demographics + housing. "
            "Read from the cfcetlsadls Azure data lake (downloaded "
            "locally via Storage Explorer for ingest)."
        ),
    )


# ─── Feature lookup ────────────────────────────────────────────────────


def _fetch_feature_ids_by_cvegeo(conn, cvegeos: list[str]) -> dict[str, str]:
    """Map CVEGEO → feature_id for blocks (type_slug='block').

    Single-query batch lookup keyed off the 16-char source_record_ref
    that ``inegi_marco`` populated for every manzana feature. Returns
    only the matches; unmatched CVEGEOs are caller-handled.
    """
    if not cvegeos:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_record_ref, id::text
            FROM curated_geo.feature
            WHERE type_slug = 'block'
              AND source_record_ref = ANY(%s)
            """,
            (cvegeos,),
        )
        return dict(cur.fetchall())


# ─── Orchestrator ──────────────────────────────────────────────────────


def bootstrap(
    conn,
    *,
    cve_ent_filter: list[str] | None = None,
    cve_mun_filter: list[str] | None = None,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    source_root: Path | None = None,
    code_version: str = "dev",
    orchestrator_run_id: str | None = None,
) -> CensoBootstrapResult:
    """Read the filtered census subset, join to features, write observations.

    ``cve_ent_filter`` is the workhorse: keep it small or you'll ingest
    all of Mexico. ``cve_mun_filter`` further narrows within those states
    (matched by ``mun`` partition value, unpadded).

    Unmatched rows (CVEGEO with no corresponding feature) are dropped
    silently — happens when the marco 2024 boundaries don't include
    every 2010 manzana, which is normal for AGEB renumberings between
    censuses. The count is reported in the return value.
    """
    if source_root is not None:
        root = Path(source_root)
    else:
        root = DEFAULT_SOURCE_ROOT
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[4] / root

    if not root.exists():
        raise FileNotFoundError(
            f"INEGI 2010 census parquet not found: {root}. "
            "Verify the data lake mirror at "
            "data/sources/azure/inegi/censo/manzanas_2010.parquet/ exists."
        )

    df = read_layer_to_dataframe(
        root,
        cve_ent_filter=cve_ent_filter,
        cve_mun_filter=cve_mun_filter,
        columns=columns,
    )
    rows_read = len(df)
    if df.empty:
        raise ValueError(
            "No census rows for the given filter. Check cve_ent / cve_mun "
            "values and that the parquet partitions exist on disk."
        )

    df = df.copy()
    df["cvegeo"] = build_cvegeo(df)

    cvegeo_to_feature = _fetch_feature_ids_by_cvegeo(conn, df["cvegeo"].tolist())
    matched_mask = df["cvegeo"].isin(cvegeo_to_feature.keys())
    matched_df = df[matched_mask].copy()
    matched_df["feature_id"] = matched_df["cvegeo"].map(cvegeo_to_feature)
    rows_skipped = int((~matched_mask).sum())

    source_id = ensure_censo_source(conn)
    conn.commit()

    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="manual",
        orchestrator_run_id=orchestrator_run_id or str(uuid4()),
        transformation_step="silver_to_curated",
        code_version=code_version,
    )
    conn.commit()

    try:
        records = []
        for col in columns:
            if col not in matched_df.columns:
                # Column not present in the requested set; skip silently
                # so callers can ask for forward-compatible columns.
                continue
            for feature_id, raw in zip(
                matched_df["feature_id"].tolist(),
                matched_df[col].tolist(),
            ):
                if pd.isna(raw):
                    continue
                records.append({
                    "feature_id": feature_id,
                    "attribute_key": f"{ATTRIBUTE_KEY_PREFIX}_{col}",
                    "value_type": "numeric",
                    "value": float(raw),
                })

        observations_written = obs.bulk_upsert_observations(
            conn, records, source_id=source_id, ingestion_run_id=run_id,
        )

        cat.complete_ingestion_run(
            conn,
            run_id,
            status="succeeded",
            details={
                "rows_read": rows_read,
                "features_matched": int(matched_mask.sum()),
                "rows_skipped_unmatched_cvegeo": rows_skipped,
                "columns": list(columns),
                "observations_written": observations_written,
            },
        )
        conn.commit()

        return CensoBootstrapResult(
            source_id=source_id,
            ingestion_run_id=run_id,
            columns=tuple(columns),
            rows_read=rows_read,
            features_matched=int(matched_mask.sum()),
            rows_skipped_unmatched_cvegeo=rows_skipped,
            observations_written=observations_written,
            cve_ent_filter=tuple(cve_ent_filter) if cve_ent_filter else None,
            cve_mun_filter=tuple(cve_mun_filter) if cve_mun_filter else None,
            diagnostics={
                "attribute_keys": [f"{ATTRIBUTE_KEY_PREFIX}_{c}" for c in columns],
            },
        )
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        cat.complete_ingestion_run(
            conn, run_id, status="failed",
            details={"error_type": type(exc).__name__, "error_message": str(exc)},
        )
        conn.commit()
        raise
