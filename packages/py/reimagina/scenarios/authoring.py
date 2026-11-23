"""Scenario and rule authoring — the named what-if and the areas drawn over it.

A scenario is ``app_core.scenario``; each rule in it is a row of
``curated_geo.scenario_rule``: an area and the numbers a planner set over it
(r055). The CRUD half of this module records that intent.
``materialize_scenario`` is the other half: it resolves the rules into the
per-feature rows of ``curated_geo.scenario_override``, which the asset graph
already partitions by scenario, so the ordinary ``evaluate-scenario`` path then
recomputes exactly the affected indicators. It is the only writer of
``materialized_at``.

A rule's ``ordinal`` is its place in "later wins". It is assigned when the rule
is created and never changes after: editing a rule's numbers or moving it keeps
its place. ``add_rule``, ``update_rule`` and ``delete_rule`` each write the
rule and materialise the scenario in the caller's one transaction
(``materialize_for_rule``): a rule the materialisation refuses is then never
stored, so no client has to undo it.

``delete_scenario`` undoes both halves. Overrides, results, job runs and AOI
summaries cascade from the scenario row (r055); versions and materialisations
are keyed by the partition string and are deleted by hand.

None of them commits. The caller owns the transaction, as with
``overrides.py``; an API route opens its own committed connection. That matters
most for materialisation and deletion, which are several statements that are
only correct together: a refusal halfway through must leave no overrides
behind, and no half-deleted scenario.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg2.errors

from reimagina.assets.keys import partition_key
from reimagina.scenarios.ceilings import (
    FLOORS,
    BlockInputs,
    CeilingBreach,
    check_ceilings,
)
from reimagina.scenarios.overrides import (
    _require_postgres,
    apply_overrides,
    publish_scenario_version,
)


class ScenarioNotFound(LookupError):
    """An id or slug the scenario surface was given matches nothing.

    A scenario, an area of interest, or an indicator the catalog does not
    carry. One class rather than three because the answer is the same in
    every case and on every surface: the thing you named is not there.

    It exists so a caller can catch *this* and not ``LookupError``. The broad
    class also catches the ``KeyError``/``IndexError`` of a genuine defect
    anywhere under ``authoring`` or ``comparison``, and a surface that treats
    those as user error reports a bug as a refusal the user caused —
    ``error: 'some_key'`` with exit 2 on the CLI, a 404 from the API. It
    subclasses ``LookupError`` so that every caller written against the old
    contract, including ``pytest.raises(LookupError)``, keeps working.
    """


class DuplicateScenarioName(Exception):
    """A scenario with this name already exists in the project."""


class InvalidGeometry(Exception):
    """The drawn area is not, and cannot be repaired into, a polygon."""


class ScenarioHasNoAOI(Exception):
    """Materialisation was asked for a scenario with no area of interest.

    ``scenario.aoi_id`` is ``ON DELETE SET NULL`` (r048), so this is reachable
    without anyone editing the scenario: delete the AOI and the column empties
    itself. Treating null as "no scope" rather than "every scope" is the whole
    reason this is an exception. Materialisation selects features by a drawn
    area, and the AOI is the only other bound on that selection — without it a
    rule drawn over three blocks in Mexicali would silently expand to every
    block of that type in the database, including another project's.
    """


class CeilingExceeded(Exception):
    """One or more blocks would compute a value the indicators cannot store.

    Carries every ``CeilingBreach``, not just the first, because the user's
    next action is to lower a number and they need to know how far — and which
    district. ``curated_geo.indicator_result``'s r016 trigger RAISES rather
    than clamps, so the alternative to refusing here is a plpgsql exception
    partway through a city-scale run, naming a row id and nothing a planner
    could act on.
    """

    def __init__(
        self,
        breaches: list[CeilingBreach],
        *,
        rules: dict[str, tuple[tuple[str, int], ...]] | None = None,
        allowed_levels: dict[tuple[str, str], int] | None = None,
        advice: str | None = None,
    ) -> None:
        self.breaches = breaches
        #: Per feature, the rules whose numbers the block is computed with, as
        #: ``(rule id, number)`` in "later wins" order — the number being the
        #: 1-based "Rule N" a planner sees.
        self.rules = rules or {}
        #: Per (feature, indicator), the most storeys that block allows with
        #: its other numbers as they are — what a planner would type instead.
        self.allowed_levels = allowed_levels or {}
        # Plain words for the planner: which rule, and what would fit. The
        # block ids, slugs and bounds travel in the structured fields.
        if advice is None:
            # No rule-aware advice: the first breach's rules and the storeys
            # that fit it, as a set rule would be told.
            worst = breaches[0]
            named = ", ".join(str(n) for _, n in self.rules_of(worst))
            most = self.allowed_levels.get((worst.feature_id, worst.indicator_slug))
            n = len({b.feature_id for b in breaches})
            where = "this block" if n == 1 else f"{n} blocks"
            advice = (
                f"Rule {named} is too much for {where}: at most {most} storeys fit."
                if named and most is not None
                else "Lower the storeys or raise the m² per home in this area."
            )
        super().__init__(advice)

    def rules_of(self, breach: CeilingBreach) -> tuple[tuple[str, int], ...]:
        return self.rules.get(breach.feature_id, ())

    @property
    def feature_ids(self) -> tuple[str, ...]:
        """Every offending feature, sorted and de-duplicated."""
        return tuple(sorted({b.feature_id for b in self.breaches}))


@dataclass(frozen=True)
class MaterializeResult:
    """What one materialisation did.

    ``matched_per_rule`` is per *rule*, keyed by its id, and counts what that
    rule's area caught inside the AOI — before any later rule supersedes it.
    That is the number a drawing UI needs ("your shape caught 4 blocks"), and
    it is deliberately not the number of rows the rule ended up owning: a rule
    fully covered by a later one still caught what it caught, and reporting 0
    for it would read as a mis-drawn area.

    ``footprint_features`` and ``footprint_attributes`` are what the recompute
    has to cover: every block a current rule overrides, and the attributes
    involved, removed ones included (a block that lost one number reads its
    own value for it again). A block that lost *every* override is not in it:
    materialisation deleted its scenario rows, so it reads the baseline
    through the overlay, and recomputing it under the scenario would only
    write the baseline back as a scenario row.

    ``aoi_id`` is the AOI the selection was scoped to, read under the
    scenario's lock — the one an evaluation of this footprint must use.
    """

    matched_per_rule: dict[str, int]
    overrides_written: int
    overrides_removed: int
    results_superseded: int
    footprint_features: tuple[str, ...] = ()
    footprint_attributes: tuple[str, ...] = ()
    aoi_id: str | None = None


def create_scenario(
    conn: Any,
    *,
    project_id: str,
    name: str,
    aoi_id: str | None = None,
    description: str | None = None,
) -> str:
    """Create a scenario and return its id.

    ``UNIQUE (project_id, name)`` is what lets a scenario be addressed by name,
    so a clash is a user error and surfaces as ``DuplicateScenarioName`` rather
    than a driver exception a route would turn into a 500. The insert runs in a
    savepoint: a failed statement aborts the whole Postgres transaction, and the
    caller's earlier work in it should not be collateral.

    An ``aoi_id`` is checked against ``project_id`` before the insert — see
    ``_require_aoi_in_project``, which is where the tie between the two lives,
    because the schema does not carry it.
    """
    _require_postgres(conn)
    if aoi_id is not None:
        _require_aoi_in_project(conn, aoi_id=aoi_id, project_id=project_id)
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT create_scenario")
        try:
            cur.execute(
                """
                INSERT INTO app_core.scenario (project_id, aoi_id, name, description)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (project_id, aoi_id, name, description),
            )
        except psycopg2.errors.UniqueViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT create_scenario")
            raise DuplicateScenarioName(
                f"scenario {name!r} already exists in this project"
            ) from exc
        scenario_id = str(cur.fetchone()[0])
        cur.execute("RELEASE SAVEPOINT create_scenario")
        return scenario_id


