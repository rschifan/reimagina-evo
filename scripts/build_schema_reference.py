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

The output is byte-stable for a given commit, schema and ``--date``: nothing
about the host it runs on (server version, locale clock) is written into it.

Usage::

    python scripts/build_schema_reference.py --dsn postgresql://... \\
        --date "20 November 2026"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "delivery" / "03_database_and_api_reference.md"
COMPOSE = ROOT / "infra" / "docker" / "docker-compose.yml"

VERSION = "1.0"
REVISIONS = [("1.0", "2026-11-20", "Issued with the v1.0.0 delivery")]

SCHEMAS = ("app_core", "curated_geo", "serving_geo", "explore", "jobs_runtime")

# One line of orientation each. The architecture document carries the full
# explanation; a reference needs only enough for a reader to know which
# section answers their question. Each claim about who reads a schema was
# checked against the code that does the reading.
SCHEMA_ROLES = {
    "app_core": (
        "Tenancy. The project a scenario or AOI belongs to, and the scenarios "
        "themselves. The API reads and writes it when scenarios are authored."
    ),
    "curated_geo": (
        "Source of truth. Features, their observations, overlaps, indicator "
        "results, the asset graph and scenario overrides. Everything else is "
        "derived from here. Besides the pipeline and the worker, the API "
        "reads it directly for the indicator catalog, scenario authoring and "
        "comparison, tile-source discovery and source attribution, and "
        "writes scenario edits and the scenario-scoped rows that follow "
        "from them."
    ),
    "serving_geo": (
        "The read contract for feature, indicator and map reads by the API, "
        "the worker and the tile server: views, one materialized view and "
        "the three vector-tile functions. Nothing writes rows here; the "
        "materialized view is refreshed when an ingestion run succeeds."
    ),
    "explore": (
        "The engineer-facing inspection contract: views built for humans "
        "reading SQL. One application read goes through it: an indicator's "
        "lineage (`GET /indicators/{slug}/lineage`) reads "
        "`explore.asset_freshness_v`."
    ),
    "jobs_runtime": (
        "Run and job bookkeeping for the ops plane. The API reads and writes "
        "it to submit, list, cancel and delete runs."
    ),
}

# Trigger functions carry no COMMENT in the migrations, so their purpose is
# stated here, summarised from each function body.
FUNCTION_PURPOSES = {
    "aoi_feature_before_write": (
        "Checks that `overlap_basis` fits the feature's geometry: presence for "
        "points, length for lines, area for polygons."
    ),
    "asset_dependency_validate": (
        "Only a derived asset may consume; the graph must stay acyclic; "
        "`derived_from` weights of one consumer are all null or sum to 1."
    ),
    "feature_before_write": (
        "Copies `type_slug` and `role` from the feature type; checks the "
        "geometry family, that `source_id` matches the ingestion run, and the "
        "parent hierarchy (no self-parent, admin level strictly increasing, "
        "no cycles); stamps `updated_at`."
    ),
    "file_asset_before_write": (
        "The source must store raster, imagery or point-cloud data, and "
        "`source_id` must match the ingestion run."
    ),
    "indicator_dependency_validate": (
        "Only a composite indicator may have dependencies; the graph must "
        "stay acyclic; weights of one parent are all null or sum to 1."
    ),
    "indicator_result_before_write": (
        "Fills `feature_type`, `indicator_slug` (both immutable afterwards) "
        "and `model_version`; checks the scenario matches the job run, the "
        "feature type is one the indicator applies to, exactly one value for "
        "a measured, estimated or imputed result and none otherwise, a "
        "`missing_reason` for missing results, and the catalog's valid range."
    ),
    "observation_before_write": (
        "Exactly one value column is set and it matches `value_type`; "
        "`source_id` must match the ingestion run; stamps `updated_at`."
    ),
}


def _rows(cur: Any, sql: str, args: tuple = ()) -> list[tuple]:
    cur.execute(sql, args)
    return list(cur.fetchall())


def _escape(text: str) -> str:
    """Markdown tables break on a bare pipe, and some defaults contain one."""
    return text.replace("|", "\\|")


