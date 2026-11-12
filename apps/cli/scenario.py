"""CLI helpers for authoring a scenario: create, draw, recompute, compare.

The sub-app is two halves that deliberately read different relations.

**The write half** — ``create``, ``set-attribute``, ``recompute`` — calls
``reimagina.scenarios.authoring`` directly. Nothing in that module commits, so
each function here owns one transaction and ends it explicitly.

**The read half** — ``list`` and ``show`` — reads ``explore.scenario_v``, not
``authoring.list_scenarios``/``get_scenario``. AGENTS.md rule 4: CLI
inspection commands read from ``explore``. That is not a stylistic preference
— the view is the engineer-facing contract, so a column added to it reaches
the CLI without an edit here (the select is ``*``), and a question the CLI can
answer is a question the engineer can also answer in DBeaver with the same
statement. The two Python readers exist for the API, which needs a scenario's
edits; the view carries the counts, which is what inspection asks for and what
the spec names (`SCENARIO_AUTHORING.md`: "`scenario show` reports an edit
count and a materialised-at timestamp").

**The ceiling precheck is partial, on purpose and not completely.**
``set-attribute`` refuses values that would breach the *density* bound before
it opens a connection — that bound is a property of levels and unit size
alone, so a form can check it without choosing a block
(``ceilings.max_levels_for``). The housing-unit and population bounds scale
with block area and are checked per block by ``materialize_scenario``, which
is where `scenario recompute` meets them. ⚠ ``floors`` has **no** bound in
``ceilings`` at all, though ``urban_form_block_planned_floors`` declares
``valid_max=200``: a tall proposal at a large unit size passes every check
here and raises in r016's trigger mid-compute. That gap is recorded in
``tests/unit/test_scenario_cli.py``; it is not closed here, because the place
to close it is ``ceilings.check_ceilings``, where all the other bounds live.
"""

from __future__ import annotations

import contextlib
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from apps.cli.indicators import evaluate_scenario_cli
from reimagina.data.feature import get_type_id
from reimagina.db import connect
from reimagina.scenarios.authoring import (
    add_edit,
    create_scenario,
    materialize_scenario,
)
from reimagina.scenarios.ceilings import (
    BlockInputs,
    CeilingBreach,
    check_ceilings,
    max_levels_for,
)
from reimagina.scenarios.comparison import compare

#: Every shipped indicator is block-grain (AGENTS.md), so this is the type a
#: drawn rule applies to unless the caller says otherwise.
DEFAULT_TYPE_CONTEXT = "MEX"
DEFAULT_TYPE_SLUG = "block"

#: The ``feature_id`` the precheck gives its one synthetic block. No feature
#: has been selected yet — that happens at materialisation — so the string is
#: never shown; it exists because ``BlockInputs`` requires one.
_PROPOSAL = "(proposal)"


class CeilingPrecheckFailed(ValueError):
    """Values the indicators could not store, refused before anything is read.

    Deliberately *not* ``authoring.CeilingExceeded``. That exception names a
    feature per breach because materialisation has blocks in hand; this
    refusal happens with no block chosen, and a message reading "feature
    (proposal) would compute…" would be worse than one that names the two
    numbers the user typed. The breaches are carried on the exception so a
    caller that wants the structured form has it.
    """

    def __init__(
        self, breaches: list[CeilingBreach], *, levels: float, unit_size: float
    ) -> None:
        self.breaches = breaches
        worst = breaches[0]
        allowed = math.floor(max_levels_for(unit_size))
        super().__init__(
            f"{levels:g} storeys at {unit_size:g} m² per dwelling computes "
            f"{worst.would_be:.1f} for {worst.indicator_slug}, whose valid_max "
            f"is {worst.bound:.1f}. At {unit_size:g} m² per dwelling the "
            f"ceiling allows {allowed:d} storeys; a larger --unit-size raises "
            f"that in proportion. The per-block bounds (housing units, "
            f"population) depend on each block's area and are checked by "
            f"`scenario recompute`."
        )