def project_of_aoi(conn: Any, aoi_id: str | None) -> str:
    """The project an area of interest belongs to.

    Two callers ask this one question: ``scenario create`` derives a project
    from ``--aoi-id`` so a person at a terminal need not look up a second
    uuid, and ``create_scenario`` checks that a project and an AOI handed over
    together agree. A second copy of the query would be a second place for
    those two answers to drift.

    ``None`` matches nothing and is refused exactly like an unknown id, which
    is what lets the CLI pass its optional flag straight through.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id::text FROM curated_geo.area_of_interest WHERE id = %s",
            (aoi_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ScenarioNotFound(f"no area_of_interest with id {aoi_id}")
    return str(row[0])


def _require_aoi_in_project(conn: Any, *, aoi_id: str, project_id: str) -> None:
    """⚠ The tie between a scenario's project and its AOI. It lives here only.

    r048 declares ``aoi_id`` as a plain reference to
    ``curated_geo.area_of_interest(id)`` with nothing linking it to
    ``project_id``, so the schema accepts a scenario in project A bounded by
    project B's AOI. ``materialize_scenario`` scopes its selection by
    ``aoi_id`` alone, so such a scenario resolves overrides over B's features
    — across the boundary ``tenant_isolation`` exists to keep.

    This check is the whole of the fix, and a complete one, because
    ``create_scenario`` is the only writer of the column: nothing updates it
    (the two UPDATEs on this table set ``updated_at`` and ``materialized_at``),
    no surface offers a PUT or a PATCH, and the FK's ``ON DELETE SET NULL`` can
    only empty it. A CHECK cannot express the rule — it spans two tables — and
    a trigger would be the right answer only if an untrusted writer existed.
    RLS is inert (a known, deferred risk: see ``SCENARIOS_LAYER.md``),
    so what this closes is our own client sending a mismatched pair, which is a
    correctness bug rather than a breached boundary.

    ⚠ A new insert path must repeat this call. The schema will not.

    An AOI that exists nowhere is deliberately **not** this refusal: the
    foreign key already rejects it, and the API reads the offending field off
    the constraint name — a better answer than this one could give.
    """
    try:
        owner = project_of_aoi(conn, aoi_id)
    except ScenarioNotFound:
        return
    if owner != project_id:
        raise ScenarioNotFound(
            f"no area_of_interest with id {aoi_id} in project {project_id}"
        )


def list_scenarios(
    conn: Any, *, project_id: str | None = None, aoi_id: str | None = None
) -> list[dict]:
    """Scenarios, newest first, optionally narrowed by project and/or AOI.

    Each row also carries ``rule_count`` and ``latest_run`` — ``{id, status}``
    of the scenario's newest run, or ``None`` — so a summary of every scenario
    costs one query. ``latest_run`` is what tells a failed recompute apart from
    a good one: both set ``materialized_at``.

    ⚠ The run is the envelope in ``orchestration_run``, not a ``job_run`` row.
    ``job_run`` holds one row per indicator *unit*, so its newest row can be a
    unit that succeeded inside a run that failed, and its id is not one
    ``GET /jobs/{run_id}`` knows.

    ⚠ A run still waiting in the queue has no envelope until a worker opens it
    (``dbos_app.s_open_run``), so during that window this names the previous
    run. A caller following the run it just enqueued knows better.
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.project_id, s.aoi_id, s.name, s.description,
                   s.created_at, s.updated_at, s.materialized_at,
                   (SELECT count(*) FROM curated_geo.scenario_rule e
                     WHERE e.scenario_id = s.id) AS rule_count,
                   r.id AS latest_run_id, r.status AS latest_run_status
            FROM app_core.scenario s
            LEFT JOIN LATERAL (
                SELECT id, status FROM jobs_runtime.orchestration_run
                WHERE scenario_id = s.id
                ORDER BY submitted_at DESC, id DESC
                LIMIT 1
            ) r ON true
            WHERE (%(p)s::uuid IS NULL OR s.project_id = %(p)s::uuid)
              AND (%(a)s::uuid IS NULL OR s.aoi_id = %(a)s::uuid)
            ORDER BY s.created_at DESC, s.name
            """,
            {"p": project_id, "a": aoi_id},
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for values in cur.fetchall():
            row = dict(zip(cols, values, strict=True))
            run_id = row.pop("latest_run_id")
            run_status = row.pop("latest_run_status")
            row["rule_count"] = int(row["rule_count"])
            row["latest_run"] = (
                None if run_id is None else {"id": str(run_id), "status": run_status}
            )
            rows.append(_stringify_ids(row))
        return rows


def unfinished_runs(conn: Any, scenario_id: str) -> list[tuple[str, str]]:
    """``(run_id, status)`` of this scenario's runs that have not ended, newest first.

    Only runs a worker has opened (``orchestration_run``): one still waiting in
    the queue has no envelope yet.
    """
    from reimagina.orchestration.port import RUN_TERMINAL_STATES

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, status FROM jobs_runtime.orchestration_run
             WHERE scenario_id = %s AND status <> ALL(%s)
             ORDER BY submitted_at DESC, id DESC
            """,
            (scenario_id, sorted(RUN_TERMINAL_STATES)),
        )
        return [(str(run_id), str(status)) for run_id, status in cur.fetchall()]


