"""CLI helpers for authoring a scenario: create, draw rules, recompute, compare.

The sub-app is two halves that deliberately read different relations.

**The write half** — ``create``, ``add-rule``, ``update-rule``,
``delete-rule``, ``delete``, ``recompute`` — calls
``reimagina.scenarios.authoring`` directly. Nothing in that module commits, so
each function here owns one transaction and ends it explicitly.

**The read half** — ``list``, ``show`` and ``rules`` — reads
``explore.scenario_v`` (and the rule rows), not ``authoring.list_scenarios``.
The rule: CLI inspection commands read from ``explore``. The view is the
engineer-facing contract, so a column added to it reaches the CLI without an
edit here (the select is ``*``), and a question the CLI can answer is a
question the engineer can also answer in DBeaver with the same statement.

**The same rules as the API, and the same refusals.** A rule writes only the
numbers given (no default coverage), is checked by ``proposal.check_inputs``
and materialised in the transaction that writes it, so a rule the server would
refuse — a ceiling breach on any block, an area that catches no block, or
numbers that are all changes and move no block — is never stored, from either surface. The CLI
writes numbers as values only (it has no unit flags); ``update-rule`` keeps
each number's own unit, set in the page, and both print the rule's units. ``recompute`` submits the materialisation's
footprint, exactly as ``POST /scenarios/{id}/recompute`` does, scoped to the
AOI the materialisation read under its lock.

What stays here is dialect. The domain says ``levels``; a person at a
terminal typed ``--levels``, and a sentence that tells them to call
``scenario recompute`` is useful to them and meaningless to an HTTP client.
So each refusal is caught and re-raised with the flag name attached — see
``_flags`` below.
"""

from __future__ import annotations

from apps.cli import UserError

import contextlib
import json
from dataclasses import asdict
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from apps.cli.indicators import evaluate_scenario_cli
from reimagina.data.feature import get_type_id
from reimagina.db import connect
from reimagina.scenarios.authoring import (
    InvalidGeometry,
    ScenarioNotFound,
    _canonical_id,
    add_rule as add_rule_row,
    create_scenario,
    delete_rule as delete_rule_row,
    delete_scenario,
    get_scenario,
    materialize_scenario,
    project_of_aoi,
    update_rule as update_rule_row,
)
from reimagina.scenarios.comparison import compare
from reimagina.scenarios.proposal import (
    CeilingPrecheckFailed,
    RULE_INPUTS,
    ProposalValueError,
    polygon_wkt,
)

#: Every shipped indicator is block-grain, so this is the type a
#: drawn rule applies to unless the caller says otherwise.
DEFAULT_TYPE_CONTEXT = "MEX"
DEFAULT_TYPE_SLUG = "block"


@contextlib.contextmanager
def _flags() -> Iterator[None]:
    """The domain's number refusals, each spelled as its command-line flag."""
    try:
        yield
    except ProposalValueError as exc:
        raise UserError(
            f"--{exc.field.replace('_', '-')} must {exc.must}, got {exc.value!r}"
        ) from exc
    except CeilingPrecheckFailed as exc:
        raise UserError(
            f"{exc} Lower --levels (or, for the density ceiling, raise --unit-size)."
        ) from exc