def _edits_for(
    levels: float, unit_size: float, people_per_household: float
) -> list[dict[str, Any]]:
    """The four edits one proposal writes, in the order they are written.

    Three numbers, four rules: ``--levels`` sets both ``floors`` and
    ``num_levels``, which are the same quantity under two names
    (``seed_demo_attributes.py``). Writing one and not the other is the
    failure this function exists to prevent — ``urban_form_block_planned_floors``
    reads ``floors`` while the edificability family reads ``num_levels``, so a
    half-written proposal makes the map contradict itself: planned floors stay
    at the baseline while density moves.

    All three are required of the caller (not defaulted) because
    ``edificability.py`` skips any feature missing any of the three, and a
    scenario that silently computed nothing is the hardest failure to see.
    """
    return [
        {"attribute_key": "floors", "value_numeric": float(levels)},
        {"attribute_key": "num_levels", "value_numeric": float(levels)},
        {"attribute_key": "unit_size", "value_numeric": float(unit_size)},
        {
            "attribute_key": "people_per_household",
            "value_numeric": float(people_per_household),
        },
    ]


def _require_positive(**values: float) -> None:
    """Refuse zero, negative and non-finite inputs.

    ``check_ceilings`` documents that degenerate inputs "yield 0 in the
    kernels and so cannot breach; a caller validates those separately" — this
    is that caller. Nothing downstream would refuse them: ``value_numeric`` is
    a plain ``double precision``, so ``--levels -3`` and ``--levels nan`` both
    reach ``scenario_override`` and then the compute.
    """
    for name, value in values.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"--{name.replace('_', '-')} must be a positive number, got {value!r}"
            )


def precheck_ceilings(
    levels: float, unit_size: float, people_per_household: float
) -> None:
    """Refuse a proposal the density bound already rules out.

    One synthetic block with ``area_m2=0``: no block has been chosen yet, so
    only the bound that is independent of area can speak, and passing a real
    or maximum area here would describe a block that does not exist — the
    reasoning ``ceilings`` gives for checking per block rather than per
    selection. ``check_ceilings`` rather than a local comparison so the bound
    comes from the catalog at call time, through the one kernel that
    materialisation also uses.
    """
    breaches = check_ceilings(
        [
            BlockInputs(
                feature_id=_PROPOSAL,
                levels=levels,
                unit_size=unit_size,
                people_per_household=people_per_household,
                area_m2=0.0,
            )
        ]
    )
    if breaches:
        raise CeilingPrecheckFailed(breaches, levels=levels, unit_size=unit_size)


def _polygon_wkt(geojson_path: str | Path) -> str:
    """The drawn area, as WKT, from a GeoJSON file a drawing tool wrote.

    Accepts a FeatureCollection, a Feature or a bare geometry, and unions
    several parts into one area: a selection made with two strokes is one
    rule, not two. Validity is left to ``add_edit``, which repairs with
    ``ST_MakeValid`` on write — the one thing refused here is input with no
    polygonal part at all, because the error PostGIS gives for it arrives
    after a connection and a savepoint.
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    raw = json.loads(Path(geojson_path).read_text())
    kind = raw.get("type")
    if kind == "FeatureCollection":
        geometries = [f.get("geometry") for f in raw.get("features", [])]
    elif kind == "Feature":
        geometries = [raw.get("geometry")]
    else:
        geometries = [raw]
    parts = [shape(g) for g in geometries if g]
    polygons = [p for p in parts if not p.is_empty and p.geom_type.endswith("Polygon")]
    if not polygons:
        raise ValueError(
            f"{geojson_path} carries no polygon: a scenario edit is an area, "
            f"and a point or a line selects nothing"
        )
    return str(unary_union(polygons).wkt)


# ── write paths ─────────────────────────────────────────────────────────


def create(
    *,
    dsn: str,
    name: str,
    project_id: str | None = None,
    aoi_id: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a named what-if, deriving its project from its AOI if need be.

    ``app_core.scenario.project_id`` is NOT NULL and ``aoi_id`` is not, so the
    project cannot simply be read off the AOI in general (r048). It can be
    whenever an AOI *is* given, and the spec's own example passes only
    ``--name`` and ``--aoi-id``; deriving it there is what makes that command
    line work, and the alternative is asking the user for a uuid the database
    already knows.
    """
    if project_id is None and aoi_id is None:
        raise ValueError(
            "a scenario belongs to a project: pass --project-id, or --aoi-id "
            "to take the project from the area of interest"
        )
    with contextlib.closing(connect(dsn)) as conn:
        resolved = project_id or _project_of_aoi(conn, aoi_id)
        scenario_id = create_scenario(
            conn,
            project_id=resolved,
            name=name,
            aoi_id=aoi_id,
            description=description,
        )
        conn.commit()
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "project_id": resolved,
        "aoi_id": aoi_id,
        "name": name,
    }


