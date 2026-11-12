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

**The rules of a proposal are not in this file.** ``reimagina.scenarios.proposal``
owns them — the four edits three numbers write, the refusal of a non-positive
or non-finite value, the density precheck, and the unioning of a drawn
FeatureCollection into one area. They are domain policy, and the API router
needs the same ones; a router that imported them from ``apps/cli`` would have
the dependency backwards, and one that re-derived them would bring back the
failure the four-edit rule exists to prevent.

What stays here is dialect. The domain says ``levels``; a person at a
terminal typed ``--levels``, and a sentence that tells them to call
``scenario recompute`` is useful to them and meaningless to an HTTP client.
So each refusal is caught and re-raised with the flag name and the command
attached — see ``_require_positive`` and ``_precheck_ceilings`` below.

**The ceiling precheck is partial, on purpose and not completely.**
``set-attribute`` refuses values that would breach the *density* bound before
it opens a connection — that bound is a property of levels and unit size
alone, so a form can check it without choosing a block
(``ceilings.max_levels_for``). The housing-unit and population bounds scale
with block area and are checked per block by ``materialize_scenario``, which
is where `scenario recompute` meets them. ⚠ ``floors`` has **no** bound in
``ceilings`` at all, though ``urban_form_block_planned_floors`` declares a
``valid_max``: a tall proposal at a large unit size passes every check here
and raises in r016's trigger mid-compute. That gap is recorded in
``tests/unit/test_scenario_cli.py``; it is not closed here, because the place
to close it is ``ceilings.check_ceilings``, where all the other bounds live.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from apps.cli.indicators import evaluate_scenario_cli
from reimagina.data.feature import get_type_id
from reimagina.db import connect
from reimagina.scenarios.authoring import (
    InvalidGeometry,
    ScenarioNotFound,
    add_edit,
    create_scenario,
    materialize_scenario,
    project_of_aoi,
)
from reimagina.scenarios.comparison import compare
from reimagina.scenarios.proposal import (
    CeilingPrecheckFailed,
    ProposalValueError,
    edits_for,
    polygon_wkt,
    precheck_ceilings,
    require_positive,
)

#: Every shipped indicator is block-grain (AGENTS.md), so this is the type a
#: drawn rule applies to unless the caller says otherwise.
DEFAULT_TYPE_CONTEXT = "MEX"
DEFAULT_TYPE_SLUG = "block"


def _require_positive(**values: float) -> None:
    """``require_positive``, with the refusal spelled as a command-line flag.

    The domain names the field (``levels``); the person at the terminal typed
    ``--levels``, and telling them to fix ``levels`` is telling them to go
    looking for it.
    """
    try:
        require_positive(**values)
    except ProposalValueError as exc:
        raise ValueError(
            f"--{exc.field.replace('_', '-')} must be a positive number, "
            f"got {exc.value!r}"
        ) from exc


def _precheck_ceilings(
    levels: float, unit_size: float, people_per_household: float
) -> None:
    """``precheck_ceilings``, with the two flags and the next command named.

    The domain message states the breach and is honest about the two bounds
    it cannot check; what it must not state is *how* to act, because that is
    different on every surface. Here the act is lowering a flag and running a
    command, so this is where those words go.
    """
    try:
        precheck_ceilings(levels, unit_size, people_per_household)
    except CeilingPrecheckFailed as exc:
        raise ValueError(
            f"{exc} Lower --levels or raise --unit-size; "
            f"`scenario recompute` is what materialises."
        ) from exc


def _polygon_wkt(geojson_path: str | Path) -> str:
    """Read the drawn area from disk and hand the parsed GeoJSON to the domain.

    The reading is all that is left here: a request body and a drawn file
    differ in where the bytes come from and in nothing else, so
    ``proposal.polygon_wkt`` owns the FeatureCollection / Feature /
    bare-geometry unioning that Task 9 needs too, and this adds the one thing
    it cannot know — which file the user named.
    """
    raw = json.loads(Path(geojson_path).read_text())
    try:
        return polygon_wkt(raw)
    except InvalidGeometry as exc:
        raise InvalidGeometry(f"{geojson_path}: {exc}") from exc


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

    Passing **both** and having them disagree is refused by ``create_scenario``
    — the AOI must belong to the project — because the schema does not tie the
    two columns together and a scenario bounded by another project's AOI would
    materialise overrides over that project's blocks.
    """
    if project_id is None and aoi_id is None:
        raise ValueError(
            "a scenario belongs to a project: pass --project-id, or --aoi-id "
            "to take the project from the area of interest"
        )
    with contextlib.closing(connect(dsn)) as conn:
        resolved = project_id or project_of_aoi(conn, aoi_id)
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
    _precheck_ceilings(levels, unit_size, people_per_household)
    geometry_wkt = _polygon_wkt(geojson)

    written = []
    with contextlib.closing(connect(dsn)) as conn:
        type_id = get_type_id(conn, type_context, type_slug)
        for edit in edits_for(levels, unit_size, people_per_household):
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
    than nested. It first had to go because ``_print`` masked only
    ``data["dsn"]`` and a nested copy printed the password in full; ``_print``
    now walks the whole payload, so the drop is no longer the thing standing
    between a password and a terminal. It stays because the key is redundant
    — the top level carries the same DSN — and because one masked DSN per
    payload is easier to be sure about than two.
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
        raise ScenarioNotFound(f"no scenario with id {scenario_id}")
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
        raise ScenarioNotFound(f"no scenario with id {scenario_id}")
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