def get_scenario(conn: Any, scenario_id: str) -> dict | None:
    """One scenario with its ``rules`` (in ordinal order) and ``override_count``.

    ``materialized_at`` is read, never written, here. ``None`` means the rules
    have not been expanded into overrides yet, which is a different statement
    from "expanded, and there was nothing to do".
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, aoi_id, name, description,
                   created_at, updated_at, materialized_at,
                   (SELECT count(*) FROM curated_geo.scenario_override o
                     WHERE o.scenario_id = s.id) AS override_count
            FROM app_core.scenario s
            WHERE id = %s
            """,
            (scenario_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        scenario = _stringify_ids(dict(zip(cols, row, strict=True)))
    scenario["rules"] = list_rules(conn, scenario_id)
    return scenario


#: A rule as every surface shows it, read straight off ``RETURNING`` or a list.
_RULE_COLUMNS = (
    "id, ordinal, type_id, inputs, mode, units, ST_AsText(geometry) AS geometry_wkt, "
    "created_at, updated_at"
)


def list_rules(conn: Any, scenario_id: str) -> list[dict]:
    """A scenario's rules, in "later wins" order (ordinal)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_RULE_COLUMNS} FROM curated_geo.scenario_rule "
            "WHERE scenario_id = %s ORDER BY ordinal",
            (scenario_id,),
        )
        cols = [d[0] for d in cur.description]
        return [_stringify_ids(dict(zip(cols, r, strict=True))) for r in cur.fetchall()]


def _lock_scenario(
    cur: Any, scenario_id: str, *, mode: str = "NO KEY UPDATE"
) -> str | None:
    """Take the scenario's row lock and return its ``aoi_id`` (possibly null).

    The serialisation point for the writers that read a scenario's state and
    then decide from it, reached from an API route where a double-submit is
    the ordinary case rather than the exotic one:

    * ``add_rule`` reads ``max(ordinal)``; two unlocked callers read the same
      one and the second hits ``UNIQUE (scenario_id, ordinal)``.
    * ``materialize_scenario`` reads the rules and resolves them, then
      deletes, upserts and publishes. Two unlocked runs can interleave so that
      the *later* resolution is overwritten by the earlier one's writes: a rule
      removed between the two is re-materialised, and because the second run's
      digest query cannot see the first's uncommitted override rows it retires
      the attribute's version over them — override rows with no current
      ``asset_version``, which is invisible to freshness.

    ``NO KEY UPDATE`` for those writers, ``UPDATE`` for ``delete_scenario``.
    A compute unit takes ``KEY SHARE`` on this row from the end of its compute
    until it commits (``runners._fenced``). ``KEY SHARE`` conflicts with ``FOR
    UPDATE`` and not with ``FOR NO KEY UPDATE``: so a delete waits for a unit
    that is committing and then cascades what it wrote, while drawing a rule
    does not wait for a compute.

    Every writer here locks the scenario before any of its rules, which is the
    order ``delete_scenario`` uses (the rules go last, by cascade).
    """
    cur.execute(
        f"SELECT aoi_id FROM app_core.scenario WHERE id = %s FOR {mode}",
        (scenario_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ScenarioNotFound(f"scenario {scenario_id} does not exist")
    return None if row[0] is None else str(row[0])


class RuleNotFound(ScenarioNotFound):
    """The rule id names no rule of this scenario.

    A subclass of ``ScenarioNotFound``: on every surface the answer is the
    same — what you named is not there.
    """


class RuleCatchesNothing(Exception):
    """The rule's area catches no block of the scenario's area of interest.

    Refused rather than stored: an area drawn over the sea, outside the AOI,
    or with longitude and latitude swapped would otherwise be a rule that
    changes nothing and still queues a run.
    """

    def __init__(self, rule_id: str | None) -> None:
        self.rule_id = rule_id
        super().__init__(
            "the area catches no block of the scenario's area of interest "
            "(a block is caught when its interior point is inside the area)"
        )


def _rule_row(cur: Any) -> dict:
    cols = [d[0] for d in cur.description]
    return _stringify_ids(dict(zip(cols, cur.fetchone(), strict=True)))


def _rule_id(rule_id: str) -> str:
    """A rule id in canonical spelling; a malformed one names no rule."""
    try:
        return str(uuid.UUID(str(rule_id)))
    except ValueError as exc:
        raise RuleNotFound(f"no rule with id {rule_id!r}") from exc


def add_rule(
    conn: Any,
    *,
    scenario_id: str,
    geometry_wkt: str,
    type_id: str,
    inputs: Mapping[str, Any],
    mode: str = "set",
    units: Mapping[str, Any] | None = None,
) -> tuple[dict, MaterializeResult]:
    """Append one rule, materialise the scenario, and return both.

    * ``inputs`` are the planner's numbers, checked by ``proposal.check_inputs``
      (the one place every surface's refusals come from); a number left out is
      not set, and each block keeps its own value for it.
    * ``mode`` is ``"set"`` (the numbers are the blocks' values) or
      ``"change"`` (each moves a block from its own baseline, by an amount or
      a percentage per ``units``) — see ``_resolve_rules``.
    * The geometry is repaired on write — see ``_repair_geometry``.
    * ``ordinal`` is ``max + 1`` under the scenario's row lock, and never
      changes after. ``ordinal`` rather than ``created_at`` orders rules
      because ``now()`` is transaction-start time: two rules written together
      share it.
    * Materialised in the same transaction (``materialize_for_rule``), so a
      rule the materialisation refuses is never stored.

    Returns the rule as stored and what the materialisation did.
    """
    from reimagina.scenarios.proposal import check_inputs, check_units

    # Before any SQL: a refused id or number costs no lock.
    scenario_id = _canonical_id(scenario_id)
    checked = check_inputs(inputs, mode=mode, units=units)
    checked_units = check_units(mode, units, checked)
    _require_postgres(conn)
    with conn.cursor() as cur:
        repaired = _repair_geometry(cur, geometry_wkt)
        _lock_scenario(cur, scenario_id)
        cur.execute(
            f"""
            INSERT INTO curated_geo.scenario_rule
              (scenario_id, ordinal, type_id, geometry, inputs, mode, units)
            SELECT %(s)s, COALESCE(max(ordinal), 0) + 1, %(t)s,
                   ST_GeomFromEWKB(%(g)s), %(i)s::jsonb, %(m)s, %(u)s::jsonb
            FROM curated_geo.scenario_rule
            WHERE scenario_id = %(s)s
            RETURNING {_RULE_COLUMNS}
            """,
            {
                "s": scenario_id,
                "t": type_id,
                "g": repaired,
                "i": json.dumps(checked),
                "m": mode,
                "u": json.dumps(checked_units),
            },
        )
        rule = _rule_row(cur)
        _touch(cur, scenario_id)
    return rule, materialize_for_rule(conn, scenario_id=scenario_id, rule_id=rule["id"])


def update_rule(
    conn: Any,
    *,
    scenario_id: str,
    rule_id: str,
    geometry_wkt: str | None,
    inputs: Mapping[str, Any],
    unset: Sequence[str] = (),
    merge: bool = False,
    mode: str | None = None,
    units: Mapping[str, Any] | None = None,
) -> tuple[dict, MaterializeResult]:
    """Change one rule in place, materialise, and return both.

    ``merge=False`` (the API's ``PATCH``, sent from a form holding every
    number): ``inputs`` is the rule's **whole** set, and a number left out is
    no longer set. ``merge=True`` (a typed command naming only what changes):
    the numbers given replace the rule's own, those in ``unset`` are removed,
    the rest are kept — merged on the rule as it is under the scenario's lock.
    Its area changes when one is given.

    In place is the point: the rule keeps its id and its ordinal, so it keeps
    its position in "later wins" — re-adding a number it had dropped does not
    move it ahead of a later rule.

    ``mode`` and ``units`` travel with the numbers: a whole set without a
    ``mode`` is a set rule (its units are dropped); a merge without one keeps
    the rule's own mode and units.
    """
    from reimagina.scenarios.proposal import check_inputs, check_units

    scenario_id = _canonical_id(scenario_id)
    rule_id = _rule_id(rule_id)
    missing = RuleNotFound(f"scenario {scenario_id} has no rule {rule_id}")

    def _checked(
        current: dict[str, Any], rule_mode: str, rule_units: Mapping[str, Any]
    ) -> tuple[str, dict[str, float], dict[str, str]]:
        given = {k: v for k, v in inputs.items() if v is not None or not merge}
        merged = {**current, **given}
        numbers = check_inputs(
            {k: v for k, v in merged.items() if k not in unset},
            mode=rule_mode,
            units=rule_units,
        )
        # A kept unit of a number the merge removed goes with the number.
        kept = {k: v for k, v in rule_units.items() if k in numbers}
        return rule_mode, numbers, check_units(rule_mode, kept, numbers)

    # A whole set is checked before any SQL; a merge needs the rule first.
    checked = None if merge else _checked({}, mode or "set", units or {})
    _require_postgres(conn)
    with conn.cursor() as cur:
        repaired = None if geometry_wkt is None else _repair_geometry(cur, geometry_wkt)
        _lock_scenario(cur, scenario_id)
        if checked is None:
            cur.execute(
                "SELECT inputs, mode, units FROM curated_geo.scenario_rule "
                "WHERE id = %s AND scenario_id = %s",
                (rule_id, scenario_id),
            )
            found = cur.fetchone()
            if found is None:
                raise missing
            current, rule_mode, rule_units = found
            checked = (
                _checked(current, rule_mode, {**rule_units, **(units or {})})
                if mode is None
                else _checked(current, mode, units or {})
            )
        new_mode, numbers, new_units = checked
        cur.execute(
            f"""
            UPDATE curated_geo.scenario_rule
               SET inputs = %(i)s::jsonb,
                   mode = %(m)s,
                   units = %(u)s::jsonb,
                   geometry = COALESCE(ST_GeomFromEWKB(%(g)s), geometry),
                   updated_at = now()
             WHERE id = %(r)s AND scenario_id = %(s)s
            RETURNING {_RULE_COLUMNS}
            """,
            {
                "i": json.dumps(numbers),
                "m": new_mode,
                "u": json.dumps(new_units),
                "g": repaired,
                "r": rule_id,
                "s": scenario_id,
            },
        )
        if cur.rowcount == 0:
            raise missing
        rule = _rule_row(cur)
        _touch(cur, scenario_id)
    return rule, materialize_for_rule(conn, scenario_id=scenario_id, rule_id=rule_id)


def delete_rule(
    conn: Any, *, scenario_id: str, rule_id: str
) -> tuple[None, MaterializeResult]:
    """Delete one rule and materialise what is left.

    Ordinals are not renumbered: they only have to be ordered, not dense.
    The scenario is locked first, the order ``delete_scenario`` uses. Returns
    ``(None, result)``, the shape the other two writers return.
    """
    scenario_id = _canonical_id(scenario_id)
    rule_id = _rule_id(rule_id)
    _require_postgres(conn)
    with conn.cursor() as cur:
        _lock_scenario(cur, scenario_id)
        cur.execute(
            "DELETE FROM curated_geo.scenario_rule "
            "WHERE id = %s AND scenario_id = %s RETURNING 1",
            (rule_id, scenario_id),
        )
        if cur.fetchone() is None:
            raise RuleNotFound(f"scenario {scenario_id} has no rule {rule_id}")
        _touch(cur, scenario_id)
    return None, materialize_for_rule(conn, scenario_id=scenario_id, rule_id=None)


def _repair_geometry(cur: Any, geometry_wkt: str) -> bytes:
    """The drawn area as stored: 2D, valid, polygonal, as EWKB.

    ``ST_Force2D`` because GeoJSON from QGIS or a GPS carries a third
    coordinate and the column is 2D (an insert raised, as a 500).
    ``ST_MakeValid`` then the polygonal part, because the column CHECK is a
    *type* check: a self-intersecting bowtie, still an ``ST_Polygon``, passes
    it, and an invalid polygon reaching ``ST_Covers`` later raises a GEOS
    exception mid-run or answers wrongly. If nothing polygonal survives
    (``POLYGON EMPTY``, a line, unparseable text) the result is
    ``InvalidGeometry``.
    """
    cur.execute("SAVEPOINT repair_geometry")
    try:
        cur.execute(
            """
            SELECT ST_AsEWKB(g)
            FROM (SELECT ST_CollectionExtract(ST_MakeValid(
                      ST_Force2D(ST_SetSRID(ST_GeomFromText(%s), 4326))), 3
                  ) AS g) r
            WHERE NOT ST_IsEmpty(g)
            """,
            (geometry_wkt,),
        )
    except (
        psycopg2.errors.InvalidParameterValue,
        psycopg2.errors.InternalError_,
    ) as exc:
        cur.execute("ROLLBACK TO SAVEPOINT repair_geometry")
        raise InvalidGeometry(f"geometry could not be parsed: {exc}") from exc
    repaired = cur.fetchone()
    cur.execute("RELEASE SAVEPOINT repair_geometry")
    if repaired is None:
        raise InvalidGeometry(
            "geometry has no polygonal area after repair "
            "(empty, or a point or line rather than a polygon)"
        )
    return bytes(repaired[0])


def _touch(cur: Any, scenario_id: str) -> None:
    cur.execute(
        "UPDATE app_core.scenario SET updated_at = now() WHERE id = %s",
        (scenario_id,),
    )


def materialize_for_rule(
    conn: Any, *, scenario_id: str, rule_id: str | None
) -> MaterializeResult:
    """Materialise after a rule was written, refusing what cannot stand.

    Called in the transaction that wrote (or deleted) the rule, so a refusal
    — a ceiling breach anywhere in the scenario, or the rule catching no block
    — rolls the write back with it. The server never keeps a rule it refused,
    and no client has to undo one. ``rule_id`` is None after a delete, which
    has no area to check.
    """
    result = materialize_scenario(conn, scenario_id=scenario_id)
    if rule_id is not None and result.matched_per_rule.get(rule_id, 0) == 0:
        raise RuleCatchesNothing(rule_id)
    return result


# ── Materialisation ─────────────────────────────────────────────────────────

#: The three attributes the ceiling kernel needs, in ``BlockInputs`` order.
#:
#: Exactly ``catalog._EDIFICABILITY_READS``; named here rather than imported
#: because ``BlockInputs`` calls the first one ``levels``, so a mapping has to
#: exist somewhere and a tuple that silently reorders itself would be worse
#: than one that is written down.
_CEILING_ATTRIBUTES = ("num_levels", "unit_size", "people_per_household")


def materialize_scenario(conn: Any, *, scenario_id: str) -> MaterializeResult:
    """Resolve a scenario's drawn rules into per-feature override rows.

    Seven steps, in this order, in the caller's one transaction:

    1. lock the scenario row and refuse without an AOI (``ScenarioHasNoAOI``);
    2. select the features each rule's area covers, scoped to the AOI;
    3. refuse the whole run if any resolved block breaches a ceiling;
    4. delete the overrides no rule implies any more;
    5. upsert the resolved ones in a single ``apply_overrides``, unpublished;
    6. ``publish_scenario_version`` with **no** ``attributes``, once;
    7. retire the scenario results the removed overrides were behind
       (``_supersede_unbacked_results``).

    **Steps 6 and 7 are the two halves of the same ghost**, and both are easy
    to leave out because nothing fails when you do.

    Publishing only the attributes step 5 touched would leave an attribute
    whose last override row vanished in step 4 with a current version forever,
    and the indicators reading it would never go stale. Step 6 passes no
    ``attributes`` precisely so ``publish_scenario_version`` reconciles
    retirements too — see its docstring.

    ``bulk_write_numeric_results`` marks stale only the features it is
    *writing*, and a recompute covers only blocks that still carry an
    override, so a block a rule left keeps its old ``status='current'``
    scenario row — which ``serving_geo.indicator_overlay_tile`` goes on
    painting. Step 7 is what makes "remove every rule" visibly remove the
    choropleth.

    Nothing is written before step 3, so a refusal is total rather than
    partial: a scenario that breaches a ceiling keeps whatever it was
    materialised to last time, and ``materialized_at`` does not move.
    """
    _require_postgres(conn)
    scenario_id = _canonical_id(scenario_id)
    aoi_id = _scenario_aoi(conn, scenario_id)
    resolution = _resolve_rules(conn, scenario_id, aoi_id)
    _refuse_ceiling_breaches(conn, resolution)
    resolved = resolution.values

    removed = _delete_unimplied_overrides(conn, scenario_id, resolved)
    written = apply_overrides(
        conn,
        scenario_id=scenario_id,
        edits=[
            {"feature_id": feature_id, "attribute_key": key, "value_numeric": value}
            for (feature_id, key), value in sorted(resolved.items())
        ],
        publish=False,
    )
    # The one publish, with no ``attributes``: it versions what step 5 wrote
    # and retires an attribute whose last override row step 4 deleted.
    publish_scenario_version(conn, scenario_id)
    superseded = _supersede_unbacked_results(conn, scenario_id, removed)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_core.scenario SET materialized_at = now() WHERE id = %s",
            (scenario_id,),
        )
    return MaterializeResult(
        matched_per_rule=resolution.matched,
        overrides_written=written,
        overrides_removed=len(removed),
        results_superseded=superseded,
        footprint_features=tuple(sorted({feature_id for feature_id, _ in resolved})),
        footprint_attributes=tuple(
            sorted({key for _, key in resolved} | {key for _, key in removed})
        ),
        aoi_id=aoi_id,
    )


def _canonical_id(scenario_id: str) -> str:
    """The scenario id in the one spelling a partition key may use.

    Postgres compares ``uuid`` values, so ``WHERE id = 'ABC…'`` finds the row
    whatever the case. A partition key is a *string*, ``scenario=<id>``, and
    compares as one. Materialising under an uppercase id wrote versions and
    materialisations under a key no lowercase delete would ever match, so they
    outlived the scenario. Both writers of that key canonicalise here first.

    A malformed id is a scenario that does not exist, the same answer
    ``_lock_scenario`` gives for a well-formed one that matches nothing.
    """
    try:
        return str(uuid.UUID(str(scenario_id)))
    except ValueError as exc:
        raise ScenarioNotFound(f"scenario {scenario_id} does not exist") from exc


def _scenario_aoi(conn: Any, scenario_id: str) -> str:
    """Step 1: the scenario's AOI, or a refusal. See ``ScenarioHasNoAOI``.

    Reads it under ``_lock_scenario``, which is why this is step 1 and not a
    convenience lookup: the lock has to be held before the rules are read, or
    the resolution can already be stale by the time it is written.

    Every surface requires an AOI when a scenario is created, so a null here
    means the AOI was deleted since (``ON DELETE SET NULL``). Nothing can set
    a new one — the rules were drawn over that AOI's blocks — so the message
    says what can be done instead.
    """
    with conn.cursor() as cur:
        aoi_id = _lock_scenario(cur, scenario_id)
    if aoi_id is None:
        raise ScenarioHasNoAOI(
            f"scenario {scenario_id} has no area of interest (the one it was "
            "created on was deleted), so there is no bound on what its rules "
            "would select. Create a new scenario on an existing area of "
            "interest and draw the rules there."
        )
    return aoi_id


@dataclass(frozen=True)
class _Resolution:
    """Step 2's answer: what each rule caught and what each block resolves to."""

    #: Rule id -> blocks its area caught (0 included), in "later wins" order.
    matched: dict[str, int]
    #: (feature, attribute) -> value, later rules winning.
    values: dict[tuple[str, str], float]
    #: (feature, attribute) -> the rule that supplied the value.
    owner: dict[tuple[str, str], str]
    #: Rule id -> (mode, units), for a refusal to speak in the rule's terms.
    modes: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)


def _resolve_rules(conn: Any, scenario_id: str, aoi_id: str) -> _Resolution:
    """Step 2: select the blocks each rule covers and resolve their values.

    ``ST_Covers(r.geometry, f.geometry_centroid)`` is the platform's one
    answer to "did my drawn shape catch this?" — the same predicate
    ``count_features_within_polygon`` uses (r027), which the map's selection
    card counts through ``POST /metrics/within-polygon``, so the count the
    user saw while drawing is the count they get here. ``ST_Covers`` rather
    than ``ST_Contains`` because a grid-aligned manzana's
    ``ST_PointOnSurface`` can land exactly on a shared block edge, which
    DE-9IM's exclusive boundary would silently drop.

    The AOI test is an ``EXISTS`` against ``aoi_feature`` rather than an
    intersection with the AOI polygon, so materialisation scopes by exactly the
    membership every other read scopes by.

    A ``LEFT JOIN``, so a rule whose area caught nothing still appears in
    ``matched`` with 0.

    Collisions resolve by the *rule's* ordinal, later wins, per attribute: a
    later rule that sets only household size leaves an earlier rule's storeys
    standing on the blocks they share. Iterating in ordinal order and letting
    a later write overwrite is the whole of that rule.

    A ``change`` rule's value is the block's **baseline** moved by the rule
    (``proposal.changed_value``), never an earlier rule's value: rules do not
    stack, the later one still replaces. A block with no baseline for an
    attribute gets no value for it from a change rule.
    """
    from reimagina.scenarios.proposal import RULE_INPUTS, changed_value, edits_for

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id::text, r.inputs, r.mode, r.units, f.id::text
            FROM curated_geo.scenario_rule r
            LEFT JOIN curated_geo.feature f
                   ON f.type_id = r.type_id
                  AND ST_Covers(r.geometry, f.geometry_centroid)
                  AND EXISTS (
                        SELECT 1 FROM curated_geo.aoi_feature af
                         WHERE af.aoi_id = %(aoi)s AND af.feature_id = f.id
                      )
            WHERE r.scenario_id = %(s)s
            ORDER BY r.ordinal
            """,
            {"s": scenario_id, "aoi": aoi_id},
        )
        rows = cur.fetchall()
        change_features = sorted(
            {f for _, _, mode, _, f in rows if mode == "change" and f is not None}
        )
        change_keys = sorted(
            {
                attribute
                for _, inputs, mode, _, _ in rows
                if mode == "change"
                for name in inputs
                for attribute in RULE_INPUTS[name].attributes
            }
        )
        base = (
            _baseline_values(cur, change_features, change_keys)
            if change_features
            else {}
        )

    matched: dict[str, int] = {}
    values: dict[tuple[str, str], float] = {}
    owner: dict[tuple[str, str], str] = {}
    edits: dict[str, list[dict[str, Any]]] = {}
    modes: dict[str, tuple[str, dict[str, str]]] = {}
    for rule_id, inputs, mode, units, feature_id in rows:
        if rule_id not in matched:
            matched[rule_id] = 0
            modes[rule_id] = (mode, dict(units or {}))
            edits[rule_id] = edits_for(**inputs)
        if feature_id is None:
            continue
        matched[rule_id] += 1
        if mode == "change":
            for name, by in inputs.items():
                spec = RULE_INPUTS[name]
                for attribute in spec.attributes:
                    start = base.get(attribute, {}).get(feature_id)
                    if start is None:
                        continue
                    key = (feature_id, attribute)
                    values[key] = changed_value(start, by, units.get(name, "abs"), spec)
                    owner[key] = rule_id
            continue
        for edit in edits[rule_id]:
            key = (feature_id, edit["attribute_key"])
            values[key] = float(edit["value_numeric"])
            owner[key] = rule_id
    return _Resolution(matched, values, owner, modes)


def _baseline_values(
    cur: Any, feature_ids: list[str], keys: Sequence[str]
) -> dict[str, dict[str, float]]:
    """``effective_numeric`` at ``scenario_id=None`` (observation ⊕ base) for
    every key at once, as ``{key: {feature_id: value}}``; absent values are
    left out. One statement, not one per key and type."""
    found: dict[str, dict[str, float]] = {k: {} for k in keys}
    cur.execute(
        """
        SELECT f.id::text, k.key,
               COALESCE(obs.value_numeric,
                        NULLIF(f.attributes->>k.key, '')::double precision)
        FROM curated_geo.feature f
        CROSS JOIN unnest(%s::text[]) AS k(key)
        LEFT JOIN curated_geo.observation obs
               ON obs.feature_id = f.id
              AND obs.attribute_key = k.key
              AND obs.is_current
        WHERE f.id = ANY(%s::uuid[])
        """,
        (list(keys), feature_ids),
    )
    for feature_id, key, value in cur.fetchall():
        if value is not None:
            found[key][feature_id] = value
    return found


def _refuse_ceiling_breaches(conn: Any, resolution: _Resolution) -> None:
    """Step 3: one ``BlockInputs`` per resolved block, each with its own area.

    Per block, not per selection. Two rules can resolve to different values on
    different blocks — six storeys in one district and twelve in another is an
    ordinary proposal — and the two area-scaled bounds then have nothing in
    common.

    The values are the *post*-materialisation effective ones: the resolved
    override where this run writes one, otherwise observation ⊕ base. Reading
    the baseline at ``scenario_id=None`` is deliberate — an override row this
    run is about to delete in step 4 must not stand in for a value that will
    be gone.

    A block missing any of the three edificability inputs is skipped by the
    compute, so its levels read as 0 here, which ``check_ceilings`` skips for
    those bounds. Its ``floors`` is still checked, because planned floors
    computes.

    A refusal names the rules each breaching block is computed with and the
    storeys that block would allow, so the planner knows which rule to change
    and to what. Every bound but the floors one scales linearly with storeys,
    so the allowed storeys are ``levels × bound / would_be``.
    """
    resolved = resolution.values
    feature_ids = sorted({feature_id for feature_id, _ in resolved})
    if not feature_ids:
        return

    checked = (*_CEILING_ATTRIBUTES, "floors", "lot_coverage")
    with conn.cursor() as cur:
        baseline = _baseline_values(cur, feature_ids, checked)
        cur.execute(
            "SELECT id::text, ST_Area(geometry::geography) "
            "FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
            (feature_ids,),
        )
        areas = dict(cur.fetchall())

    def _value(feature_id: str, key: str) -> float:
        # A block with no lot_coverage is the whole block, not none of it.
        absent = 1.0 if key == "lot_coverage" else 0.0
        return resolved.get((feature_id, key), baseline[key].get(feature_id, absent))

    def _computed(feature_id: str) -> bool:
        return all(
            (feature_id, key) in resolved or feature_id in baseline[key]
            for key in _CEILING_ATTRIBUTES
        )

    blocks = {
        feature_id: BlockInputs(
            feature_id=feature_id,
            # 0 skips the three edificability bounds, not the floors one.
            levels=_value(feature_id, "num_levels") if _computed(feature_id) else 0.0,
            unit_size=_value(feature_id, "unit_size"),
            people_per_household=_value(feature_id, "people_per_household"),
            area_m2=float(areas.get(feature_id, 0.0)),
            floors=_value(feature_id, "floors"),
            lot_coverage=_value(feature_id, "lot_coverage"),
        )
        for feature_id in feature_ids
    }
    breaches = check_ceilings(blocks.values())
    if not breaches:
        return
    allowed: dict[tuple[str, str], int] = {}
    for breach in breaches:
        block = blocks[breach.feature_id]
        levels = block.floors if breach.indicator_slug == FLOORS else block.levels
        if levels and breach.would_be > 0:
            allowed[(breach.feature_id, breach.indicator_slug)] = math.floor(
                levels * breach.bound / breach.would_be
            )
    breaching = {b.feature_id for b in breaches}
    owners: dict[str, set[str]] = {}
    for (feature_id, _), rule_id in resolution.owner.items():
        if feature_id in breaching:
            owners.setdefault(feature_id, set()).add(rule_id)
    # A rule's "Rule N" is its place in ordinal order, which ``matched`` keeps.
    number = {rule_id: n for n, rule_id in enumerate(resolution.matched, 1)}
    rules = {
        feature_id: tuple(sorted(((r, number[r]) for r in ids), key=lambda p: p[1]))
        for feature_id, ids in owners.items()
    }
    raise CeilingExceeded(
        breaches,
        rules=rules,
        allowed_levels=allowed,
        advice=_ceiling_advice(resolution, baseline, breaches, allowed, number),
    )


def _ceiling_advice(
    resolution: _Resolution,
    baseline: dict[str, dict[str, float]],
    breaches: list[CeilingBreach],
    allowed: dict[tuple[str, str], int],
    number: dict[str, int],
) -> str | None:
    """ "Rule 2 is too much for 40 blocks: at most +24 % storeys fits all of
    them." — per rule that sets the breaching blocks' storeys, the largest
    value every one of them takes, in the rule's own terms. A change rule is
    told a change (``+24 %``, ``+8 storeys``), never the total it implies."""
    fits: dict[str, float] = {}
    blocks: dict[str, set[str]] = {}
    for breach in breaches:
        most = allowed.get((breach.feature_id, breach.indicator_slug))
        key = "floors" if breach.indicator_slug == FLOORS else "num_levels"
        rule_id = resolution.owner.get((breach.feature_id, key))
        if most is None or rule_id is None:
            continue
        mode, units = resolution.modes.get(rule_id, ("set", {}))
        start = baseline.get(key, {}).get(breach.feature_id)
        if mode != "change":
            value = float(most)
        elif start is None or start <= 0:
            continue
        elif units.get("levels", "abs") == "pct":
            value = math.floor((most / start - 1) * 100)
        else:
            value = math.floor(most - start)
        fits[rule_id] = min(value, fits.get(rule_id, value))
        blocks.setdefault(rule_id, set()).add(breach.feature_id)
    if not fits:
        return None
    parts = []
    for rule_id in sorted(fits, key=lambda r: number.get(r, 0)):
        n = len(blocks[rule_id])
        where = "this block" if n == 1 else f"{n} blocks"
        value = fits[rule_id]
        if resolution.modes.get(rule_id, ("set", {}))[0] != "change":
            what = f"at most {value:g} storeys fit"
        elif value <= 0:
            what = "it cannot add storeys to all of them"
        elif resolution.modes[rule_id][1].get("levels", "abs") == "pct":
            what = f"at most +{value:g} % storeys fits"
        else:
            what = f"at most +{value:g} storeys fits"
        parts.append(
            f"Rule {number.get(rule_id, '?')} is too much for {where}: {what}."
        )
    return " ".join(parts)


def _delete_unimplied_overrides(
    conn: Any, scenario_id: str, resolved: dict[tuple[str, str], float]
) -> list[tuple[str, str]]:
    """Step 4: drop this scenario's override rows no surviving edit implies.

    Returns the ``(feature_id, attribute_key)`` pairs removed, because step 7
    needs exactly those ids and they are gone by the time it runs.

    An anti-join against the resolved pairs rather than "delete everything and
    re-insert". The rows carry no state worth keeping, but deleting them all
    would churn the version digest in ``publish_scenario_version`` for a
    scenario nobody edited, and the whole point of a content digest is that
    re-saving an unchanged what-if recomputes nothing.

    With nothing resolved the ``unnest`` yields no rows and every override goes
    — which is right: an edit-less scenario overrides nothing.
    """
    pairs = sorted(resolved)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM curated_geo.scenario_override o
            WHERE o.scenario_id = %(s)s
              AND NOT EXISTS (
                    SELECT 1
                      FROM unnest(%(fids)s::uuid[], %(keys)s::text[])
                           AS implied(feature_id, attribute_key)
                     WHERE implied.feature_id = o.feature_id
                       AND implied.attribute_key = o.attribute_key
                  )
            RETURNING o.feature_id::text, o.attribute_key
            """,
            {
                "s": scenario_id,
                "fids": [feature_id for feature_id, _ in pairs],
                "keys": [key for _, key in pairs],
            },
        )
        return [(feature_id, key) for feature_id, key in cur.fetchall()]


def _supersede_unbacked_results(
    conn: Any, scenario_id: str, removed: list[tuple[str, str]]
) -> int:
    """Step 7: retire the scenario results no override stands behind any more.

    Three statements, each for a different ghost:

    * **A block with no override left loses every scenario row** — deleted,
      any status. It reads the baseline through the overlay, so a scenario row
      for it is at best a copy of the baseline, and a copy that keeps
      shadowing the baseline after the baseline moves (a census refit, a new
      height). This also clears rows a whole-AOI run wrote for blocks no rule
      ever touched. The recompute that follows covers only blocks that still
      carry an override, so nothing writes them back.
    * **A block that lost one number but keeps others** has its results for
      the indicators reading that number marked ``stale`` per
      ``(feature, attribute)`` — the recompute rewrites them, and a block the
      compute now skips (it lost one of the three edificability inputs) stops
      painting its old value.
    * **The scenario partition's materialisation records of those indicators
      are deleted**, so freshness cannot vouch for them. Versions are content
      digests and come back: re-adding a removed rule, or moving it back,
      returns the override version to the one an old run recorded, and that
      old record would call the indicator fresh over rows this step just
      retired — a noop recompute and a scenario stuck on the baseline.

    The readers of an attribute are the same closure a recompute runs: the
    indicators reading it *and* their dependents (a composite reads no
    attribute directly).
    """
    from reimagina.assets.keys import indicator_asset_key, partition_key
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import compute_dirty_set

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM curated_geo.indicator_result r
             WHERE r.scenario_id = %(s)s
               AND NOT EXISTS (
                     SELECT 1 FROM curated_geo.scenario_override o
                      WHERE o.scenario_id = %(s)s AND o.feature_id = r.feature_id)
            """,
            {"s": scenario_id},
        )
        retired = cur.rowcount
    if not removed:
        return retired

    # One closure per attribute, not per removed pair: a large rule removes
    # thousands of pairs over a handful of attributes.
    slugs_for = {
        key: [u.indicator for u in compute_dirty_set([key], [], SHIPPED_INDICATORS)]
        for key in {attribute_key for _, attribute_key in removed}
    }
    ordered = sorted(
        {
            (slug, feature_id)
            for feature_id, attribute_key in removed
            for slug in slugs_for[attribute_key]
        }
    )
    slugs = sorted({slug for slug, _ in ordered})
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE curated_geo.indicator_result r
               SET status = 'stale', updated_at = now()
              FROM unnest(%(slugs)s::text[], %(fids)s::uuid[])
                   AS unbacked(slug, feature_id)
             WHERE r.scenario_id = %(s)s
               AND r.status = 'current'
               AND r.indicator_slug = unbacked.slug
               AND r.feature_id = unbacked.feature_id
            """,
            {
                "s": scenario_id,
                "slugs": [slug for slug, _ in ordered],
                "fids": [feature_id for _, feature_id in ordered],
            },
        )
        retired += cur.rowcount
        cur.execute(
            """
            DELETE FROM curated_geo.asset_materialization m
             USING curated_geo.asset a
             WHERE a.id = m.asset_id
               AND m.partition_key = %s
               AND a.asset_key = ANY(%s)
            """,
            (
                partition_key({"scenario": scenario_id}),
                [indicator_asset_key(slug) for slug in slugs],
            ),
        )
    return retired


