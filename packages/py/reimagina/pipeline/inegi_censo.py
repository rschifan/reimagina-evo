"""Ingest INEGI 2020 census per-block attributes into curated_geo.observation.

Targets ``data/sources/azure/inegi/censo/manzanas_2020.parquet/``,
hive-partitioned by ``entidad=N/`` only (the 2020 release dropped the
nested ``mun`` partition that the 2010 release used). Municipality
identification lives inside each row as the human-readable ``nom_mun``
string, not a numeric ``CVE_MUN`` — see :func:`_build_muni_lookup` for
how we recover the numeric code.

Each input row carries ~225 columns of demographic + housing variables
per manzana (city block). We ingest a small headline subset as
``observation`` rows, joined to the existing block ``feature`` table by
CVEGEO. Headline coverage is enough for the prototype's population
indicator family; the rest can be added by extending
``HEADLINE_COLUMNS`` without code changes.

Why a separate pipeline from ``inegi_marco``: the geometry pipeline
writes ``feature`` rows with ``role='analysis_unit'``; this pipeline
writes ``observation`` rows with ``value_type='numeric'`` against
those features. They share the upstream provider but land in different
tables — keeping them in separate modules makes each one's contract
obvious.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from reimagina.paths import REPO_ROOT
from typing import Any
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads

from reimagina.data import catalog as cat
from reimagina.data import observation as obs


CENSO_PROVIDER = "INEGI"
CENSO_LICENSE = "Libre uso — Términos de Uso de la Información del INEGI"
CENSO_VERSION = "2020-censo"
CENSO_DATASET_NAME = "México · Censo de Población 2020 — manzanas (INEGI)"

# Default location relative to repo root; overridable for tests.
DEFAULT_SOURCE_ROOT = Path("data/sources/azure/inegi/censo/manzanas_2020.parquet")

AZURE_PREFIX = "azure://cfcetlsadls/raw/inegi/censo/manzanas_2020.parquet"


# Headline subset of the ~225-column census schema. Names match the
# INEGI columns 1:1 so downstream tooling can grep cross-system.
# ``attribute_key`` in the observation table is built as
# ``censo_2020_<column>``.
#
# 2020 retains the same headline names as 2010 (pobtot/pobmas/pobfem/…),
# so the picker layout doesn't have to change. Housing splits are still
# suppressed at manzana grain in 2020 (vivpar_hab, tvivhab, …) — only
# ``vivtot`` carries values; the rest are deliberately left out of this
# tuple so they don't pollute the picker with all-zero choropleths.
HEADLINE_COLUMNS: tuple[str, ...] = (
    # Population — total + sex breakdown
    "pobtot",
    "pobmas",
    "pobfem",
    # Population — adult-age subsets (proxy for voting / labour pools)
    "p_15ymas",
    "p_18ymas",
    # Housing — total dwellings only
    "vivtot",
)

ATTRIBUTE_KEY_PREFIX = "censo_2020"


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
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ─── Read + CVEGEO build ─────────────────────────────────────────────────


def _normalise_partition_value(v: int | str) -> str:
    """The 2020 census partitions use unpadded ``entidad=9`` directories.
    Coerce any int / padded / unpadded input to the canonical unpadded
    string the partition system expects."""
    text = str(v).lstrip("0") or "0"
    return text


def _normalise_name(s: str) -> str:
    """Canonicalise a municipality name for cross-source matching.

    INEGI marco_geostadistico stores ``NOMGEO='Cuauhtémoc'`` (NFC) while
    some downstream sources strip diacritics or change case. Normalise
    to lower-case, accent-stripped form so the join doesn't miss munis
    just because one source carries `Cuauhtemoc` instead of
    `Cuauhtémoc`. Lower-case + NFD strip is the standard recipe.
    """
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower().strip()


def _build_muni_lookup(conn) -> dict[tuple[str, str], str]:
    """``(cve_ent, normalised_nom_mun) → cve_mun`` from our own features.

    The 2020 census carries ``nom_mun`` (string) but no ``CVE_MUN``. The
    INEGI marco_geostadistico ingest already loaded municipality
    features with both fields in ``feature.attributes`` — derive the
    map from those rows instead of pulling in a separate crosswalk.
    Scoped automatically: we'll only build entries for whatever munis
    are actually loaded.
    """
    with conn.cursor() as cur:
        # ``attributes->>'KEY' IS NOT NULL`` avoids the psycopg2 ``?``
        # paramstyle clash the JSONB ``attributes ? 'KEY'`` operator would
        # trigger. The Python-side filter then drops any row that's missing
        # one of the three components (NULL would skip the dict entry anyway).
        cur.execute(
            """
            SELECT attributes->>'CVE_ENT', attributes->>'NOMGEO',
                   attributes->>'CVE_MUN'
            FROM curated_geo.feature
            WHERE type_slug = 'municipality'
              AND attributes->>'CVE_ENT' IS NOT NULL
              AND attributes->>'CVE_MUN' IS NOT NULL
              AND attributes->>'NOMGEO' IS NOT NULL
            """
        )
        rows = cur.fetchall()
    # Normalise entidad to the unpadded form the parquet partition uses
    # (e.g. '02' from feature attributes → '2', matching `entidad=2/`).
    # Without this strip, build_cvegeo's later lookup misses every row
    # because the parquet side is unpadded but the DB side is padded.
    return {
        (_normalise_partition_value(cve_ent), _normalise_name(str(nom_mun))): str(
            cve_mun
        )
        for cve_ent, nom_mun, cve_mun in rows
    }


def read_layer_to_dataframe(
    parquet_dir: Path,
    *,
    cve_ent_filter: list[str] | None,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
) -> pd.DataFrame:
    """Read a filtered subset of the partitioned 2020 census parquet.

    Returns a pandas DataFrame containing the entidad partition column,
    ``nom_mun`` + composite-key columns (``loc/ageb/mza``), and the
    requested headline columns. Empty when no rows match the filter.

    The cve_mun filter that the 2010 pipeline accepted is gone: 2020
    has no ``mun`` partition, so the most efficient filter is
    "all rows in this entidad" + a string ``nom_mun`` filter at the
    Python layer if needed. In practice we don't filter on muni at all
    here — the CVEGEO join with the existing feature table already
    drops rows for blocks we don't have geometry for, and that's the
    only filter that matters for choropleth coverage.
    """
    partitioning = pads.HivePartitioning(pa.schema([("entidad", pa.string())]))
    dataset = pads.dataset(
        str(parquet_dir), format="parquet", partitioning=partitioning
    )

    requested_cols = list(
        dict.fromkeys(["entidad", "nom_mun", "loc", "ageb", "mza", *columns])
    )

    if cve_ent_filter:
        flt = pads.field("entidad").isin(
            [_normalise_partition_value(v) for v in cve_ent_filter]
        )
        table = dataset.to_table(filter=flt, columns=requested_cols)
    else:
        table = dataset.to_table(columns=requested_cols)
    return table.to_pandas()


def build_cvegeo(
    df: pd.DataFrame, muni_lookup: dict[tuple[str, str], str]
) -> pd.Series:
    """Reconstruct the 16-character CVEGEO from entidad/nom_mun + (loc, ageb, mza).

    Format: ``EE + MMM + LLLL + AAAA + ZZZ`` (entidad 2, municipio 3,
    localidad 4, ageb 4, manzana 3). 2020 doesn't carry ``CVE_MUN``
    directly, so we look it up from the marco-derived ``muni_lookup``.
    Rows whose ``nom_mun`` doesn't appear in the lookup get an empty
    string CVEGEO and will be dropped at the join step (logged as
    skipped).
    """
    cve_ent = df["entidad"].astype(str).str.zfill(2)
    nom_mun_norm = df["nom_mun"].astype(str).map(_normalise_name)
    cve_mun_raw = pd.Series(
        [
            muni_lookup.get((str(e).lstrip("0") or "0", n), "")
            for e, n in zip(df["entidad"].astype(str), nom_mun_norm)
        ],
        index=df.index,
    )
    full = (
        cve_ent
        + cve_mun_raw.str.zfill(3)
        + df["loc"].astype("Int64").astype(str).str.zfill(4)
        + df["ageb"].astype(str).str.zfill(4)
        + df["mza"].astype("Int64").astype(str).str.zfill(3)
    )
    # Rows whose nom_mun didn't resolve to a CVE_MUN get an empty string
    # so the orchestrator can drop them by length check. Padding "" to
    # "000" would silently produce a real-looking but wrong CVEGEO.
    return full.where(cve_mun_raw != "", "")


# ─── Source registration ───────────────────────────────────────────────


def ensure_censo_source(conn) -> str:
    """Idempotent data_source_catalog row for the 2020 census ingest."""
    existing = cat.get_source(
        conn,
        name=CENSO_DATASET_NAME,
        provider=CENSO_PROVIDER,
        version=CENSO_VERSION,
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
            "INEGI Censo 2020 manzana-level demographics + housing. "
            "Read from the cfcetlsadls Azure data lake mirror."
        ),
    )


# ─── Feature lookup ────────────────────────────────────────────────────


def _fetch_feature_ids_by_cvegeo(conn, cvegeos: list[str]) -> dict[str, str]:
    """Map CVEGEO → feature_id for blocks (type_slug='block')."""
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
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    source_root: Path | None = None,
    code_version: str = "dev",
    orchestrator_run_id: str | None = None,
) -> CensoBootstrapResult:
    """Read the filtered 2020 census subset, join to features, write observations.

    ``cve_ent_filter`` is the workhorse: keep it small or you'll ingest
    all of Mexico. Municipality scoping is implicit — only blocks with
    a matching ``feature.source_record_ref`` (16-char CVEGEO) get
    written. Run :func:`reimagina.pipeline.inegi_marco.bootstrap` first
    so the municipality + block features exist for the join.

    Unmatched rows (CVEGEO with no corresponding feature) are dropped
    silently — happens when the marco 2024 boundaries don't include
    every 2020 manzana, or when ``nom_mun`` resolves to a CVE_MUN we
    haven't loaded. The count is reported in the return value.
    """
    if source_root is not None:
        root = Path(source_root)
    else:
        root = DEFAULT_SOURCE_ROOT
    if not root.is_absolute():
        root = REPO_ROOT / root

    if not root.exists():
        raise FileNotFoundError(
            f"INEGI 2020 census parquet not found: {root}. "
            "Verify the data lake mirror at "
            "data/sources/azure/inegi/censo/manzanas_2020.parquet/ exists."
        )

    df = read_layer_to_dataframe(
        root,
        cve_ent_filter=cve_ent_filter,
        columns=columns,
    )
    rows_read = len(df)
    if df.empty:
        raise ValueError(
            "No census rows for the given filter. Check cve_ent values "
            "and that the parquet partitions exist on disk."
        )

    muni_lookup = _build_muni_lookup(conn)
    if not muni_lookup:
        raise RuntimeError(
            "No municipality features loaded — run "
            "`inegi-marco bootstrap --layers areas_municipales` first so "
            "the (CVE_ENT, nom_mun) → CVE_MUN lookup can be built."
        )

    df = df.copy()
    df["cvegeo"] = build_cvegeo(df, muni_lookup)
    # Rows whose nom_mun didn't resolve to a CVE_MUN produce a CVEGEO
    # with empty mun digits — surfaces as a 13-char string instead of
    # 16. Drop them rather than feed them to the feature lookup; they'd
    # never match anyway and would inflate ``rows_skipped`` artificially.
    df = df[df["cvegeo"].str.len() == 16]

    cvegeo_to_feature = _fetch_feature_ids_by_cvegeo(conn, df["cvegeo"].tolist())
    matched_mask = df["cvegeo"].isin(cvegeo_to_feature.keys())
    matched_df = df[matched_mask].copy()
    matched_df["feature_id"] = matched_df["cvegeo"].map(cvegeo_to_feature)
    rows_skipped = int(rows_read - matched_mask.sum())

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
                continue
            for feature_id, raw in zip(
                matched_df["feature_id"].tolist(),
                matched_df[col].tolist(),
            ):
                if pd.isna(raw):
                    continue
                records.append(
                    {
                        "feature_id": feature_id,
                        "attribute_key": f"{ATTRIBUTE_KEY_PREFIX}_{col}",
                        "value_type": "numeric",
                        "value": float(raw),
                    }
                )

        observations_written = obs.bulk_upsert_observations(
            conn,
            records,
            source_id=source_id,
            ingestion_run_id=run_id,
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
            diagnostics={
                "attribute_keys": [f"{ATTRIBUTE_KEY_PREFIX}_{c}" for c in columns],
                "muni_lookup_size": len(muni_lookup),
            },
        )
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="failed",
            details={"error_type": type(exc).__name__, "error_message": str(exc)},
        )
        conn.commit()
        raise