def _polygon_wkt(geojson_path: str | Path) -> str:
    """Read the drawn area from disk and hand the parsed GeoJSON to the domain."""
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
    aoi_id: str,
    project_id: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a named what-if on an area of interest.

    The AOI is required: a scenario without one can hold rules but never
    materialise them, and nothing can set it later. The project is read off
    the AOI unless given; given and disagreeing is refused by
    ``create_scenario``, because the schema does not tie the two together.
    """
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


def add_rule(
    *,
    dsn: str,
    scenario_id: str,
    geojson: str,
    inputs: dict[str, float | None],
    type_context: str = DEFAULT_TYPE_CONTEXT,
    type_slug: str = DEFAULT_TYPE_SLUG,
) -> dict[str, Any]:
    """Draw one rule and materialise the scenario, in one transaction.

    Only the numbers given are set. A ceiling breach or an area that catches
    no block is refused by the materialisation and rolls the rule back with
    it. ``recompute`` is what then computes the indicators.
    """
    scenario_id = _canonical_id(scenario_id)
    geometry_wkt = _polygon_wkt(geojson)
    with _flags(), contextlib.closing(connect(dsn)) as conn:
        type_id = get_type_id(conn, type_context, type_slug)
        rule, result = add_rule_row(
            conn,
            scenario_id=scenario_id,
            geometry_wkt=geometry_wkt,
            type_id=type_id,
            inputs=inputs,
        )
        conn.commit()
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "rule_id": rule["id"],
        "inputs": rule["inputs"],
        # How each number applies (set, abs, pct): "1" is 1 storey or +1.
        "units": rule["units"],
        "feature_type": f"{type_context}:{type_slug}",
        "geojson": str(geojson),
        "materialized": asdict(result),
    }


def update_rule(
    *,
    dsn: str,
    scenario_id: str,
    rule_id: str,
    inputs: dict[str, float | None],
    unset: Sequence[str] = (),
    geojson: str | None = None,
) -> dict[str, Any]:
    """Change one rule: the numbers given replace its own, each keeping the
    rule's choice for it (=, + amount, + %; a new number is a value), those
    named in ``unset`` are removed, the rest are kept; its area changes if
    given.

    ``merge=True``: a typed command names only what changes, so ``--levels 8``
    alone must not drop the rule's other numbers, and ``--geojson`` alone
    moves the rule. The merge happens in ``authoring.update_rule``, under the
    scenario's lock. The rule keeps its id and its place in "later wins".
    Refused changes roll back, leaving the rule as it was.
    """
    scenario_id = _canonical_id(scenario_id)
    removed = {name.replace("-", "_") for name in unset}
    unknown = sorted(removed - set(RULE_INPUTS))
    if unknown:
        flags = ", ".join(name.replace("_", "-") for name in RULE_INPUTS)
        raise UserError(f"--unset must name one of {flags}, got {', '.join(unknown)}")
    both = sorted(n for n in removed if inputs.get(n) is not None)
    if both:
        raise UserError(f"--{both[0].replace('_', '-')} is both set and unset")
    geometry_wkt = None if geojson is None else _polygon_wkt(geojson)
    with _flags(), contextlib.closing(connect(dsn)) as conn:
        rule, result = update_rule_row(
            conn,
            scenario_id=scenario_id,
            rule_id=rule_id,
            geometry_wkt=geometry_wkt,
            inputs=inputs,
            unset=sorted(removed),
            merge=True,
        )
        conn.commit()
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "rule_id": rule["id"],
        "inputs": rule["inputs"],
        # How each number applies (set, abs, pct): "1" is 1 storey or +1.
        "units": rule["units"],
        "materialized": asdict(result),
    }


def delete_rule(*, dsn: str, scenario_id: str, rule_id: str) -> dict[str, Any]:
    """Delete one rule and materialise what is left, in one transaction."""
    scenario_id = _canonical_id(scenario_id)
    with contextlib.closing(connect(dsn)) as conn:
        _, result = delete_rule_row(conn, scenario_id=scenario_id, rule_id=rule_id)
        conn.commit()
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "rule_id": rule_id,
        "materialized": asdict(result),
    }


def delete(*, dsn: str, scenario_id: str) -> dict[str, Any]:
    """Delete a scenario and every row keyed by it."""
    scenario_id = _canonical_id(scenario_id)
    with contextlib.closing(connect(dsn)) as conn:
        removed = delete_scenario(conn, scenario_id=scenario_id)
        conn.commit()
    return {"dsn": dsn, "scenario_id": scenario_id, "deleted": asdict(removed)}


def recompute(
    *,
    dsn: str,
    scenario_id: str,
    mode: str = "embedded",
    code_version: str = "dev",
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Materialise the scenario's rules, then recompute their footprint.

    **Two transactions, and the boundary is a decision rather than an
    accident.** ``materialize_scenario`` locks the scenario row as its first
    statement and holds it until the caller commits, while the evaluation that
    follows computes indicators over the footprint. Running both under one
    transaction would hold that lock for the whole compute, blocking every
    other writer. So this commits the materialisation, closes the connection,
    and evaluates on its own.

    **The dirty set is the footprint**, as the API sends it: every block a
    current rule overrides, with the attributes involved. Planned from
    freshness instead, the evaluation wrote a scenario row for every block of
    the AOI — 95,290 rows against 15 for a three-block rule. The AOI is the
    one the materialisation read under its lock; an empty footprint (no
    override left) runs nothing.

    ⚠ The evaluation's own ``dsn`` key is dropped from the payload rather
    than nested: the top level carries the same DSN, and one masked DSN per
    payload is easier to be sure about than two.
    """
    scenario_id = _canonical_id(scenario_id)
    with contextlib.closing(connect(dsn)) as conn:
        result = materialize_scenario(conn, scenario_id=scenario_id)
        conn.commit()

    if result.footprint_features:
        evaluation = evaluate_scenario_cli(
            dsn=dsn,
            changed_attributes=list(result.footprint_attributes),
            edited_features=list(result.footprint_features),
            scenario_id=scenario_id,
            aoi_id=result.aoi_id,
            code_version=code_version,
            mode=mode,
            timeout_s=timeout_s,
        )
    else:
        evaluation = {"status": "noop", "run_id": None}
    return {
        "dsn": dsn,
        "scenario_id": scenario_id,
        "materialized": asdict(result),
        "evaluation": {k: v for k, v in evaluation.items() if k != "dsn"},
    }


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

    ``rule_count`` and ``materialized_at`` together are the state between
    runs: rules drawn, and whether a materialisation has seen them.
    """
    scenario_id = _canonical_id(scenario_id)
    with contextlib.closing(connect(dsn)) as conn:
        rows = _select(
            conn,
            "SELECT * FROM explore.scenario_v WHERE id = %(scenario_id)s",
            {"scenario_id": scenario_id},
        )
    if not rows:
        raise ScenarioNotFound(f"no scenario with id {scenario_id}")
    return {"dsn": dsn, "scenario": rows[0]}


def rules(*, dsn: str, scenario_id: str) -> dict[str, Any]:
    """One scenario's rules, in "later wins" order, with their ids."""
    scenario_id = _canonical_id(scenario_id)
    with contextlib.closing(connect(dsn)) as conn:
        found = get_scenario(conn, scenario_id)
    if found is None:
        raise ScenarioNotFound(f"no scenario with id {scenario_id}")
    rows = found["rules"]
    return {"dsn": dsn, "scenario_id": scenario_id, "count": len(rows), "rules": rows}


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
    scenario_id = _canonical_id(scenario_id)
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