# ── Deletion ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeleteResult:
    """What one deletion took away.

    Counts rather than a bare ``True``: "it is gone" is several separate
    claims, and the only way to see that each was made is to count it. The
    scenario row itself is not counted — it is always exactly one, or the
    call raised.
    """

    overrides: int
    versions_deleted: int
    results_deleted: int
    job_runs_deleted: int
    materializations: int
    runs_deleted: int


#: What ``delete_scenario`` deletes, in order: (DeleteResult field or None for
#: uncounted, table, key column). A ``partition_key`` column is matched against
#: the scenario's partition string, any other against its id.
_DELETE_STEPS: tuple[tuple[str | None, str, str], ...] = (
    ("overrides", "curated_geo.scenario_override", "scenario_id = %s"),
    ("versions_deleted", "curated_geo.asset_version", "partition_key = %s"),
    # The scenario_id tables cascade from the scenario row (r055); the counted
    # ones are deleted explicitly first so each count is its own table's.
    ("results_deleted", "curated_geo.indicator_result", "scenario_id = %s"),
    ("job_runs_deleted", "jobs_runtime.job_run", "scenario_id = %s"),
    ("materializations", "curated_geo.asset_materialization", "partition_key = %s"),
    # Finished recompute runs; their units cascade. A run still in flight
    # stays: the coordinator still writes its row, and its units end
    # `cancelled` when they find the scenario gone.
    (
        "runs_deleted",
        "jobs_runtime.orchestration_run",
        "scenario_id = %s AND status IN ('succeeded','partial','failed','cancelled')",
    ),
    # Last: cascades its rules and its AOI summaries.
    (None, "app_core.scenario", "id = %s"),
)