def _first_paragraph(text: str | None) -> str:
    para = (text or "").strip().split("\n\n")[0]
    return " ".join(para.split())


def parse_date(text: str | None) -> date:
    """``"20 November 2026"`` or ISO; today when not given."""
    if not text:
        return date.today()
    for fmt in ("%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise SystemExit(f"--date {text!r}: expected e.g. '20 November 2026'")


def deployed_stack(compose: str) -> str:
    """The database image the delivery deploys, read from the compose file."""
    m = re.search(r"image:\s*pgrouting/pgrouting:(\d+)-([\d.]+)-([\d.]+)", compose)
    if not m:
        raise SystemExit(f"no pgrouting/pgrouting image in {COMPOSE}")
    pg, postgis, pgrouting = m.groups()
    return f"PostgreSQL {pg}, PostGIS {postgis} and pgRouting {pgrouting}"


# ── Database ────────────────────────────────────────────────────────────────


def database_section(dsn: str) -> list[str]:
    out: list[str] = []
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        head = _rows(cur, "select version_num from alembic_version")[0][0]
        out += [
            "## Database",
            "",
            f"The delivered stack runs {deployed_stack(COMPOSE.read_text())} "
            "(`infra/docker/docker-compose.yml`). The schema below is at "
            f"Alembic revision **`{head}`**.",
            "",
            "Types are as PostgreSQL prints them (`format_type`), so a geometry "
            "column shows its geometry type and SRID.",
            "",
            "Two things live in the same database without belonging to this "
            "schema. The `dbos` schema is created at runtime by the DBOS "
            "workflow library, which orchestrates runs, for its own checkpoint "
            "tables; it is third-party and not described here. The three tile "
            "functions in `serving_geo` follow Martin's function-source URL "
            "contract, `/{function}/{z}/{x}/{y}` with the query string passed "
            "as `query_params`; the same contract is served by Martin or by the "
            "API's `/tiles` router (chosen by `REIMAGINA_TILE_LIVE_BASE_URL`), "
            "and clients take the URLs from `GET /tile-sources`.",
            "",
        ]

        for schema in SCHEMAS:
            rels = _rows(
                cur,
                """
                select c.relname, c.relkind
                from pg_class c join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = %s and c.relkind in ('r', 'p', 'v', 'm')
                order by c.relname
                """,
                (schema,),
            )
            functions = _rows(
                cur,
                """
                select p.proname, pg_get_function_identity_arguments(p.oid),
                       pg_get_function_result(p.oid), l.lanname,
                       coalesce(obj_description(p.oid, 'pg_proc'), '')
                from pg_proc p
                join pg_namespace n on n.oid = p.pronamespace
                join pg_language l on l.oid = p.prolang
                where n.nspname = %s
                order by p.proname, 2
                """,
                (schema,),
            )
            if not rels and not functions:
                continue
            base = [r for r, k in rels if k in ("r", "p")]
            views = [r for r, k in rels if k == "v"]
            matviews = [r for r, k in rels if k == "m"]
            out += [
                f"### `{schema}`",
                "",
                SCHEMA_ROLES.get(schema, ""),
                "",
                f"{len(base)} table(s), {len(views)} view(s), "
                f"{len(matviews)} materialized view(s), "
                f"{len(functions)} function(s).",
                "",
            ]

            for table in base:
                out += _table_detail(cur, schema, table)

            for title, names in (
                ("Views", views),
                ("Materialized views", matviews),
            ):
                if not names:
                    continue
                out += [f"#### {title} in `{schema}`", ""]
                for name in names:
                    listed = ", ".join(
                        f"`{c}` {t}" for c, t, *_ in _columns(cur, schema, name)
                    )
                    out += [f"- **`{schema}.{name}`** — {listed}", ""]
                    out += _indexes(cur, schema, name)

            if functions:
                out += [f"#### Functions in `{schema}`", ""]
                for name, fargs, result, lang, comment in functions:
                    purpose = _first_paragraph(comment) or FUNCTION_PURPOSES.get(
                        name, ""
                    )
                    out += [
                        f"- **`{schema}.{name}({fargs})`** → {result} "
                        f"({lang}). {purpose}".rstrip(),
                        "",
                    ]

            triggers = _rows(
                cur,
                """
                select t.tgname, c.relname, pg_get_triggerdef(t.oid)
                from pg_trigger t
                join pg_class c on c.oid = t.tgrelid
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = %s and not t.tgisinternal
                order by c.relname, t.tgname
                """,
                (schema,),
            )
            if triggers:
                out += [f"#### Triggers in `{schema}`", ""]
                out += [
                    f"- `{name}` on `{schema}.{table}` — {_escape(definition)}"
                    for name, table, definition in triggers
                ]
                out.append("")

        out += _geometry_section(cur)
    return out


def _columns(cur: Any, schema: str, rel: str) -> list[tuple]:
    """Name, type, nullable, default — from the catalog, matviews included.

    ``information_schema.columns`` would print ``USER-DEFINED`` for every
    geometry and ``ARRAY`` for every array, and it does not list
    materialized views at all.
    """
    return _rows(
        cur,
        """
        select a.attname, format_type(a.atttypid, a.atttypmod),
               not a.attnotnull,
               case when a.attgenerated = 's'
                    then 'generated: ' || pg_get_expr(d.adbin, d.adrelid)
                    else coalesce(pg_get_expr(d.adbin, d.adrelid), '') end
        from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
        where n.nspname = %s and c.relname = %s
          and a.attnum > 0 and not a.attisdropped
        order by a.attnum
        """,
        (schema, rel),
    )


def _indexes(cur: Any, schema: str, rel: str) -> list[str]:
    indexes = _rows(
        cur,
        "select indexname, indexdef from pg_indexes "
        "where schemaname = %s and tablename = %s order by indexname",
        (schema, rel),
    )
    if not indexes:
        return []
    return [
        "Indexes:",
        "",
        *(f"- `{name}` — {_escape(definition)}" for name, definition in indexes),
        "",
    ]


def _table_detail(cur: Any, schema: str, table: str) -> list[str]:
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
    rls = _rows(
        cur,
        """
        select c.relrowsecurity, c.relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = %s and c.relname = %s
        """,
        (schema, table),
    )[0]
    policies = _rows(
        cur,
        """
        select policyname, cmd, array_to_string(roles, ', '),
               coalesce(qual, ''), coalesce(with_check, '')
        from pg_policies where schemaname = %s and tablename = %s
        order by policyname
        """,
        (schema, table),
    )

    out = [
        f"#### `{schema}.{table}`",
        "",
        "| Column | Type | Null | Default |",
        "|---|---|---|---|",
    ]
    for name, dtype, nullable, default in _columns(cur, schema, table):
        out.append(
            f"| `{name}` | {dtype} | {'yes' if nullable else 'no'} "
            f"| {_escape(default) or '—'} |"
        )
    out.append("")
    if constraints:
        out += ["Constraints:", ""]
        out += [
            f"- `{name}` — {_escape(definition)}" for name, definition in constraints
        ]
        out.append("")
    out += _indexes(cur, schema, table)
    if rls[0]:
        forced = "forced" if rls[1] else "not forced, so the table owner bypasses it"
        out += [f"Row-level security: enabled ({forced}). Policies:", ""]
        for name, cmd, roles, using, check in policies:
            line = f"- `{name}` — {cmd} to {roles}, using `{_escape(using)}`"
            if check:
                line += f", with check `{_escape(check)}`"
            out.append(line)
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


# ── API ─────────────────────────────────────────────────────────────────────


def schema_type(s: dict[str, Any]) -> str:
    """A JSON Schema fragment as a short, readable type."""
    if "$ref" in s:
        return s["$ref"].rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf"):
        if key in s:
            parts = [schema_type(p) for p in s[key]]
            nullable = "null" in parts
            text = " or ".join(p for p in parts if p != "null")
            return f"{text} or null" if nullable else text
    if "allOf" in s and len(s["allOf"]) == 1:
        return schema_type(s["allOf"][0])
    if "enum" in s:
        return "one of " + ", ".join(f"`{v}`" for v in s["enum"])
    if "const" in s:
        return f"`{s['const']}`"
    kind = s.get("type")
    if kind == "array":
        return f"list[{schema_type(s.get('items', {}))}]"
    if kind == "object" and isinstance(s.get("additionalProperties"), dict):
        return f"dict[string, {schema_type(s['additionalProperties'])}]"
    if kind is None:
        return "any"
    return f"{kind} ({s['format']})" if "format" in s else str(kind)


def _cell(text: str) -> str:
    return _escape(text) if text else "—"


def _operation(method: str, path: str, op: dict[str, Any]) -> list[str]:
    out = [f"##### `{method.upper()} {path}`", ""]
    description = _first_paragraph(op.get("description")) or op.get("summary", "")
    out += [description, ""]

    params = op.get("parameters") or []
    if params:
        out += [
            # Three columns, not five: the PDF lays tables out at equal
            # column widths, and descriptions need the room.
            "| Parameter | Type | Description |",
            "|---|---|---|",
        ]
        for p in params:
            schema = p.get("schema", {})
            kind = schema_type(schema)
            if "default" in schema:
                kind += f", default `{json.dumps(schema['default'])}`"
            out.append(
                f"| `{p['name']}` ({p['in']}, "
                f"{'required' if p.get('required') else 'optional'}) "
                f"| {_cell(kind)} "
                f"| {_cell(_first_paragraph(p.get('description')))} |"
            )
        out.append("")

    facts = []
    body = op.get("requestBody")
    if body:
        media, content = next(iter(body["content"].items()))
        req = "required" if body.get("required") else "optional"
        facts.append(
            f"Request body ({media}, {req}): `{schema_type(content.get('schema', {}))}`"
        )
    for code, resp in sorted(op.get("responses", {}).items()):
        if code == "422" and resp.get("description") == "Validation Error":
            continue  # FastAPI's own, on every operation with inputs
        content = resp.get("content") or {}
        json_schema = (content.get("application/json") or {}).get("schema")
        if json_schema:
            shape = f"`{schema_type(json_schema)}`"
        elif content:
            shape = ", ".join(f"`{m}`" for m in content)
        else:
            shape = "no declared body"
        note = resp.get("description", "")
        if note and note != "Successful Response":
            shape += f" — {note}"
        facts.append(f"Response {code}: {shape}")
    out += [f"- {_escape(f)}" for f in facts]
    out.append("")
    return out


def _component(name: str, s: dict[str, Any]) -> list[str]:
    out = [f"#### `{name}`", ""]
    if s.get("description"):
        out += [_first_paragraph(s["description"]), ""]
    props = s.get("properties")
    if not props:
        out += [f"Type: {schema_type(s)}.", ""]
        return out
    required = set(s.get("required", []))
    out += [
        "| Field | Type | Description |",
        "|---|---|---|",
    ]
    for field, fs in props.items():
        kind = schema_type(fs)
        if "default" in fs:
            kind += f", default `{json.dumps(fs['default'])}`"
        out.append(
            f"| `{field}` ({'required' if field in required else 'optional'}) "
            f"| {_cell(kind)} | {_cell(_first_paragraph(fs.get('description')))} |"
        )
    out.append("")
    return out


def _referenced(node: Any, schemas: dict[str, Any], seen: set[str]) -> set[str]:
    """Every component schema reachable from ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref:
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                _referenced(schemas[name], schemas, seen)
        for value in node.values():
            _referenced(value, schemas, seen)
    elif isinstance(node, list):
        for value in node:
            _referenced(value, schemas, seen)
    return seen


def api_section() -> list[str]:
    # `apps` is at the repo root; the backend namespace is under packages/py.
    for entry in (ROOT, ROOT / "packages" / "py"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    from apps.api.app import app  # noqa: PLC0415 — import cost only when generating

    spec = app.openapi()
    paths: dict[str, Any] = spec.get("paths", {})
    components: dict[str, Any] = spec.get("components", {}).get("schemas", {})

    grouped: dict[str, list[str]] = {}
    own_ops: list[Any] = []
    mounted: list[str] = []
    for path, operations in sorted(paths.items()):
        for method, operation in sorted(operations.items()):
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            # `/cog/*` is titiler's own router, mounted in-process. It is part
            # of the delivered surface but it is not code written for this
            # project, and a reader counting "the API" should not be handed
            # its paths as though they were ours, so they are only indexed.
            if path.startswith("/cog"):
                params = operation.get("parameters") or []
                names = ", ".join(f"`{p['name']}`" for p in params) or "—"
                body = "yes" if operation.get("requestBody") else "no"
                summary = (operation.get("summary") or "").strip() or "—"
                mounted.append(
                    f"| `{method.upper()}` | `{path}` | {summary} | {names} | {body} |"
                )
            else:
                own_ops.append(operation)
                tag = str((operation.get("tags") or ["untagged"])[0])
                grouped.setdefault(tag, []).extend(_operation(method, path, operation))

    own = len(own_ops)
    own_schemas = sorted(_referenced(own_ops, components, set()))
    out = [
        "## API",
        "",
        f"Taken from the application's own OpenAPI document "
        f"(`{spec['info']['title']}` {spec['info'].get('version', '')}, "
        f"OpenAPI {spec.get('openapi', '')}), served at `/openapi.json` and "
        "rendered at `/docs`.",
        "",
        f"The API exposes **{own + len(mounted)} operations** in total. "
        f"**{own}** are written for this project; the remaining "
        f"**{len(mounted)}** belong to titiler's raster router, which is "
        "mounted in-process rather than deployed separately, and are listed "
        "apart for that reason.",
        "",
        "Each project operation below gives its description, its parameters "
        "(where they travel, their type and whether they are required), its "
        "request body and its responses, naming the schema of each JSON body; "
        "the schemas themselves follow in "
        "[Schemas](#schemas). Every operation that takes input can also "
        "answer 422 with FastAPI's `HTTPValidationError`; that response is not "
        "repeated per operation.",
        "",
        f"### Project endpoints ({own})",
        "",
    ]
    for tag in sorted(grouped):
        out += [f"#### `{tag}`", "", *grouped[tag]]

    out += [
        "### Schemas",
        "",
        f"The {len(own_schemas)} component schemas the project endpoints "
        "use, directly or nested, with each field's type and whether it is "
        "required. Titiler's own schemas are omitted.",
        "",
    ]
    for name in own_schemas:
        out += _component(name, components[name])

    out += [
        f"### Mounted raster endpoints ({len(mounted)})",
        "",
        "Provided by titiler. Access is restricted by `apps/api/cog_security.py` "
        "to `file://` URLs beneath the configured data root, with symlink "
        "escapes resolved and rejected; titiler's own default would hand any "
        "`url` parameter to GDAL.",
        "",
        "| Method | Path | Summary | Parameters | Request body |",
        "|---|---|---|---|---|",
        *mounted,
        "",
    ]
    return out


# ── Document ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the schema/API reference.")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("REIMAGINA_APP_DSN", ""),
        help="Database migrated to head. Defaults to $REIMAGINA_APP_DSN.",
    )
    parser.add_argument(
        "--date",
        help="Issue date, e.g. '20 November 2026'. Defaults to today.",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        print("No DSN. Set REIMAGINA_APP_DSN or pass --dsn.", file=sys.stderr)
        return 2
    issued = parse_date(args.date)
    author = "Rossano Schifanella"

    document = [
        "---",
        'title: "Database and API reference"',
        f'author: "{author}"',
        f'date: "{issued.day} {issued:%B %Y}"',
        "---",
        "",
        "## Document control",
        "",
        "| | |",
        "|---|---|",
        "| **Deliverable** | Schedule A, Work 3 — Database/API schemas and "
        "documentation |",
        f"| **Version** | {VERSION} |",
        "| **Status** | Issued |",
        "| **Audience** | Technical reviewers, maintainers and operators |",
        "",
        "| Version | Date | Author | Summary |",
        "|---|---|---|---|",
        *(f"| {v} | {when} | {author} | {summary} |" for v, when, summary in REVISIONS),
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