def _project_of_aoi(conn: Any, aoi_id: str | None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id::text FROM curated_geo.area_of_interest WHERE id = %s",
            (aoi_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no area_of_interest with id {aoi_id}")
    return str(row[0])


def set_attribute(
    *,
    dsn: str,
    scenario_id: str,
    geojson: str,
    levels: float,
    unit_size: float,
    people_per_household: float,
    type_context: str = DEFAULT_TYPE_CONTEXT,
    type_slug: str = DEFAULT_TYPE_SLUG,
) -> dict[str, Any]:
    """Draw one area and write the four edits of one proposal over it.

    The refusals come first and in this order — values, then ceiling, then
    geometry — so a proposal that cannot be stored costs no connection and
    leaves no partial rule behind. The four writes then share one
    transaction: three edits of four is not a proposal, it is a block the
    edificability family skips.
    """
    _require_positive(
        levels=levels, unit_size=unit_size, people_per_household=people_per_household
    )
    precheck_ceilings(levels, unit_size, people_per_household)
    geometry_wkt = _polygon_wkt(geojson)

    written = []
    with contextlib.closing(connect(dsn)) as conn:
        type_id = get_type_id(conn, type_context, type_slug)
        for edit in _edits_for(levels, unit_size, people_per_household):
            edit_id = add_edit(
                conn,
                scenario_id=scenario_id,
                geometry_wkt=geometry_wkt,
                type_id=type_id,
                attribute_key=edit["attribute_key"],
                value_numeric=edit["value_numeric"],
            )
            written.append({"edit_id": edit_id, **edit})
        conn.commit()
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "feature_type": f"{type_context}:{type_slug}",
        "geojson": str(geojson),
        "edits": written,
    }


def recompute(
    *,
    dsn: str,
    scenario_id: str,
    aoi_id: str | None = None,
    mode: str = "embedded",
    code_version: str = "dev",
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Materialise the scenario's edits, then recompute what they invalidated.

    **Two transactions, and the boundary is a decision rather than an
    accident.** ``materialize_scenario`` takes ``FOR UPDATE`` on the scenario
    row as its first statement and holds it until the caller commits, while
    the evaluation that follows computes indicators over every block of the
    AOI — tens of thousands of them on the Mexicali pilot. Running both under
    one transaction would hold a write lock on the scenario for the whole
    compute, blocking every other writer: a second ``set-attribute``, a
    delete, the API's own recompute. So this commits the materialisation,
    closes the connection, and evaluates on its own.

    The state in between — overrides current, results not yet recomputed — is
    one the platform already models: it is exactly what ``asset_freshness_v``
    calls stale, and what a crash between the two steps would leave anyway.
    The next ``recompute`` converges from it, because materialisation is
    idempotent and freshness is derived rather than remembered.

    **The dirty set is left empty on purpose.** Materialisation's
    ``publish_scenario_version`` has just moved the version of every override
    asset this scenario touches, so ``plan_run`` with no attributes and no
    features resolves the work from freshness at this scenario's partition —
    which also covers an attribute whose *last* edit was removed, something a
    list of the surviving edits' keys could not name.

    ⚠ The evaluation's own ``dsn`` key is dropped from the payload rather
    than nested. ``_print`` masks the password of ``data["dsn"]`` and only
    that one — it does not walk the structure — so a nested copy is printed
    in full, which is how a password reaches a terminal and a log. It is also
    redundant: the top level carries the same DSN, masked.
    """
    with contextlib.closing(connect(dsn)) as conn:
        scope = aoi_id if aoi_id is not None else _scenario_aoi(conn, scenario_id)
        result = materialize_scenario(conn, scenario_id=scenario_id)
        conn.commit()

    evaluation = evaluate_scenario_cli(
        dsn=dsn,
        changed_attributes=[],
        edited_features=[],
        scenario_id=scenario_id,
        aoi_id=scope,
        code_version=code_version,
        mode=mode,
        timeout_s=timeout_s,
    )
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "materialized": asdict(result),
        "evaluation": {k: v for k, v in evaluation.items() if k != "dsn"},
    }


def _scenario_aoi(conn: Any, scenario_id: str) -> str | None:
    """The scenario's AOI, read from the inspection view the CLI already uses.

    Null is returned rather than refused: ``materialize_scenario`` raises
    ``ScenarioHasNoAOI`` for it two statements later, with the explanation,
    and duplicating that refusal here would mean two messages for one state.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT aoi_id::text FROM explore.scenario_v WHERE id = %s", (scenario_id,)
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no scenario with id {scenario_id}")
    return None if row[0] is None else str(row[0])


# ── read paths: explore.scenario_v ──────────────────────────────────────


def list_scenarios(
    *, dsn: str, project_id: str | None = None, aoi_id: str | None = None
) -> dict[str, Any]:
    """Every scenario, newest first, with its edit and override counts."""
    with contextlib.closing(connect(dsn)) as conn:
        rows = _select(
            conn,
            """
            SELECT * FROM explore.scenario_v
             WHERE (%(project_id)s::uuid IS NULL
                    OR project_id = %(project_id)s::uuid)
               AND (%(aoi_id)s::uuid IS NULL OR aoi_id = %(aoi_id)s::uuid)
             ORDER BY created_at DESC, name
            """,
            {"project_id": project_id, "aoi_id": aoi_id},
        )
    return {"dsn": dsn, "count": len(rows), "scenarios": rows}


def show(*, dsn: str, scenario_id: str) -> dict[str, Any]:
    """One scenario's row of the inspection view.

    ``edit_count`` and ``materialized_at`` together are the state between
    runs: edits drawn, and whether a materialisation has seen them.
    """
    with contextlib.closing(connect(dsn)) as conn:
        rows = _select(
            conn,
            "SELECT * FROM explore.scenario_v WHERE id = %(scenario_id)s",
            {"scenario_id": scenario_id},
        )
    if not rows:
        raise LookupError(f"no scenario with id {scenario_id}")
    return {"dsn": dsn, "scenario": rows[0]}


def _select(conn: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows as dicts, keyed by whatever columns the view returned.

    ``SELECT *`` and ``cur.description``, so a column added to
    ``explore.scenario_v`` appears in the CLI's output without an edit here.
    The view is the contract; this function is a window onto it.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


# ── comparison ──────────────────────────────────────────────────────────


def compare_to_baseline(
    *,
    dsn: str,
    scenario_id: str,
    indicator_slug: str,
    aoi_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """What one indicator does under the scenario against the baseline.

    ``limit`` truncates the printed rows and never the counts: a real AOI has
    19,058 blocks and a terminal is not where you read them one by one, but
    ``row_count`` and ``summary`` must still describe all of them, or the
    header disagrees with the table for the same reason
    ``ComparisonResult.summary`` is counted off its own rows. ``--limit 0``
    prints every row, for a caller piping into ``jq``.
    """
    with contextlib.closing(connect(dsn)) as conn:
        result = compare(
            conn,
            scenario_id=scenario_id,
            indicator_slug=indicator_slug,
            aoi_id=aoi_id,
        )
    rows = result.rows if limit <= 0 else result.rows[:limit]
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "indicator_slug": indicator_slug,
        "aoi_id": aoi_id,
        "summary": result.summary,
        "row_count": len(result.rows),
        "rows": rows,
        "rows_truncated": len(rows) < len(result.rows),
    }
