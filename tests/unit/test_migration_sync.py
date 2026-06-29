"""Guard against silent migration drift between the storage backends.

DuckDB is dev/compute-only and frozen at r024 (see
``infra/db/migrations/common/manifest.py``). Postgres (Alembic) is the source of
truth and may go further. These tests fail if:

- a DuckDB ``.sql`` file is added/removed without updating the manifest (the
  exact bug that left an orphan ``r026.sql`` unapplied), or
- the manifest revisions stop being a prefix of the Alembic chain.

Pure-Python — no database required. The manifest is loaded by path so the test
does not depend on package install layout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DUCKDB_DIR = REPO / "infra" / "db" / "migrations" / "duckdb"
ALEMBIC_DIR = REPO / "infra" / "db" / "migrations" / "versions"
MANIFEST = REPO / "infra" / "db" / "migrations" / "common" / "manifest.py"


def _load_revisions() -> list[str]:
    spec = importlib.util.spec_from_file_location("_manifest", MANIFEST)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return list(module.REVISIONS)


def _rev_prefixes(directory: Path, suffix: str) -> set[str]:
    # filenames look like "r001_schemas_and_extensions.sql" -> "r001"
    return {p.name.split("_", 1)[0] for p in directory.glob(f"*{suffix}")}


def test_duckdb_sql_files_match_manifest() -> None:
    manifest = set(_load_revisions())
    duckdb = _rev_prefixes(DUCKDB_DIR, ".sql")
    orphan = duckdb - manifest  # a .sql the runner never applies
    missing = manifest - duckdb  # a manifest rev with no .sql
    assert duckdb == manifest, (
        f"DuckDB .sql files out of sync with manifest REVISIONS — "
        f"orphan(unapplied)={sorted(orphan)} missing(no file)={sorted(missing)}"
    )


def test_manifest_is_a_prefix_of_alembic() -> None:
    manifest = set(_load_revisions())
    alembic = _rev_prefixes(ALEMBIC_DIR, ".py")
    absent = manifest - alembic
    assert (
        not absent
    ), f"manifest revisions absent from the Alembic chain: {sorted(absent)}"
    # DuckDB is frozen at r024; Alembic is the source of truth and may go further,
    # never behind. (No assertion that they are equal — divergence beyond the
    # freeze point is expected and allowed.)
    assert manifest <= alembic


def test_revisions_are_contiguous_and_ordered() -> None:
    revisions = _load_revisions()
    assert revisions == sorted(
        revisions
    ), "manifest REVISIONS must be in ascending order"
    numbers = [int(r[1:]) for r in revisions]
    assert numbers == list(
        range(numbers[0], numbers[-1] + 1)
    ), f"manifest REVISIONS has a gap: {revisions}"