def delete_scenario(conn: Any, *, scenario_id: str) -> DeleteResult:
    """Delete a scenario and every row keyed by it (``_DELETE_STEPS``), in the
    caller's transaction.

    ``scenario_override``, ``indicator_result``, ``job_run`` and
    ``aoi_indicator_summary`` reference the scenario ``ON DELETE CASCADE``
    (r055); versions and materialisations are keyed by the *string*
    ``scenario=<id>``, which no FK can follow, so those two are deleted here
    or not at all.

    **Deleted, not staled or retired.** They used to be kept "as history", but
    nothing can read it: every surface that shows a scenario's numbers starts
    from the scenario row. Kept, they only grew — 614,880 results for three
    deleted scenarios on the pilot database.

    The ``job_run`` delete is safe to scope by ``scenario_id`` alone: r016's
    trigger forces a result's ``scenario_id`` to equal its ``job_run``'s, so a
    run carrying this id produced this scenario's results and nothing else. Its
    other referrers, ``job_unit`` and ``asset_materialization``, are ``ON
    DELETE SET NULL``. The scenario's finished recompute runs
    (``orchestration_run``, cascading their ``job_unit`` rows) are deleted too;
    one still in flight stays, its units ending ``cancelled``.

    The ``asset_materialization`` delete is not cosmetic.
    ``asset_component_v`` unions ``asset_materialization`` into its node set
    with no filter, so the partition of a deleted scenario would stay a node in
    the graph — one whose upstream versions are gone — and ``assets stale``
    would list it for ever.

    The scenario's row lock is taken first, ``FOR UPDATE``, before anything is
    read or deleted. A compute unit takes ``KEY SHARE`` on the row only once
    its compute is done, until it commits (``runners._fenced``): a delete
    arriving then waits a moment and removes what the unit wrote; one arriving
    earlier does not wait, and the unit, finding no scenario, rolls its writes
    back and ends ``cancelled``. Taking the lock
    is also the existence check — an unknown id raises ``ScenarioNotFound``
    rather than reporting zeros for a typo.

    Nothing here commits; the caller owns the transaction, so a failure at any
    step leaves the scenario whole.
    """
    _require_postgres(conn)
    scenario_id = _canonical_id(scenario_id)
    partition = partition_key({"scenario": scenario_id})
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        _lock_scenario(cur, scenario_id, mode="UPDATE")
        for field, table, where in _DELETE_STEPS:
            param = partition if where.startswith("partition_key") else scenario_id
            cur.execute(f"DELETE FROM {table} WHERE {where}", (param,))
            if field is not None:
                counts[field] = int(cur.rowcount)
    return DeleteResult(**counts)


def _stringify_ids(row: dict) -> dict:
    for key in ("id", "project_id", "aoi_id", "type_id"):
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row
