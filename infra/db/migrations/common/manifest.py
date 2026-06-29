"""DuckDB migration revision order.

DuckDB is a dev/compute-only backend, frozen at r024. Postgres (Alembic) is the
source of truth for the serving schema; revisions beyond r024 are Postgres-only.
This list is the exact set of ``infra/db/migrations/duckdb/*.sql`` files applied,
in order. The migration-sync guard test (``tests/unit/test_migration_sync.py``)
asserts the files and this list stay in step, and that these revisions remain a
prefix of the Alembic chain.
"""

from __future__ import annotations

REVISIONS = [
    "r001",
    "r002",
    "r003",
    "r004",
    "r005",
    "r006",
    "r007",
    "r008",
    "r009",
    "r010",
    "r011",
    "r012",
    "r013",
    "r014",
    "r015",
    "r016",
    "r017",
    "r018",
    "r019",
    "r020",
    "r021",
    "r022",
    "r023",
    "r024",
]
