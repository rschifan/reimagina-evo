"""Scenario authoring over HTTP: create it, draw rules on it, recompute, compare.

The same operations ``apps/cli/scenario.py`` offers, for the map. A rule is a
row (``curated_geo.scenario_rule``) with its own id; the edits its numbers
write, the refusal of a degenerate or implausible value, the density precheck
and the unioning of a drawn FeatureCollection all come from
``reimagina.scenarios.proposal``, which exists so that this router and the CLI
refuse the same things for the same reasons. A router that re-derived them
would bring back the failure ``edits_for`` prevents — planned floors frozen on
the baseline while edificability moves — and one that imported them from
``apps/cli`` would make a deployable app
depend on another deployable app, which is the mistake
``test_the_shared_backend_never_imports_a_deployable_app`` forbids in the
other direction.

What *is* owned here, and nowhere else:

**The connection.** Every handler opens its own, and the writes commit it:
``with contextlib.closing(connect(api_dsn())) as conn, conn:``. Never
``Depends(get_db_conn)``: the pooled dependency rolls back on the *success*
path (``apps/api/deps.py``), so a write through it answers 200 and persists
nothing, and no assertion on the response can see that. The reads open their
own connection too, so that the contract test can say "no handler in this
module depends on the pool" about every route rather than about a list
someone has to keep in step — and so a GET that grows a write later cannot
inherit the pool by having been a GET.

``closing()`` around it with the connection itself as the inner context,
because psycopg2's connection context manager scopes the *transaction*: it
commits or rolls back and leaves the socket open. In a long-lived API process
that is a leaked backend per request, which is what ``POST /jobs`` learned.

**The status code of every refusal.** The scenario layer raises ten distinct
things and leaves two driver errors raw; each becomes one status, chosen
once, in ``_refusals``. The last row is the exception — handled app-wide rather
than here, for the reason given under it:

===============================  ======  =====================================
raised                           status  why
===============================  ======  =====================================
``ScenarioNotFound``             404     one vocabulary for "what you named is
                                        not there" — a scenario, an AOI, an
                                        indicator slug
``DuplicateScenarioName``        409     the name is how a scenario is
                                        addressed; the clash is with state
``ScenarioHasNoAOI``             409     the request is faultless and the
                                        resource's state is not
``ProposalValueError``           400     a number no indicator could be
                                        computed from, whatever is stored
``InvalidGeometry``              400     a point is not an area, ever; nor
                                        is one outside lon/lat degrees
``RuleCatchesNothing``           422     the area parses and catches no block
                                        of the AOI; nothing is stored
``ChangeHasNoBaseline``          422     a change rule's blocks have no value
                                        to change; nothing is stored
``CeilingPrecheckFailed``        422     every field parses and is plausible;
                                        the *combination* cannot be processed
``CeilingExceeded``              422     same, per block, with the blocks,
                                        the rules and the storeys allowed
``ForeignKeyViolation``          404     a well-formed id naming no record —
                                        the same answer the layer gives when
                                        it does the check itself
a malformed id in the path       404     ``test_malformed_ids.py``'s rule: an
                                        id no scenario can have names no
                                        scenario, and it must not become SQL
a malformed id in a filter       422     the same file's other half: rejected
                                        at the boundary, before any statement
``OpsPlaneNotProvisioned``       503     the engine's own schema is missing from
                                        this database, so no run can be
                                        submitted at all. Registered app-wide
                                        in ``create_app``, not mapped here: a
                                        deployment state rather than anything
                                        about this request, and the same answer
                                        on all three routes that submit
===============================  ======  =====================================

That last one carries no materialisation counts, deliberately. ``_enqueue_failure``
below reports what stands for a *constraint* failure; this is the other kind, where
the counts are no use to a client that cannot read anything back until an operator
runs ``bootstrap-db`` — the same reasoning that re-raises a connection-class
failure bare.

``_refusals`` does **not** wrap the enqueue half of ``recompute``. By then the
materialisation is committed, so the same mapping would describe the wrong half
— an FK violation from ``submit_run`` answering 404 "no record matches the id
in this request", about ids that were fine, after overrides were written. That
phase has its own translation, ``_enqueue_failure``, which names the phase and
reports what stands — for a *constraint* failure. A connection-class failure is
re-raised untranslated, so an unreachable database answers 503 at the enqueue
exactly as it does at the materialisation one phase earlier: two codes for one
outage, on one route, is worse than either code alone.

``ScenarioNotFound`` and not ``LookupError``. The broad class also catches the
``KeyError``/``IndexError`` of a genuine defect anywhere under ``authoring``
or ``comparison``, and a 404 for that is a bug reported as the client's typo —
the hardest kind to find, because nobody looks for a server defect behind a
404. ``ScenarioNotFound`` subclasses ``LookupError`` precisely so the narrow
catch is available.

**The dialect.** ``proposal``'s messages name fields (``levels``), never flags
(``--levels``), and never tell the reader to run a command: a 400 describing a
command line is written for someone this API does not have. So the refusals
here carry the JSON field the client sent — ``field``, for a form to
highlight — and ``allowed_levels``, so the form can clamp its own input
rather than re-derive a catalog bound. ``apps/cli/scenario.py`` is the same
translation into the other dialect.

**No credential in any response.** The CLI once printed ``recompute``'s DSN
with its password, so every payload it prints is now masked recursively. These
payloads are built here, so a mask would protect nothing — the DSN simply is
not in them, which is also why the CLI's ``{"dsn": …}`` key has no counterpart
here.

Requires ``DATABASE_URL_DIRECT`` and a running ``python -m apps.worker`` for
``POST /{id}/recompute`` to get past the enqueue, exactly as ``POST /jobs``
does: the materialisation is synchronous and committed, the indicator
recompute is a queued run, and the 202 says which half is which.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from reimagina.db import connect
from reimagina.scenarios.authoring import (
    CeilingExceeded,
    ChangeHasNoBaseline,
    DuplicateScenarioName,
    InvalidGeometry,
    MaterializeResult,
    RuleCatchesNothing,
    ScenarioHasNoAOI,
    ScenarioNotFound,
    add_rule,
    create_scenario,
    delete_rule,
    delete_scenario,
    get_scenario,
    list_scenarios,
    materialize_scenario,
    unfinished_runs,
    update_rule,
)
from reimagina.scenarios.comparison import compare
from reimagina.scenarios.proposal import (
    CeilingPrecheckFailed,
    EmptyProposal,
    ProposalValueError,
    polygon_wkt,
    reachable_indicators,
    table_statistic,
)

from ..deps import api_dsn, get_backend
from .indicators import IndicatorSummary, catalog_row_to_dict

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

#: Every shipped indicator is block-grain, so this is the type a
#: drawn rule applies to unless the caller says otherwise.
DEFAULT_TYPE_CONTEXT = "MEX"
DEFAULT_TYPE_SLUG = "block"

#: How many breaches a refusal names. The count is exact; the list is capped,
#: because a selection can breach on every block of a city and 19,058 of them
#: is not an error message. The planner's next action needs a few examples and
#: the size of the problem, not the problem itself.
_MAX_BREACHES_REPORTED = 20


# ── wire shapes ──────────────────────────────────────────────────────────


class RuleBody(BaseModel):
    """One rule: an area and the numbers a planner sets over it.

    ``inputs`` maps a number's name (``proposal.RULE_INPUTS``: ``levels``,
    ``unit_size``, ``people_per_household``, ``lot_coverage``) to its value. A
    number left out, or null, is not set: each block keeps its own value for
    it. At least one must be set. Values are refused by the domain
    (``proposal.check_inputs``) rather than by this model, so a refusal is a
    400 naming the field rather than a schema complaint.
    """

    area: dict[str, Any] = Field(
        description="The drawn area as GeoJSON: a FeatureCollection, a "
        "Feature, or a bare geometry. Several polygons are unioned into one "
        "area, because a selection made with two strokes is one rule."
    )
    inputs: dict[str, Any] = Field(
        description="The numbers the rule sets, by name: levels, unit_size, "
        "people_per_household, lot_coverage (0 < c ≤ 1). Omitted or null: "
        "not set."
    )
    mode: Literal["set", "change"] = Field(
        "set",
        description='"set": the numbers are the blocks\' new values. "change": '
        "each number moves a block from its own baseline value (never from an "
        "earlier rule's), by an amount or a percentage per ``units``.",
    )
    units: dict[str, Literal["abs", "pct"]] = Field(
        default_factory=dict,
        description='For a change rule: per number, "abs" (an amount; lot '
        'coverage as a share, 0.05 = +5 points) or "pct". Missing: "abs".',
    )
    type_context: str = DEFAULT_TYPE_CONTEXT
    type_slug: str = DEFAULT_TYPE_SLUG


class CreateScenarioBody(BaseModel):
    """A named what-if inside a project, bounded by one of its AOIs.

    ``project_id`` is required rather than derived from ``aoi_id`` the way
    ``scenario create`` derives it. The CLI derives it because a person at a
    terminal would otherwise type a uuid they have to look up first; an HTTP
    client already has it — ``GET /aois`` carries ``project_id`` on every row.

    ``aoi_id`` is required. A scenario without one could hold rules but never
    materialise them, and nothing can set it later — its rules are drawn over
    that AOI's blocks — so it was a dead end.

    ``rule`` is optional: drawing on the baseline city creates the scenario
    and its first rule together, and a refusal of either stores neither.
    """

    project_id: UUID
    name: str = Field(min_length=1)
    aoi_id: UUID
    description: str | None = None
    rule: RuleBody | None = Field(
        None,
        description="The scenario's first rule. Created, written and "
        "materialised in one transaction, so a refused rule leaves no "
        "scenario behind.",
    )


class RuleUpdateBody(BaseModel):
    """A rule's whole new set of numbers, and optionally its new area.

    ``inputs`` replaces the rule's numbers: one left out is no longer set.
    The rule keeps its id and its place in "later wins".
    """

    area: dict[str, Any] | None = Field(
        None, description="The moved or resized area; omitted keeps the area."
    )
    inputs: dict[str, Any]
    mode: Literal["set", "change"] | None = Field(
        None,
        description='"set" or "change", as for a new rule. Omitted: the rule '
        "keeps its own mode (and its units for numbers given none), so a "
        "client that does not know about modes cannot turn a change into a "
        'set. Send "set" to make a change rule a set rule.',
    )
    units: dict[str, Literal["abs", "pct"]] = Field(
        default_factory=dict,
        description='For a change rule: per number, "abs" (an amount; lot '
        'coverage as a share, 0.05 = +5 points) or "pct". Missing: the '
        'rule\'s own unit, else "abs".',
    )


class RuleRow(BaseModel):
    id: str
    #: Its place in "later wins": assigned at creation, never changed after.
    ordinal: int
    type_id: str
    inputs: dict[str, float]
    mode: str = "set"
    units: dict[str, str] = Field(default_factory=dict)
    geometry_wkt: str
    created_at: datetime
    updated_at: datetime


class ScenarioRow(BaseModel):
    id: str
    project_id: str
    aoi_id: str | None = None
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    # None means the rules have not been expanded into overrides yet, which is
    # a different statement from "expanded, and there was nothing to do".
    materialized_at: datetime | None = None


class LatestRun(BaseModel):
    id: str
    status: str


class ScenarioListRow(ScenarioRow):
    """A list row: the scenario plus what a summary of every scenario needs.

    Only on the list. ``get_scenario`` does not compute them, and a detail
    defaulting ``rule_count`` to 0 beside its own non-empty ``rules`` would be
    a response contradicting itself.
    """

    rule_count: int
    #: The newest run envelope (``orchestration_run``), or None. A queued run
    #: has no envelope until a worker opens it, so during that window this is
    #: the previous run.
    latest_run: LatestRun | None = None


class ScenarioDetail(ScenarioRow):
    override_count: int = 0
    rules: list[RuleRow] = []


class RecomputeResponse(BaseModel):
    """What the materialisation did, and the run that will recompute from it.

    ``matched_per_rule`` is per *rule* and counts what that rule's area caught
    inside the AOI — the number a drawing UI needs ("your shape caught 4
    blocks").

    ``run_id`` is null for a submit that found nothing to recompute. The
    materialisation still happened and its counts still stand; handing back an
    id that ``GET /jobs/{run_id}`` would 404 is what the jobs router already
    refuses to do. When a run of this scenario is still outstanding, that run
    is answered instead (``deduplicated``): "nothing to do" would read as
    "done" while it is not.
    """

    scenario_id: str
    matched_per_rule: dict[str, int] = {}
    overrides_written: int = 0
    overrides_removed: int = 0
    results_superseded: int = 0
    run_id: str | None = None
    run_status: str = "queued"
    deduplicated: bool = False
    """True when ``run_id`` names a run that was already doing this work.

    Carried for the same reason ``POST /jobs`` carries it: a resubmission is
    answered exactly like a fresh submit — 202, a run id, a status — so
    without this a client cannot tell that its ``run_id`` names work already
    in flight. Dropping it here while the jobs router reports it would make
    the same orchestrator answer differ by which route asked."""


class RuleChangeResponse(RecomputeResponse):
    """A rule written (or deleted), materialised, and its recompute enqueued.

    ``rule`` is the rule as stored, or null after a delete.
    """

    rule: RuleRow | None = None


class CreatedScenario(ScenarioDetail):
    """The scenario as ``GET`` returns it, and its first rule's change when
    the body carried one (null otherwise)."""

    first_rule: RuleChangeResponse | None = None


class DeleteScenarioResponse(BaseModel):
    """One count per table the delete removed rows from.

    "It is gone" is several separate claims, and counting each is the only way
    to see that each was made.
    """

    scenario_id: str
    overrides: int = 0
    versions_deleted: int = 0
    results_deleted: int = 0
    job_runs_deleted: int = 0
    materializations: int = 0
    runs_deleted: int = 0


class ComparisonSummary(BaseModel):
    """⚠ Three counts and no total, on purpose.

    ``changed + unchanged + new == row_count`` always — but that sum is *less*
    than the AOI's block count whenever the baseline indicator has gaps, since
    a feature with no numeric value on either side is absent from the rows by
    design. A ``total`` here would be read as the size of the territory. If
    the gap is wanted it is a ``no_value`` key, counted deliberately, not a
    subtraction a reader cannot see.

    ``extra="forbid"`` is what makes that sentence true tomorrow. The route
    builds this with ``ComparisonSummary(**result.summary)``, and pydantic's
    default is to *ignore* a key it does not declare — so the ``no_value``
    the paragraph above anticipates would be dropped on the floor, silently,
    with every test still green. Forbidding instead turns the day
    ``comparison`` grows a key into a loud failure here, where the fix is to
    declare the field.
    """

    model_config = ConfigDict(extra="forbid")

    changed: int = 0
    unchanged: int = 0
    new: int = 0
    # None when no row has a baseline to subtract from. Averaged over every
    # comparable row, the untouched zeros included: it is the mean shift
    # across the territory, so upzoning two blocks of 19,058 reads small.
    mean_delta: float | None = None
    # Whether a positive delta is an improvement. From the catalog, because
    # only the catalog knows: four of the twelve indicators are distances.
    higher_is_better: bool = True


class ComparisonRow(BaseModel):
    feature_id: str
    baseline: float | None = None
    scenario: float | None = None
    # Null for a ``new`` row: a number with nothing to compare against is not
    # a change of that size.
    delta: float | None = None
    state: str


class ComparisonResponse(BaseModel):
    scenario_id: str
    indicator_slug: str
    aoi_id: str | None = None
    summary: ComparisonSummary
    # Always the whole comparison, whatever ``limit`` did to ``rows`` — a
    # header that disagrees with the table below it is the failure
    # ``ComparisonResult.summary`` is counted off its own rows to avoid.
    row_count: int = 0
    rows: list[ComparisonRow] = []
    rows_truncated: bool = False


# ── refusals ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _refusals() -> Iterator[None]:
    """Translate the scenario layer's refusals into statuses. See the module docstring."""
    try:
        yield
    except ScenarioNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ChangeHasNoBaseline as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "change_has_no_baseline",
                "field": exc.names[0] if exc.names else None,
                "message": str(exc),
            },
        ) from exc
    except RuleCatchesNothing as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "rule_catches_nothing",
                "field": "area",
                "message": f"{exc}. Nothing was stored; draw it over the "
                "scenario's blocks.",
            },
        ) from exc
    except DuplicateScenarioName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScenarioHasNoAOI as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EmptyProposal as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_proposal", "field": None, "message": str(exc)},
        ) from exc
    except ProposalValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_value",
                "field": exc.field,
                "message": str(exc),
            },
        ) from exc
    except InvalidGeometry as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_geometry", "field": "area", "message": str(exc)},
        ) from exc
    except CeilingPrecheckFailed as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "ceiling_precheck_failed",
                "message": (
                    f"{exc} Lower levels, or raise unit_size, and post the area again."
                ),
                "levels": exc.levels,
                "unit_size": exc.unit_size,
                # What a form needs to clamp its own input instead of
                # re-deriving `valid_max × unit_size / 10000`.
                "allowed_levels": exc.allowed_levels,
            },
        ) from exc
    except CeilingExceeded as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "ceiling_exceeded",
                "message": str(exc),
                "breach_count": len(exc.breaches),
                "breaches": [
                    {
                        "feature_id": breach.feature_id,
                        "indicator_slug": breach.indicator_slug,
                        "bound": breach.bound,
                        "would_be": breach.would_be,
                        # Which rules the block is computed with, and their
                        # "Rule N" numbers, so a form can point at them.
                        "rule_ids": [r for r, _ in exc.rules_of(breach)],
                        "rule_numbers": [n for _, n in exc.rules_of(breach)],
                        "allowed_levels": exc.allowed_levels.get(
                            (breach.feature_id, breach.indicator_slug)
                        ),
                    }
                    for breach in exc.breaches[:_MAX_BREACHES_REPORTED]
                ],
            },
        ) from exc
    except psycopg2.errors.ForeignKeyViolation as exc:
        field = _reference_field(exc)
        named = field or "reference"
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_reference",
                "field": field,
                "message": f"no record matches the {named} in this request",
            },
        ) from exc


