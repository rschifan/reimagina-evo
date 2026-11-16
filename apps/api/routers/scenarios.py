"""Scenario authoring over HTTP: create it, draw rules on it, recompute, compare.

The same six operations ``apps/cli/scenario.py`` offers, for the map. Nothing
here owns a rule: the four edits one proposal writes, the refusal of a
degenerate value, the density precheck and the unioning of a drawn
FeatureCollection all come from ``reimagina.scenarios.proposal``, which exists
so that this router and the CLI refuse the same things for the same reasons.
A router that re-derived them would bring back the failure the four-edit rule
prevents — planned floors frozen on the baseline while edificability moves —
and one that imported them from ``apps/cli`` would make a deployable app
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

**The status code of every refusal.** The scenario layer raises nine distinct
things and leaves two driver errors raw; each becomes one status, chosen
once, in ``_refusals``:

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
``InvalidGeometry``              400     a point is not an area, ever
``CeilingPrecheckFailed``        422     every field parses and is plausible;
                                        the *combination* cannot be processed
``CeilingExceeded``              422     same, per block, with the blocks
``ForeignKeyViolation``          404     a well-formed id naming no record —
                                        the same answer the layer gives when
                                        it does the check itself
a malformed id in the path       404     ``test_malformed_ids.py``'s rule: an
                                        id no scenario can have names no
                                        scenario, and it must not become SQL
a malformed id in a filter       422     the same file's other half: rejected
                                        at the boundary, before any statement
===============================  ======  =====================================

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
from typing import Any
from uuid import UUID

import psycopg2
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from reimagina.db import connect
from reimagina.scenarios.authoring import (
    CeilingExceeded,
    DuplicateScenarioName,
    InvalidGeometry,
    MaterializeResult,
    ScenarioHasNoAOI,
    ScenarioNotFound,
    add_edit,
    create_scenario,
    delete_scenario,
    get_scenario,
    list_scenarios,
    materialize_scenario,
    remove_edit,
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

from ..deps import api_dsn, get_backend

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

#: Every shipped indicator is block-grain (AGENTS.md), so this is the type a
#: drawn rule applies to unless the caller says otherwise.
DEFAULT_TYPE_CONTEXT = "MEX"
DEFAULT_TYPE_SLUG = "block"

#: How many breaches a refusal names. The count is exact; the list is capped,
#: because a selection can breach on every block of a city and 19,058 of them
#: is not an error message. The planner's next action needs a few examples and
#: the size of the problem, not the problem itself.
_MAX_BREACHES_REPORTED = 20


# ── wire shapes ──────────────────────────────────────────────────────────


class CreateScenarioBody(BaseModel):
    """A named what-if inside a project, optionally bounded by an AOI.

    ``project_id`` is required rather than derived from ``aoi_id`` the way
    ``scenario create`` derives it. The CLI derives it because a person at a
    terminal would otherwise type a uuid they have to look up first; an HTTP
    client already has it — ``GET /aois`` carries ``project_id`` on every row
    — so deriving it here would buy nothing and would put a SELECT of its own
    in a router that has none.

    ``aoi_id`` is optional here and required by ``recompute``: a scenario can
    be named before its scope is chosen, and the refusal when it is still
    missing says so.
    """

    project_id: UUID
    name: str = Field(min_length=1)
    aoi_id: UUID | None = None
    description: str | None = None


class ProposalBody(BaseModel):
    """One proposal: an area and the three numbers a planner proposes over it.

    The field names are the domain's own (``proposal.require_positive`` raises
    about ``levels``, not ``storeys``), so a refusal can name the field the
    client actually sent without a translation table that could rot.

    ``levels`` writes **both** ``floors`` and ``num_levels`` — the same
    quantity under the two names the indicators read. All three numbers are
    required because ``edificability.py`` skips any feature missing any of
    them, and a scenario that silently computed nothing is the hardest failure
    to see.

    Non-finite values are *not* refused here, deliberately. ``allow_inf_nan``
    would make ``NaN`` a 422 from the model, which reads as a transport
    complaint; it is a domain refusal — ``require_positive`` owns zero,
    negative and non-finite together, for this surface and the CLI both — and
    it is answered with the field name a form can highlight.
    """

    area: dict[str, Any] = Field(
        description="The drawn area as GeoJSON: a FeatureCollection, a "
        "Feature, or a bare geometry. Several polygons are unioned into one "
        "area, because a selection made with two strokes is one rule."
    )
    levels: float
    unit_size: float
    people_per_household: float
    type_context: str = DEFAULT_TYPE_CONTEXT
    type_slug: str = DEFAULT_TYPE_SLUG


class EditRow(BaseModel):
    id: str
    ordinal: int
    type_id: str
    attribute_key: str
    value_numeric: float
    geometry_wkt: str
    created_at: datetime


class ScenarioRow(BaseModel):
    id: str
    project_id: str
    aoi_id: str | None = None
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    # None means the edits have not been expanded into overrides yet, which is
    # a different statement from "expanded, and there was nothing to do".
    materialized_at: datetime | None = None


class ScenarioDetail(ScenarioRow):
    override_count: int = 0
    edits: list[EditRow] = []


class WrittenEdit(BaseModel):
    id: str
    attribute_key: str
    value_numeric: float


class EditsWritten(BaseModel):
    scenario_id: str
    feature_type: str
    edits: list[WrittenEdit] = []


class RecomputeResponse(BaseModel):
    """What the materialisation did, and the run that will recompute from it.

    ``matched_per_edit`` is per *edit* and counts what that edit's area caught
    inside the AOI — the number a drawing UI needs ("your shape caught 4
    blocks"). Zero is an answer, not an omission: it means the area caught
    nothing, which is the one thing a client has to be told.

    ``run_id`` is null for a submit that found nothing to recompute. The
    materialisation still happened and its counts still stand; handing back an
    id that ``GET /jobs/{run_id}`` would 404 is what the jobs router already
    refuses to do.
    """

    scenario_id: str
    matched_per_edit: dict[str, int] = {}
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


class DeleteScenarioResponse(BaseModel):
    """Four counts, one per trace that would otherwise outlive the scenario.

    A scenario's traces live in four tables no constraint connects, so "it is
    gone" is four separate claims and counting each is the only way to see
    that each was made.
    """

    scenario_id: str
    overrides: int = 0
    versions_retired: int = 0
    results_superseded: int = 0
    materializations: int = 0


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
    except DuplicateScenarioName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScenarioHasNoAOI as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.get("", response_model=list[ScenarioRow])
def list_scenarios_route(
    project_id: UUID | None = Query(default=None),
    aoi_id: UUID | None = Query(default=None),
) -> list[ScenarioRow]:
    """Scenarios, newest first, optionally narrowed by project and/or AOI."""
    with _refusals(), contextlib.closing(connect(api_dsn())) as conn:
        rows = list_scenarios(conn, project_id=_opt(project_id), aoi_id=_opt(aoi_id))
    return [ScenarioRow(**row) for row in rows]


@router.post("", status_code=201, response_model=ScenarioDetail)
def create_scenario_route(body: CreateScenarioBody) -> ScenarioDetail:
    """Create a named what-if and answer with it, as ``GET`` would.

    One shape for the created resource and for a later read, so a client that
    just created a scenario does not need a second request to learn what it
    looks like.
    """
    with _refusals(), contextlib.closing(connect(api_dsn())) as conn, conn:
        scenario_id = create_scenario(
            conn,
            project_id=str(body.project_id),
            name=body.name,
            aoi_id=_opt(body.aoi_id),
            description=body.description,
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
    return ScenarioDetail(**created)


@router.get("/{scenario_id}", response_model=ScenarioDetail)
def get_scenario_route(scenario_id: str) -> ScenarioDetail:
    """One scenario with its edits in ordinal order, and its override count."""
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn:
            found = get_scenario(conn, resolved)
        if found is None:
            raise HTTPException(
                status_code=404, detail=f"no scenario with id {scenario_id!r}"
            )
    return ScenarioDetail(**found)


@router.delete("/{scenario_id}", response_model=DeleteScenarioResponse)
def delete_scenario_route(scenario_id: str) -> DeleteScenarioResponse:
    """Delete a scenario and the four traces that would otherwise outlive it.

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
            removed = delete_scenario(conn, scenario_id=resolved)
    return DeleteScenarioResponse(
        scenario_id=resolved,
        overrides=removed.overrides,
        versions_retired=removed.versions_retired,
        results_superseded=removed.results_superseded,
        materializations=removed.materializations,
    )


# ── edits ────────────────────────────────────────────────────────────────


@router.post("/{scenario_id}/edits", status_code=201, response_model=EditsWritten)
def add_edits_route(scenario_id: str, body: ProposalBody) -> EditsWritten:
    """Draw one area and write the four edits of one proposal over it.

    The refusals come first, and in this order — the id, then the values, then
    the ceiling, then the geometry — so a proposal that cannot be stored costs
    no connection and leaves no half-written rule behind. The four writes then
    share one transaction: three edits of four is not a proposal, it is a
    block the edificability family skips while planned floors moves.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        require_positive(
            levels=body.levels,
            unit_size=body.unit_size,
            people_per_household=body.people_per_household,
        )
        precheck_ceilings(body.levels, body.unit_size, body.people_per_household)
        geometry_wkt = polygon_wkt(body.area)
        proposed = edits_for(body.levels, body.unit_size, body.people_per_household)

        written: list[WrittenEdit] = []
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            type_id = _feature_type_id(conn, body.type_context, body.type_slug)
            for edit in proposed:
                edit_id = add_edit(
                    conn,
                    scenario_id=resolved,
                    geometry_wkt=geometry_wkt,
                    type_id=type_id,
                    attribute_key=edit["attribute_key"],
                    value_numeric=edit["value_numeric"],
                )
                written.append(
                    WrittenEdit(
                        id=edit_id,
                        attribute_key=edit["attribute_key"],
                        value_numeric=edit["value_numeric"],
                    )
                )
    return EditsWritten(
        scenario_id=resolved,
        feature_type=f"{body.type_context}:{body.type_slug}",
        edits=written,
    )


@router.delete("/{scenario_id}/edits", status_code=204, response_class=Response)
def remove_edit_route(
    scenario_id: str, edit_id: UUID = Query(description="The edit to remove.")
) -> Response:
    """Remove one rule from this scenario.

    ⚠ The scenario in the path is checked, not decoration. ``remove_edit`` is
    keyed by ``edit_id`` alone — nothing in the data layer ties the edit to a
    scenario — so without this, ``DELETE /scenarios/<A>/edits?edit_id=<an
    edit of B>`` would delete B's rule and report success.

    The overrides the removed rule produced are **not** withdrawn here: that
    happens on the next ``recompute``, which deletes the overrides no edit
    implies any more and stales the results behind them. The alternative is a
    materialisation inside a DELETE, which would hold the scenario's write
    lock for a city-scale resolution in a request nobody expects to be slow.

    204, because there is nothing to say that the request did not already
    contain. The refusal for an unknown or foreign edit is a 404.
    """
    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            scenario = get_scenario(conn, resolved)
            if scenario is None:
                raise HTTPException(
                    status_code=404, detail=f"no scenario with id {scenario_id!r}"
                )
            if str(edit_id) not in {edit["id"] for edit in scenario["edits"]}:
                raise HTTPException(
                    status_code=404,
                    detail=f"scenario {resolved} has no edit {edit_id}",
                )
            if not remove_edit(conn, edit_id=str(edit_id)):
                # Lost a race with another remover between read and write.
                raise HTTPException(
                    status_code=404,
                    detail=f"scenario {resolved} has no edit {edit_id}",
                )
    return Response(status_code=204)


# ── recompute ────────────────────────────────────────────────────────────


@router.post(
    "/{scenario_id}/recompute", status_code=202, response_model=RecomputeResponse
)
def recompute_route(scenario_id: str) -> RecomputeResponse:
    """Materialise the scenario's edits, then enqueue the recompute.

    **Two transactions, and the boundary is a decision rather than an
    accident.** ``materialize_scenario`` takes ``FOR UPDATE`` on the scenario
    row as its first statement and holds it until the caller commits. The
    evaluation that follows computes indicators over every block of the AOI —
    tens of thousands on the Mexicali pilot. Under one transaction that lock
    would be held for the whole compute, blocking every other writer: a
    second drawn rule, a delete, another recompute. So this commits the
    materialisation, closes the connection, and enqueues on its own.

    The state in between — overrides current, results not yet recomputed — is
    one the platform already models: it is exactly what ``asset_freshness_v``
    calls stale, and what a crash between the two steps would leave anyway.
    The next recompute converges from it, because materialisation is
    idempotent and freshness is derived rather than remembered.

    The AOI is read **after** the materialisation, inside the same
    transaction, so it is read under the row lock the materialisation holds
    and is therefore the AOI the materialisation actually scoped itself to.
    Read before, it could be moved by a concurrent writer in between and the
    evaluation would be scoped to the other one.

    **The dirty set is left empty on purpose.** The materialisation's
    ``publish_scenario_version`` has just moved the version of every override
    asset this scenario touches, so a submit with no attributes and no
    features resolves the work from freshness at this scenario's partition —
    which also covers an attribute whose *last* edit was removed, something a
    list of the surviving edits' keys could not name.

    202, not 200: the materialisation is done and committed, the indicator
    recompute is accepted. ``run_id`` is what follows it.
    """
    from reimagina.orchestration.factory import get_orchestrator
    from reimagina.provenance import code_version

    with _refusals():
        resolved = _scenario_id(scenario_id)
        with contextlib.closing(connect(api_dsn())) as conn, conn:
            result = materialize_scenario(conn, scenario_id=resolved)
            scenario = get_scenario(conn, resolved)
            if scenario is None:
                # Materialisation holds this row's lock, so this is
                # unreachable. A `raise` rather than an `assert` because
                # `python -O` strips asserts and the next line would then
                # subscript None.
                raise RuntimeError(
                    f"scenario {resolved} vanished under the lock "
                    "materialisation holds on it"
                )
            aoi_id = scenario["aoi_id"]

    # ⚠ Outside `_refusals`, and that is the point. The materialisation above
    # is committed. A refusal raised from here on is about the *enqueue*, and
    # translating it with the same mapping would describe the wrong half —
    # a `ForeignKeyViolation` from `submit_run` would answer 404
    # `unknown_reference`, as if the client's ids were bad and nothing had
    # happened, after overrides had already been written.
    with (
        _enqueue_failure(resolved, result),
        contextlib.closing(connect(api_dsn())) as conn,
        conn,
    ):
        # A second connection, because the first is gone: see above.
        handle = get_orchestrator().submit_run(
            conn,
            changed_attributes=[],
            edited_features=[],
            scenario_id=resolved,
            aoi_id=aoi_id,
            code_version=code_version(),
        )
    status = handle.status.status if handle.status else "queued"
    return RecomputeResponse(
        scenario_id=resolved,
        matched_per_edit=result.matched_per_edit,
        overrides_written=result.overrides_written,
        overrides_removed=result.overrides_removed,
        results_superseded=result.results_superseded,
        run_id=None if status == "noop" else handle.run_id,
        run_status=status,
        deduplicated=handle.deduplicated,
    )


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
