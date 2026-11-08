"""Generate the database and API reference (Schedule A, Work 3).

Work 3 is accepted when "the schema and endpoints correspond to the delivered
system". That is a correspondence test, so this document is *derived* from the
system rather than written about it: the schema is introspected out of a
migrated database, and the endpoints come from the FastAPI application's own
OpenAPI document. A hand-written reference is a claim about the system; an
introspected one is the system.

Neither source needs a running server. The API spec is produced by importing
the app and calling ``app.openapi()``, so the document can be regenerated at
the delivery commit without standing anything up.

What it needs: ``REIMAGINA_APP_DSN`` (or ``--dsn``) pointing at a database
migrated to head. Anything less and the output would describe a schema nobody
is delivering, which is the one failure mode this document cannot have.

Usage::

    python scripts/build_schema_reference.py
    python scripts/build_schema_reference.py --dsn postgresql://...
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "delivery" / "03_database_and_api_reference.md"

SCHEMAS = ("app_core", "curated_geo", "serving_geo", "explore", "jobs_runtime")

# One line of orientation each. The architecture document carries the full
# explanation; a reference needs only enough for a reader to know which
# section answers their question.
SCHEMA_ROLES = {
    "app_core": "Tenancy. The project a scenario, AOI or feature belongs to.",
    "curated_geo": (
        "Source of truth. Features, their observations, overlaps, indicator "
        "results, the asset graph and scenario overrides. Everything else is "
        "derived from here."
    ),
    "serving_geo": (
        "The read contract for the API, the worker and the tile server. Views "
        "only — nothing writes here, and nothing outside this schema is read "
        "by the application."
    ),
    "explore": (
        "The engineer-facing inspection contract. Views built for humans "
        "reading SQL, deliberately not depended on by application code."
    ),
    "jobs_runtime": "Run and job bookkeeping for the ops plane.",
}


def _rows(cur: Any, sql: str, args: tuple = ()) -> list[tuple]:
    cur.execute(sql, args)
    return list(cur.fetchall())


def _escape(text: str) -> str:
    """Markdown tables break on a bare pipe, and some defaults contain one."""
    return text.replace("|", "\\|")


def database_section(dsn: str) -> list[str]:
    out: list[str] = []
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        server = _rows(cur, "select version()")[0][0].split(" on ")[0]
        postgis = _rows(cur, "select postgis_lib_version()")[0][0]
        head = _rows(cur, "select version_num from alembic_version")[0][0]

        out += [
            "## Database",
            "",
            f"Introspected from a database migrated to Alembic revision "
            f"**`{head}`**, running {server} with PostGIS {postgis}.",
            "",
        ]

        for schema in SCHEMAS:
            tables = _rows(
                cur,
                """
                select table_name, table_type
                from information_schema.tables
                where table_schema = %s
                order by table_type, table_name
                """,
                (schema,),
            )
            if not tables:
                continue
            base = [t for t, kind in tables if kind == "BASE TABLE"]
            views = [t for t, kind in tables if kind == "VIEW"]
            out += [
                f"### `{schema}`",
                "",
                SCHEMA_ROLES.get(schema, ""),
                "",
                f"{len(base)} table(s), {len(views)} view(s).",
                "",
            ]

            for table in base:
                out += _table_detail(cur, schema, table)

            if views:
                out += [f"#### Views in `{schema}`", ""]
                for view in views:
                    cols = _rows(
                        cur,
                        """
                        select column_name, data_type
                        from information_schema.columns
                        where table_schema = %s and table_name = %s
                        order by ordinal_position
                        """,
                        (schema, view),
                    )
                    listed = ", ".join(f"`{c}` {t}" for c, t in cols)
                    out += [f"- **`{schema}.{view}`** — {listed}", ""]

        out += _geometry_section(cur)
    return out


def _table_detail(cur: Any, schema: str, table: str) -> list[str]:
    cols = _rows(
        cur,
        """
        select column_name, data_type, is_nullable, coalesce(column_default,'')
        from information_schema.columns
        where table_schema = %s and table_name = %s
        order by ordinal_position
        """,
        (schema, table),
    )
    constraints = _rows(
        cur,
        """
        select con.conname, pg_get_constraintdef(con.oid)
        from pg_constraint con
        join pg_class rel on rel.oid = con.conrelid
        join pg_namespace ns on ns.oid = rel.relnamespace
        where ns.nspname = %s and rel.relname = %s
        order by con.contype, con.conname
        """,
        (schema, table),
    )
    indexes = _rows(
        cur,
        "select indexname, indexdef from pg_indexes "
        "where schemaname = %s and tablename = %s order by indexname",
        (schema, table),
    )

    out = [
        f"#### `{schema}.{table}`",
        "",
        "| Column | Type | Null | Default |",
        "|---|---|---|---|",
    ]
    for name, dtype, nullable, default in cols:
        out.append(
            f"| `{name}` | {dtype} | {'yes' if nullable == 'YES' else 'no'} "
            f"| {_escape(default) or '—'} |"
        )
    out.append("")
    if constraints:
        out += ["Constraints:", ""]
        out += [
            f"- `{name}` — {_escape(definition)}" for name, definition in constraints
        ]
        out.append("")
    if indexes:
        out += ["Indexes:", ""]
        out += [f"- `{name}` — {_escape(definition)}" for name, definition in indexes]
        out.append("")
    return out


def _geometry_section(cur: Any) -> list[str]:
    rows = _rows(
        cur,
        """
        select f_table_schema, f_table_name, f_geometry_column, type, srid, coord_dimension
        from geometry_columns
        order by f_table_schema, f_table_name, f_geometry_column
        """,
    )
    out = [
        "### Geometry columns",
        "",
        "Every geometry column, with the SRID and dimension enforced by its "
        "type. A single SRID across the platform is what lets any two layers "
        "be compared without a reprojection step.",
        "",
        "| Table | Column | Type | SRID | Dimensions |",
        "|---|---|---|---|---|",
    ]
    for schema, table, column, gtype, srid, dims in rows:
        out.append(f"| `{schema}.{table}` | `{column}` | {gtype} | {srid} | {dims} |")
    out.append("")
    return out


def api_section() -> list[str]:
    # `apps` is at the repo root; the backend namespace is under packages/py.
    for entry in (ROOT, ROOT / "packages" / "py"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    from apps.api.app import app  # noqa: PLC0415 — import cost only when generating

    spec = app.openapi()
    paths: dict[str, Any] = spec.get("paths", {})

    grouped: dict[str, list[str]] = {}
    for path, operations in sorted(paths.items()):
        for method, operation in sorted(operations.items()):
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            tags = operation.get("tags") or ["untagged"]
            summary = (operation.get("summary") or "").strip()
            params = operation.get("parameters") or []
            names = ", ".join(f"`{p['name']}`" for p in params) or "—"
            body = "yes" if operation.get("requestBody") else "no"
            grouped.setdefault(str(tags[0]), []).append(
                f"| `{method.upper()}` | `{path}` | {summary or '—'} | {names} | {body} |"
            )

    out = [
        "## API",
        "",
        f"{sum(len(v) for v in grouped.values())} endpoints, taken from the "
        f"application's own OpenAPI document "
        f"(`{spec['info']['title']}` {spec['info'].get('version', '')}). The "
        "machine-readable form is served at `/openapi.json` and rendered at "
        "`/docs`.",
        "",
    ]
    for tag in sorted(grouped):
        out += [
            f"### `{tag}`",
            "",
            "| Method | Path | Summary | Parameters | Request body |",
            "|---|---|---|---|---|",
            *grouped[tag],
            "",
        ]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the schema/API reference.")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("REIMAGINA_APP_DSN", ""),
        help="Database migrated to head. Defaults to $REIMAGINA_APP_DSN.",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        print("No DSN. Set REIMAGINA_APP_DSN or pass --dsn.", file=sys.stderr)
        return 2

    document = [
        "---",
        'title: "Database and API reference"',
        'subtitle: "Reimagina · Conceptualización y diseño de una centralidad '
        'urbana en Mexicali — Schedule A, Work 3"',
        'author: "Rossano Schifanella"',
        f'date: "{date.today():%-d %B %Y}"',
        "---",
        "",
        "## Document control",
        "",
        "| | |",
        "|---|---|",
        "| **Project** | Conceptualización y diseño de una centralidad urbana "
        "en Mexicali |",
        "| **Deliverable** | Schedule A, Work 3 — Database/API schemas and "
        "documentation |",
        "| **Status** | Draft for review |",
        "| **Audience** | Technical reviewers, maintainers and operators |",
        "",
        "> **Generated — do not edit.** Regenerate with",
        "> `python scripts/build_schema_reference.py`. The schema is",
        "> introspected from a migrated database and the endpoints come from",
        "> the application's own OpenAPI document, so this reference describes",
        "> the delivered system by construction rather than by assertion.",
        "",
        *database_section(args.dsn),
        *api_section(),
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(document) + "\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