@contextlib.contextmanager
def _enqueue_failure(
    scenario_id: str, materialized: MaterializeResult
) -> Iterator[None]:
    """The *second* half of a recompute, refused without disowning the first.

    ``recompute`` is two transactions and only the first is undoable. By the
    time the enqueue runs, the overrides are written and committed, so the
    one thing the response must not do is describe the request as having
    failed: ``_refusals`` would turn a ``ForeignKeyViolation`` from
    ``submit_run`` into 404 ``unknown_reference`` — "no record matches the id
    in this request" — about ids that were fine, after work that happened.

    So the two phases answer differently. 500, because the client cannot fix a
    *constraint* failure by changing the request: the scenario exists (the
    materialisation just locked it) and the AOI was read from its own row, so a
    violated constraint here is state moving underneath us or a defect. The
    detail names the phase and carries the materialisation's counts, so a
    client knows what stands — and a retry is safe, because materialisation is
    idempotent.

    ⚠ **Connection-class failures are explicitly not that**, and the first
    version of this function swallowed them. ``psycopg2.Error`` is the parent
    of ``OperationalError`` and ``InterfaceError``, so a database that went
    away at the enqueue step — which used to reach
    ``errors.database_unavailable_handler`` and answer 503, as it still does
    one phase earlier in the same route — started answering 500 instead. One
    route, two codes for one outage, and the 500 reads as terminal to a client
    whose retry logic keys on 503, while this body's own text says to retry.
    So they are re-raised, before the clause below can see them.

    The re-raise rather than hoisting ``connect()`` out of this context, which
    was the other option on offer: hoisting only moves the *connect* out of the
    clause, and an outage that arrives mid-statement inside ``submit_run`` —
    the same cause, a few microseconds later — would still answer 500. The
    condition is "the database is unreachable", not "the database was
    unreachable at this one call site", so the discrimination belongs where the
    exception is classified.
    """
    try:
        yield
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Must precede the clause below: both are subclasses of psycopg2.Error.
        # Nothing is translated — 503 is the app-wide handler's answer, and the
        # materialisation's counts are of no use to a client that has to wait
        # for the database before it can read anything back anyway.
        raise
    except psycopg2.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "recompute_not_enqueued",
                "phase": "enqueue",
                "message": (
                    f"scenario {scenario_id} was materialised and committed; "
                    "the indicator recompute was not accepted. Retry — "
                    "materialisation is idempotent."
                ),
                "materialized": {
                    "overrides_written": materialized.overrides_written,
                    "overrides_removed": materialized.overrides_removed,
                    "results_superseded": materialized.results_superseded,
                },
            },
        ) from exc


def _reference_field(exc: psycopg2.Error) -> str | None:
    """Which body field an FK violation was about, from the constraint's name.

    Read from ``diag.constraint_name`` rather than from the driver's message,
    which names the table and the constraint and is internal —
    ``errors.py``'s rule is that the internal message is never echoed back.
    A substring match rather than a table of exact names, so renaming a
    constraint while keeping its column in the name does not silently stop
    identifying the field; an unrecognised one degrades to a 404 that names
    no field rather than to a 500.

    Only the first two are reachable from this router today: a drawn rule's
    ``type_id`` comes from ``_feature_type_id``, which refuses an undeclared
    type before any insert, and its ``scenario_id`` from ``_lock_scenario``,
    which refuses an unknown scenario the same way. They are listed because
    the FK is what would catch a route that one day took either one raw, and
    because the answer would be the same.
    """
    name = getattr(getattr(exc, "diag", None), "constraint_name", None) or ""
    for field in ("project_id", "aoi_id", "type_id", "scenario_id"):
        if field in name:
            return field
    return None


def _scenario_id(raw: str) -> str:
    """A path id, normalised — or the same 404 an unknown one gets.

    ``app_core.scenario.id`` is ``uuid``, so a mistyped id used to travel to
    Postgres and come back a ``DataError``, which ``database_error_handler``
    maps to 500 *and* which aborts the request's transaction on the way out.
    This is the whole of the fix: the typo never becomes SQL.

    404 rather than 422 because this id *names* the resource rather than
    filtering one, and an id no scenario can have names no scenario — the
    answer, and the reasoning, ``test_malformed_ids.py`` already settled for
    ``/jobs/{run_id}``. The two are indistinguishable to a client anyway.
    """
    try:
        return str(UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScenarioNotFound(f"no scenario with id {raw!r}") from exc


def _opt(value: UUID | None) -> str | None:
    """A filter id as the domain takes it: ``str``, or nothing at all."""
    return None if value is None else str(value)


def _feature_type_id(conn: Any, context: str, slug: str) -> str:
    """The feature type a drawn rule applies to, through the storage backend.

    ``get_type_id`` raises a bare ``ValueError`` for a type nobody declared,
    which is a client error with no class of its own; it becomes the one
    refusal that already means "what you named is not there".
    """
    try:
        return get_backend(conn).feature_repo().get_type_id(context, slug)
    except ValueError as exc:
        raise ScenarioNotFound(f"no feature type {context}:{slug}") from exc


# ── scenarios ────────────────────────────────────────────────────────────


@router.get("", response_model=list[ScenarioListRow])
def list_scenarios_route(
    project_id: UUID | None = Query(default=None),
    aoi_id: UUID | None = Query(default=None),
) -> list[ScenarioListRow]:
    """Scenarios, newest first, optionally narrowed by project and/or AOI.

    Each row carries its ``rule_count`` and ``latest_run``.
    """
    with _refusals(), contextlib.closing(connect(api_dsn())) as conn:
        rows = list_scenarios(conn, project_id=_opt(project_id), aoi_id=_opt(aoi_id))
    return [ScenarioListRow(**row) for row in rows]


@router.post("", status_code=201, response_model=CreatedScenario)
def create_scenario_route(body: CreateScenarioBody) -> CreatedScenario:
    """Create a named what-if and answer with it, as ``GET`` would.

    One shape for the created resource and for a later read, so a client that
    just created a scenario does not need a second request to learn what it
    looks like.

    With a ``rule``, the scenario and the rule are one transaction — the
    rule's refusals (``POST …/rules``) roll the scenario back with it, so no
    client has to delete an empty scenario its refused first rule was going
    into — and the rule's recompute is enqueued after the commit
    (``first_rule``).
    """
    first = body.rule
    with _refusals(), contextlib.closing(connect(api_dsn())) as conn, conn:
        scenario_id = create_scenario(
            conn,
            project_id=str(body.project_id),
            name=body.name,
            aoi_id=str(body.aoi_id),
            description=body.description,
        )
        change = (
            None
            if first is None
            else add_rule(
                conn,
                scenario_id=scenario_id,
                geometry_wkt=polygon_wkt(first.area),
                type_id=_feature_type_id(conn, first.type_context, first.type_slug),
                inputs=first.inputs,
                mode=first.mode,
                units=first.units,
            )
        )
        created = get_scenario(conn, scenario_id)
        if created is None:
            # Read in the transaction that wrote it, so this is unreachable —
            # and it is a `raise`, not an `assert`, because `python -O` strips
            # asserts and the next line would then unpack None into a 500 with
            # a TypeError that says nothing about where it came from.
            raise RuntimeError(
                f"scenario {scenario_id} is not readable in the transaction "
                "that created it"
            )
    if change is None:
        return CreatedScenario(**created)
    rule, result = change
    return CreatedScenario(
        **created,
        first_rule=RuleChangeResponse(
            **_enqueue(scenario_id, result), rule=RuleRow(**rule)
        ),
    )


class ReachableIndicator(IndicatorSummary):
    """An indicator a scenario rule can change, and the number a table shows.

    ``statistic`` is ``sum`` for counts (dwellings, people) and ``median``
    otherwise; ``proposal.table_statistic`` is the one place that decides.
    """

    statistic: Literal["sum", "median"]


# ⚠ Declared before `/{scenario_id}`: FastAPI matches in declaration order, and
# below it this path is read as a scenario called "indicators" and answers 404.
@router.get("/indicators", response_model=list[ReachableIndicator])
def reachable_indicators_route() -> list[ReachableIndicator]:
    """The indicators a scenario rule can change, inputs before dependents.

    Derived from the catalog's declared reads (``proposal.reachable_indicators``)
    rather than listed, so the scenario workspace offers what a rule moves and
    nothing a rule cannot.
    """
    order = reachable_indicators()
    with _refusals(), contextlib.closing(connect(api_dsn())) as conn:
        rows = get_backend(conn).indicator_catalog_repo().list_indicators()
    by_slug = {row["slug"]: catalog_row_to_dict(row) for row in rows}
    return [
        ReachableIndicator(
            **by_slug[slug], statistic=table_statistic(by_slug[slug]["unit"])
        )
        for slug in order
        if slug in by_slug
    ]


@router.get("/{scenario_id}", response_model=ScenarioDetail)
def get_scenario_route(scenario_id: str) -> ScenarioDetail:
    """One scenario with its rules in ordinal order, and its override count."""
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn:
            found = get_scenario(conn, resolved)
        if found is None:
            raise HTTPException(
                status_code=404, detail=f"no scenario with id {scenario_id!r}"
            )
    return ScenarioDetail(**found)


def _cancel(run_ids: list[str]) -> None:
    """Cancel a deleted scenario's unfinished runs. Best effort.

    Not what keeps the database clean — a unit still computing finds the
    scenario gone before it commits and rolls its writes back
    (``runners._fenced``), and one already committing held the delete back a
    moment, then was cascaded. This stops the
    queue spending minutes on a scenario nobody can see, and the jobs page
    showing it as running.
    """
    import logging

    from reimagina.orchestration.factory import get_orchestrator

    for run_id in run_ids:
        try:
            with contextlib.closing(connect(api_dsn())) as conn, conn:
                get_orchestrator().cancel_run(conn, run_id)
        except Exception:  # noqa: BLE001 — the delete has committed; this is tidying
            logging.getLogger(__name__).warning(
                "could not cancel run %s of a deleted scenario", run_id, exc_info=True
            )


@router.delete("/{scenario_id}", response_model=DeleteScenarioResponse)
def delete_scenario_route(scenario_id: str) -> DeleteScenarioResponse:
    """Delete a scenario and every row keyed by it.

    **404 for an id that is not there, not 204.** The data layer refuses
    rather than "reporting four zeros for a typo" — its own words — and this
    response *is* those four counts, so a silent success would be a body of
    zeros claiming a deletion happened. That is the confident "nothing to see"
    the comparison refuses for an unknown AOI, and it is indistinguishable
    from a scenario that really had no traces. A second DELETE after a
    successful one therefore answers 404, which is accurate: it is not there.
    Idempotence is not free here, it is paid for in information.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            running = unfinished_runs(conn, resolved)
            removed = delete_scenario(conn, scenario_id=resolved)
    _cancel([run_id for run_id, _ in running])
    return DeleteScenarioResponse(
        scenario_id=resolved,
        overrides=removed.overrides,
        versions_deleted=removed.versions_deleted,
        results_deleted=removed.results_deleted,
        job_runs_deleted=removed.job_runs_deleted,
        materializations=removed.materializations,
        runs_deleted=removed.runs_deleted,
    )


# ── rules ────────────────────────────────────────────────────────────────


@router.get("/{scenario_id}/rules", response_model=list[RuleRow])
def list_rules_route(scenario_id: str) -> list[RuleRow]:
    """A scenario's rules in "later wins" order."""
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn:
            found = get_scenario(conn, resolved)
        if found is None:
            raise ScenarioNotFound(f"no scenario with id {scenario_id!r}")
    return [RuleRow(**row) for row in found["rules"]]


@router.post("/{scenario_id}/rules", status_code=201, response_model=RuleChangeResponse)
def add_rule_route(scenario_id: str, body: RuleBody) -> RuleChangeResponse:
    """Draw one rule, materialise the scenario, and enqueue its recompute.

    **The rule and the materialisation are one transaction** (``add_rule``
    does both). A rule that breaches a ceiling on any block, or catches no
    block of the scenario's AOI, is refused (422) and nothing is stored — the
    server never keeps a rule it would not materialise, so no client has to
    undo one. The recompute is enqueued after the commit, for the reason
    ``recompute`` gives; if only the enqueue fails, the rule stands (500
    ``recompute_not_enqueued``) and a later recompute picks it up.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        geometry_wkt = polygon_wkt(body.area)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            type_id = _feature_type_id(conn, body.type_context, body.type_slug)
            rule, result = add_rule(
                conn,
                scenario_id=resolved,
                geometry_wkt=geometry_wkt,
                type_id=type_id,
                inputs=body.inputs,
                mode=body.mode,
                units=body.units,
            )
    return RuleChangeResponse(**_enqueue(resolved, result), rule=RuleRow(**rule))


@router.patch("/{scenario_id}/rules/{rule_id}", response_model=RuleChangeResponse)
def update_rule_route(
    scenario_id: str, rule_id: str, body: RuleUpdateBody
) -> RuleChangeResponse:
    """Change one rule's numbers, and optionally its area, in place.

    ``inputs`` is the rule's whole set (``merge=False``). The rule keeps its
    id and its place in "later wins". Same refusals, in the same transaction,
    as ``POST …/rules``: a refused change leaves the rule as it was.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        geometry_wkt = None if body.area is None else polygon_wkt(body.area)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            rule, result = update_rule(
                conn,
                scenario_id=resolved,
                rule_id=rule_id,
                geometry_wkt=geometry_wkt,
                inputs=body.inputs,
                mode=body.mode,
                units=body.units,
            )
    return RuleChangeResponse(**_enqueue(resolved, result), rule=RuleRow(**rule))


@router.delete("/{scenario_id}/rules/{rule_id}", response_model=RuleChangeResponse)
def delete_rule_route(scenario_id: str, rule_id: str) -> RuleChangeResponse:
    """Delete one rule — one row, one transaction — and recompute.

    Refused (422) only when what is left breaches a ceiling: an earlier rule
    the deleted one was overriding can then win, and its numbers are checked
    like any other.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            _, result = delete_rule(conn, scenario_id=resolved, rule_id=rule_id)
    return RuleChangeResponse(**_enqueue(resolved, result))


# ── recompute ────────────────────────────────────────────────────────────


def _enqueue(scenario_id: str, result: MaterializeResult) -> dict[str, Any]:
    """Enqueue the recompute of a committed materialisation's footprint.

    **The dirty set is the footprint**, not freshness: every block a current
    rule overrides, with the attributes involved (removed ones included).
    Planned from freshness, every indicator reading a changed attribute
    recomputed every block of the AOI under the scenario — 19,058 in Mexicali
    for a rule over 2,400 — though a block without an override reads the
    baseline through the overlay anyway.

    An empty footprint enqueues nothing: an empty feature list means "every
    feature" to the planner, and a scenario with no override left has nothing
    to compute — materialisation already retired its rows.

    A noop while a run of this scenario is still going answers that run
    (``deduplicated``): the change is being computed, and "nothing to do"
    would read as "done".

    Runs on its own connection, after the materialisation committed: see
    ``recompute_route``. Answers the fields of ``RecomputeResponse``.
    """
    from reimagina.orchestration.factory import get_orchestrator
    from reimagina.provenance import code_version

    fields: dict[str, Any] = {
        "scenario_id": scenario_id,
        "matched_per_rule": result.matched_per_rule,
        "overrides_written": result.overrides_written,
        "overrides_removed": result.overrides_removed,
        "results_superseded": result.results_superseded,
        "run_id": None,
        "run_status": "noop",
        "deduplicated": False,
    }
    # ⚠ Outside `_refusals`: the materialisation is committed, so a refusal
    # raised from here on is about the *enqueue* — see `_enqueue_failure`.
    with (
        _enqueue_failure(scenario_id, result),
        contextlib.closing(connect(api_dsn())) as conn,
        conn,
    ):
        if result.footprint_features:
            handle = get_orchestrator().submit_run(
                conn,
                changed_attributes=list(result.footprint_attributes),
                edited_features=list(result.footprint_features),
                scenario_id=scenario_id,
                aoi_id=result.aoi_id,
                code_version=code_version(),
            )
            status = handle.status.status if handle.status else "queued"
            if status != "noop":
                fields.update(
                    run_id=handle.run_id,
                    run_status=status,
                    deduplicated=handle.deduplicated,
                )
                return fields
        running = unfinished_runs(conn, scenario_id)
    if running:
        fields.update(run_id=running[0][0], run_status=running[0][1], deduplicated=True)
    return fields


@router.post(
    "/{scenario_id}/recompute", status_code=202, response_model=RecomputeResponse
)
def recompute_route(scenario_id: str) -> RecomputeResponse:
    """Materialise the scenario's rules, then enqueue the recompute.

    Writing a rule already does both; this is for retrying a recompute that
    was not enqueued, or that failed.

    **Two transactions, and the boundary is a decision rather than an
    accident.** ``materialize_scenario`` locks the scenario row as its first
    statement and holds it until the caller commits. The evaluation that
    follows computes indicators over the footprint — up to every block of the
    AOI. Under one transaction that lock would be held for the whole compute,
    blocking every other writer. So this commits the materialisation, closes
    the connection, and enqueues on its own.

    The state in between — overrides current, results not yet recomputed — is
    one the platform already models: it is exactly what ``asset_freshness_v``
    calls stale, and what a crash between the two steps would leave anyway.
    The next recompute converges from it, because materialisation is
    idempotent and freshness is derived rather than remembered. The AOI the
    run is scoped to is the one the materialisation read under its lock.

    202, not 200: the materialisation is done and committed, the indicator
    recompute is accepted. ``run_id`` is what follows it.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            result = materialize_scenario(conn, scenario_id=resolved)
    return RecomputeResponse(**_enqueue(resolved, result))


# ── comparison ───────────────────────────────────────────────────────────


@router.get("/{scenario_id}/comparison", response_model=ComparisonResponse)
def comparison_route(
    scenario_id: str,
    indicator_slug: str = Query(description="Indicator to compare."),
    aoi_id: UUID | None = Query(default=None),
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Per-feature rows to return. Omitted, every row is "
        "returned; `row_count` and `summary` always cover all of them.",
    ),
) -> ComparisonResponse:
    """One indicator under the scenario against the baseline it departs from.

    Each row is ``{feature_id, baseline, scenario, delta, state}``, where
    ``scenario`` is the *effective* value — a scenario stores only what it
    changed and reads the rest through to the baseline — and ``state`` is
    ``changed``, ``unchanged`` or ``new``. ``new`` is the common case for the
    five indicators that exist only under a scenario: they have no baseline
    row at all, so a two-state model would render them as absent.

    Rows are not truncated unless ``limit`` says so, because a silently
    shortened per-feature list reads as a smaller territory. ``row_count``
    and ``summary`` describe the whole comparison either way.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn:
            result = compare(
                conn,
                scenario_id=resolved,
                indicator_slug=indicator_slug,
                aoi_id=_opt(aoi_id),
            )
    rows = result.rows if limit is None else result.rows[:limit]
    return ComparisonResponse(
        scenario_id=resolved,
        indicator_slug=indicator_slug,
        aoi_id=_opt(aoi_id),
        summary=ComparisonSummary(**result.summary),
        row_count=len(result.rows),
        rows=[ComparisonRow(**row) for row in rows],
        rows_truncated=len(rows) < len(result.rows),
    )
