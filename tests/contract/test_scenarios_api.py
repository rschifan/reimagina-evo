"""The scenario HTTP surface: what it refuses, and on which connection.

Nothing here opens a database. Every test either reads the router's own
structure or installs a fake connection and fake domain functions, which is
the shape ``tests/contract/test_jobs_api.py`` and ``tests/unit/test_scenario_cli.py``
already use. Two kinds of claim live here and nowhere else:

* **Which connection a handler runs on.** The pool rolls back on the success
  path (``apps/api/deps.py``), so a write through it answers 200 and persists
  nothing. That is a property of the route object, not of any response, and
  it is asserted directly — with a positive control proving the detector can
  see a pooled dependency where one exists.
* **Which status code each refusal becomes.** The scenario layer raises nine
  different things and two of them are raw driver errors; every one of those
  mappings is a decision this router owns, and a decision is only pinned if
  flattening it to 500 fails a test.

The round trips — a scenario that persists, a duplicate name, a recompute
that reports what it matched — are in ``tests/integration/test_scenarios_api.py``.
They need Postgres, and a Postgres-backed test in this directory would never
run: CI's ``Unit + Contract tests`` job attaches no database, and the
integration job collects only ``tests/integration``, so it would skip its way
to green in one job and not be collected by the other.
"""

from __future__ import annotations

import json
import math
from typing import Any
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

import apps.api.routers.scenarios as mod
from apps.api.app import create_app
from reimagina.scenarios.authoring import (
    CeilingExceeded,
    DeleteResult,
    DuplicateScenarioName,
    InvalidGeometry,
    MaterializeResult,
    ScenarioHasNoAOI,
    ScenarioNotFound,
)
from reimagina.scenarios.ceilings import DENSITY, CeilingBreach, max_levels_for
from reimagina.scenarios.comparison import ComparisonResult
from reimagina.scenarios.proposal import edits_for

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
AOI_ID = "22222222-2222-2222-2222-222222222222"
SCENARIO_ID = "33333333-3333-3333-3333-333333333333"
TYPE_ID = "44444444-4444-4444-4444-444444444444"
EDIT_ID = "55555555-5555-5555-5555-555555555555"
FEATURE_ID = "66666666-6666-6666-6666-666666666666"

SLUG = "edificability_block_density"

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
}
PROPOSAL = {
    "area": POLYGON,
    "levels": 6,
    "unit_size": 60,
    "people_per_household": 3.6,
}

SCENARIO_ROW = {
    "id": SCENARIO_ID,
    "project_id": PROJECT_ID,
    "aoi_id": AOI_ID,
    "name": "Centralidad norte",
    "description": None,
    "created_at": "2026-11-12T00:00:00+00:00",
    "updated_at": "2026-11-12T00:00:00+00:00",
    "materialized_at": None,
}
SCENARIO_DETAIL = {**SCENARIO_ROW, "override_count": 0, "edits": []}

MATERIALIZED = MaterializeResult(
    matched_per_edit={EDIT_ID: 4},
    overrides_written=16,
    overrides_removed=0,
    results_superseded=0,
)
DELETED = DeleteResult(
    overrides=16, versions_retired=4, results_superseded=4, materializations=1
)


# ── doubles ──────────────────────────────────────────────────────────────


class _FakeConn:
    """A psycopg2 connection's two context behaviours, kept apart.

    ``__exit__`` ends the *transaction* and leaves the socket open — which is
    the whole reason the router writes ``closing(connect(...)) as c, c:`` and
    the reason this double records "commit" and "close" as separate events.
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.events.append("rollback" if exc_type else "commit")
        return False

    def close(self) -> None:
        self.events.append("close")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def cursor(self):  # pragma: no cover - a reaching test is a bug in itself
        raise AssertionError("no handler may send SQL of its own")


@pytest.fixture
def events(monkeypatch) -> list[str]:
    """Install a fake ``connect`` and return the shared event log."""
    log: list[str] = []

    def _connect(dsn=None):
        log.append("connect")
        return _FakeConn(log)

    monkeypatch.setattr(mod, "connect", _connect)
    return log


@pytest.fixture
def no_connection(monkeypatch):
    """Fail the test if any handler opens a connection."""
    monkeypatch.setattr(
        mod, "connect", lambda dsn=None: pytest.fail("nothing may be opened")
    )


@pytest.fixture
def client() -> TestClient:
    """The real app, with no pool.

    ``initialise_pool=False`` is load-bearing: a handler that took
    ``Depends(get_db_conn)`` would raise ``PoolNotInitialised`` here, so every
    test in this file is also a check that none does.
    """
    with TestClient(
        create_app(initialise_pool=False), raise_server_exceptions=False
    ) as c:
        yield c


def _patch(monkeypatch, **targets: Any) -> None:
    for name, value in targets.items():
        monkeypatch.setattr(mod, name, value)


def _type_id(monkeypatch, type_id: str = TYPE_ID) -> None:
    """Resolve a feature type without a database."""

    class _Repo:
        def get_type_id(self, context: str, slug: str) -> str:
            return type_id

    class _Backend:
        def feature_repo(self) -> _Repo:
            return _Repo()

    monkeypatch.setattr(mod, "get_backend", lambda conn: _Backend())


def _orchestrator(monkeypatch, events: list[str] | None = None):
    """A fake ops-plane, installed over the factory the router asks."""

    class _Fake:
        def submit_run(self, conn, **kwargs):
            if events is not None:
                events.append("submit_run")
            self.kwargs = kwargs
            return _Handle()

    class _Handle:
        run_id = "run-123"
        status = None
        deduplicated = False

    fake = _Fake()
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator", lambda mode=None: fake
    )
    return fake


def _comparison(rows: list[dict] | None = None) -> ComparisonResult:
    rows = (
        rows
        if rows is not None
        else [
            {
                "feature_id": FEATURE_ID,
                "baseline": 10.0,
                "scenario": 14.0,
                "delta": 4.0,
                "state": "changed",
            }
        ]
    )
    return ComparisonResult(
        rows=rows,
        summary={
            "changed": sum(1 for r in rows if r["state"] == "changed"),
            "unchanged": sum(1 for r in rows if r["state"] == "unchanged"),
            "new": sum(1 for r in rows if r["state"] == "new"),
            "mean_delta": 4.0,
            "higher_is_better": True,
        },
    )


# ── which connection ─────────────────────────────────────────────────────


def _dependency_names(route) -> set[str]:
    """Every callable FastAPI would resolve for this route, by name."""
    names: set[str] = set()
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return names
    pending = list(dependant.dependencies)
    while pending:
        dependency = pending.pop()
        call = getattr(dependency, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
        pending.extend(dependency.dependencies)
    return names


def test_no_handler_uses_the_pooled_connection():
    """⚠ The failure is a 200 with nothing written.

    ``deps.get_db_conn`` rolls back on the *success* path, so a write through
    it reports success and persists nothing — and no response assertion can
    see that, because the response is correct. Every handler here opens its
    own connection and commits it, which is a property of the route object.
    """
    offenders = {
        (route.methods and sorted(route.methods), route.path): sorted(names)
        for route in mod.router.routes
        if "get_db_conn" in (names := _dependency_names(route))
    }
    assert not offenders, (
        "these handlers depend on the API's pooled connection, which rolls "
        f"back on success, so their writes would not persist: {offenders}"
    )


def test_the_pool_detector_sees_a_pooled_dependency_where_one_exists():
    """The negative control for the test above, which is otherwise vacuous.

    ``_dependency_names`` returning an empty set for every route would make
    the assertion pass for a router that used nothing but the pool. ``/aois``
    does take it, legitimately — it only reads — so it is the control that
    proves the detector works.
    """
    import apps.api.routers.aois as aois

    found = {name for route in aois.router.routes for name in _dependency_names(route)}
    assert "get_db_conn" in found


def test_the_router_is_mounted_on_the_app(client):
    """Read off the published schema: a router nobody included serves nothing."""
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {
        "/scenarios",
        "/scenarios/{scenario_id}",
        "/scenarios/{scenario_id}/edits",
        "/scenarios/{scenario_id}/recompute",
        "/scenarios/{scenario_id}/comparison",
    } <= paths


WRITES = ["create", "edits", "recompute", "delete_edit", "delete"]


@pytest.mark.parametrize("route", WRITES)
def test_every_write_commits_and_closes_its_own_connection(
    client, events, monkeypatch, route
):
    """One connection per write, committed, then closed.

    Committed because nothing in ``reimagina.scenarios`` commits — the caller
    owns the transaction. Closed because the API is a long-lived process and
    psycopg2's context manager ends the transaction without closing the
    socket, which is how ``POST /jobs`` once leaked a backend per request.
    """
    _patch(
        monkeypatch,
        create_scenario=lambda c, **kw: SCENARIO_ID,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL, edits=[{**EDIT_ROW}]),
        add_edit=lambda c, **kw: EDIT_ID,
        remove_edit=lambda c, **kw: True,
        materialize_scenario=lambda c, **kw: MATERIALIZED,
        delete_scenario=lambda c, **kw: DELETED,
    )
    _type_id(monkeypatch)
    _orchestrator(monkeypatch)

    response = _call(client, route)

    assert response.status_code < 300, response.text
    # One connection for the write; recompute opens a second for the enqueue.
    assert events.count("connect") == events.count("close")
    assert events.count("connect") >= 1
    assert "rollback" not in events
    assert events[-1] == "close"
    assert events[events.index("commit") + 1] == "close"


EDIT_ROW = {
    "id": EDIT_ID,
    "ordinal": 1,
    "type_id": TYPE_ID,
    "attribute_key": "floors",
    "value_numeric": 6.0,
    "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
    "created_at": "2026-11-12T00:00:00+00:00",
}


def _call(client: TestClient, route: str, scenario_id: str = SCENARIO_ID):
    """One request per named route, with a body that would otherwise succeed."""
    if route == "create":
        return client.post(
            "/scenarios", json={"project_id": PROJECT_ID, "name": "Centralidad norte"}
        )
    if route == "list":
        return client.get("/scenarios")
    if route == "show":
        return client.get(f"/scenarios/{scenario_id}")
    if route == "edits":
        return client.post(f"/scenarios/{scenario_id}/edits", json=PROPOSAL)
    if route == "delete_edit":
        return client.delete(f"/scenarios/{scenario_id}/edits?edit_id={EDIT_ID}")
    if route == "recompute":
        return client.post(f"/scenarios/{scenario_id}/recompute")
    if route == "comparison":
        return client.get(f"/scenarios/{scenario_id}/comparison?indicator_slug={SLUG}")
    if route == "delete":
        return client.delete(f"/scenarios/{scenario_id}")
    raise AssertionError(route)


ALL_ROUTES = WRITES + ["list", "show", "comparison"]


# ── recompute is two transactions ────────────────────────────────────────


def test_recompute_materialises_commits_and_closes_before_it_evaluates(
    client, events, monkeypatch
):
    """⚠ One transaction would hold a scenario write lock across the compute.

    ``materialize_scenario`` takes ``FOR UPDATE`` on the scenario row as its
    first statement and holds it until the caller commits. Enqueueing the
    evaluation inside that transaction would keep the lock for the whole
    city-scale recompute, blocking every other writer — a second edit, a
    delete, another recompute. The boundary is therefore asserted as an
    order, including the ``close``: the materialisation's connection is gone
    before the evaluation's is opened.
    """
    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL),
        materialize_scenario=lambda c, **kw: (
            events.append("materialize"),
            MATERIALIZED,
        )[1],
    )
    fake = _orchestrator(monkeypatch, events)

    response = client.post(f"/scenarios/{SCENARIO_ID}/recompute")

    assert response.status_code == 202, response.text
    assert events == [
        "connect",
        "materialize",
        "commit",
        "close",
        "connect",
        "submit_run",
        "commit",
        "close",
    ]
    assert fake.kwargs["scenario_id"] == SCENARIO_ID
    assert fake.kwargs["aoi_id"] == AOI_ID
    # Empty on purpose: materialisation has just published the versions, so
    # freshness at this scenario's partition resolves the work — including an
    # attribute whose last edit was removed, which no list of keys could name.
    assert fake.kwargs["changed_attributes"] == []
    assert fake.kwargs["edited_features"] == []


def test_recompute_reports_what_it_matched(client, events, monkeypatch):
    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL),
        materialize_scenario=lambda c, **kw: MATERIALIZED,
    )
    _orchestrator(monkeypatch)

    body = client.post(f"/scenarios/{SCENARIO_ID}/recompute").json()

    assert body["matched_per_edit"] == {EDIT_ID: 4}
    assert body["overrides_written"] == 16
    assert body["overrides_removed"] == 0
    assert body["results_superseded"] == 0
    assert body["run_id"] == "run-123"


def test_recompute_reads_the_aoi_under_the_lock_materialisation_holds(
    client, events, monkeypatch
):
    """The scope of the evaluation is read *after* the materialisation.

    Reading it first would read it outside ``_lock_scenario``'s ``FOR
    UPDATE``, so a concurrent write could move the AOI between the read and
    the lock and the evaluation would be scoped to the one the
    materialisation did not use.
    """
    order: list[str] = []
    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: (order.append("read"), dict(SCENARIO_DETAIL))[1],
        materialize_scenario=lambda c, **kw: (
            order.append("materialize"),
            MATERIALIZED,
        )[1],
    )
    _orchestrator(monkeypatch)

    client.post(f"/scenarios/{SCENARIO_ID}/recompute")

    assert order == ["materialize", "read"]


# ── the four edits of one proposal ───────────────────────────────────────


def test_the_edits_route_delegates_the_four_edits_to_the_domain(
    client, events, monkeypatch
):
    """⚠ Three numbers make four edits, and that rule has one home.

    ``--levels`` writes both ``floors`` and ``num_levels``; a surface that
    re-derived the list would re-introduce the spec's named failure — planned
    floors frozen on the baseline while edificability moves. The assertion is
    therefore that the router *calls* ``proposal.edits_for``, not that it
    happens to produce the same four keys today: a local reimplementation
    passes the second test and fails this one.
    """
    calls: list[dict] = []
    sentinel = [{"attribute_key": "num_levels", "value_numeric": 99.0}]
    _patch(
        monkeypatch,
        edits_for=lambda *a, **kw: sentinel,
        add_edit=lambda c, **kw: (calls.append(kw), EDIT_ID)[1],
    )
    _type_id(monkeypatch)

    response = client.post(f"/scenarios/{SCENARIO_ID}/edits", json=PROPOSAL)

    assert response.status_code == 201, response.text
    assert [(c["attribute_key"], c["value_numeric"]) for c in calls] == [
        ("num_levels", 99.0)
    ]


def test_one_proposal_writes_all_four_edits_over_one_area_in_one_transaction(
    client, events, monkeypatch
):
    """The policy outcome, with the expectation derived from the domain.

    One commit, not four: ``add_edit`` raising on the third would otherwise
    leave ``floors`` and ``num_levels`` written and ``unit_size`` not, which
    is a block the edificability family skips while planned floors moves.
    """
    calls: list[dict] = []
    _patch(monkeypatch, add_edit=lambda c, **kw: (calls.append(kw), EDIT_ID)[1])
    _type_id(monkeypatch)

    response = client.post(f"/scenarios/{SCENARIO_ID}/edits", json=PROPOSAL)

    assert response.status_code == 201, response.text
    assert [(c["attribute_key"], c["value_numeric"]) for c in calls] == [
        (e["attribute_key"], e["value_numeric"]) for e in edits_for(6.0, 60.0, 3.6)
    ]
    assert {c["geometry_wkt"] for c in calls} == {"POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"}
    assert {c["type_id"] for c in calls} == {TYPE_ID}
    assert events.count("commit") == 1
    assert len(response.json()["edits"]) == 4


def test_the_drawn_area_may_be_a_feature_collection(client, events, monkeypatch):
    """A drawing tool exports a FeatureCollection; several strokes are one area."""
    calls: list[dict] = []
    _patch(monkeypatch, add_edit=lambda c, **kw: (calls.append(kw), EDIT_ID)[1])
    _type_id(monkeypatch)

    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits",
        json={
            **PROPOSAL,
            "area": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {}, "geometry": POLYGON}
                ],
            },
        },
    )

    assert response.status_code == 201, response.text
    assert calls[0]["geometry_wkt"].startswith("POLYGON ((0 0")


# ── refusals: one status each ────────────────────────────────────────────


def test_a_duplicate_name_is_409_not_500(client, events, monkeypatch):
    def _raise(conn, **kwargs):
        raise DuplicateScenarioName("scenario 'N' already exists in this project")

    _patch(monkeypatch, create_scenario=_raise)

    response = client.post("/scenarios", json={"project_id": PROJECT_ID, "name": "N"})

    assert response.status_code == 409
    assert "already exists" in json.dumps(response.json())
    # The failed insert is not committed as a no-op transaction.
    assert "rollback" in events


UNKNOWN_SCENARIO_ROUTES = [
    "show",
    "edits",
    "delete_edit",
    "recompute",
    "comparison",
    "delete",
]


@pytest.mark.parametrize("route", UNKNOWN_SCENARIO_ROUTES)
def test_an_unknown_scenario_is_404_on_every_route(client, events, monkeypatch, route):
    """One vocabulary, one status.

    ``ScenarioNotFound`` covers an unknown scenario, an unknown AOI and an
    unknown indicator slug, because the answer is the same in every case:
    the thing you named is not there.
    """

    def _raise(*args, **kwargs):
        raise ScenarioNotFound(f"no scenario with id {SCENARIO_ID}")

    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: None,
        add_edit=_raise,
        remove_edit=_raise,
        materialize_scenario=_raise,
        delete_scenario=_raise,
        compare=_raise,
    )
    _type_id(monkeypatch)

    response = _call(client, route)

    assert response.status_code == 404, response.text


def test_an_unknown_indicator_slug_is_404(client, events, monkeypatch):
    def _raise(conn, **kwargs):
        raise ScenarioNotFound("no indicator in the catalog with slug 'nope'")

    _patch(monkeypatch, compare=_raise)

    response = client.get(f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug=nope")

    assert response.status_code == 404


def test_a_defect_under_the_scenario_layer_is_not_a_404(client, events, monkeypatch):
    """⚠ ``except LookupError`` would make a bug look like a typo.

    ``ScenarioNotFound`` subclasses ``LookupError`` so that callers written
    against the old contract keep working — which means catching the broad
    class also catches the ``KeyError``/``IndexError`` of a genuine defect
    anywhere under ``authoring`` or ``comparison``. A 404 for that is a bug
    reported as the client's mistake, and it is the hardest kind to find.
    """

    def _raise(conn, **kwargs):
        raise KeyError("higher_is_better")

    _patch(monkeypatch, compare=_raise)

    response = client.get(f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug={SLUG}")

    assert response.status_code == 500


def test_a_scenario_without_an_aoi_is_409(client, events, monkeypatch):
    """Nothing the client sent is wrong: ``aoi_id`` is ``ON DELETE SET NULL``.

    409 rather than 400 or 404 — the scenario exists and the request is well
    formed; it conflicts with the current state of the resource, and the fix
    is to set an AOI rather than to re-send anything.
    """

    def _raise(conn, **kwargs):
        raise ScenarioHasNoAOI(f"scenario {SCENARIO_ID} has no area of interest")

    _patch(monkeypatch, materialize_scenario=_raise)

    response = client.post(f"/scenarios/{SCENARIO_ID}/recompute")

    assert response.status_code == 409
    assert "area of interest" in json.dumps(response.json())


def test_an_unparseable_drawn_area_is_400(client, no_connection, monkeypatch):
    """Well-formed JSON, malformed GeoJSON — refused before any connection."""
    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits",
        json={**PROPOSAL, "area": {"type": "Polygon"}},
    )

    assert response.status_code == 400
    detail = json.dumps(response.json())
    assert "coordinates" in detail  # the useful half of shapely's complaint
    assert "Traceback" not in detail


def test_an_area_with_no_polygon_is_400(client, no_connection):
    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits",
        json={**PROPOSAL, "area": {"type": "Point", "coordinates": [0.0, 0.0]}},
    )

    assert response.status_code == 400
    assert "polygon" in json.dumps(response.json()).lower()


def test_a_geometry_postgis_cannot_repair_is_400(client, events, monkeypatch):
    """The other half of ``InvalidGeometry``: the one ``add_edit`` raises."""

    def _raise(conn, **kwargs):
        raise InvalidGeometry("geometry has no polygonal area after repair")

    _patch(monkeypatch, add_edit=_raise)
    _type_id(monkeypatch)

    response = client.post(f"/scenarios/{SCENARIO_ID}/edits", json=PROPOSAL)

    assert response.status_code == 400


def test_a_ceiling_breach_at_materialisation_is_422(client, events, monkeypatch):
    def _raise(conn, **kwargs):
        raise CeilingExceeded([CeilingBreach(FEATURE_ID, DENSITY, 2000.0, 2166.0)])

    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL),
        materialize_scenario=_raise,
    )

    response = client.post(f"/scenarios/{SCENARIO_ID}/recompute")

    assert response.status_code == 422
    detail = response.json()["detail"]
    # Which blocks, and how far over — the planner's next action is to lower a
    # number, and they need to know where.
    assert detail["breaches"][0]["feature_id"] == FEATURE_ID
    assert detail["breaches"][0]["indicator_slug"] == DENSITY
    assert detail["breach_count"] == 1


def test_the_ceiling_refusal_does_not_list_every_block_of_the_city(
    client, events, monkeypatch
):
    """19,058 breaches is not an error message. The count is exact; the list is capped."""
    breaches = [
        CeilingBreach(str(uuid4()), DENSITY, 2000.0, 2166.0) for _ in range(200)
    ]

    def _raise(conn, **kwargs):
        raise CeilingExceeded(breaches)

    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL),
        materialize_scenario=_raise,
    )

    detail = client.post(f"/scenarios/{SCENARIO_ID}/recompute").json()["detail"]

    assert detail["breach_count"] == 200
    assert 0 < len(detail["breaches"]) <= 20


def test_an_unknown_feature_type_is_404(client, events, monkeypatch):
    """``get_type_id`` raises a bare ``ValueError`` for a slug nobody declared."""

    class _Repo:
        def get_type_id(self, context, slug):
            raise ValueError(f"Unknown feature type: {context!r}/{slug!r}")

    class _Backend:
        def feature_repo(self):
            return _Repo()

    _patch(monkeypatch, get_backend=lambda conn: _Backend())

    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits", json={**PROPOSAL, "type_slug": "nope"}
    )

    assert response.status_code == 404
    assert "nope" in json.dumps(response.json())


def test_an_unknown_reference_in_the_body_is_404_naming_its_field(
    client, events, monkeypatch
):
    """A well-formed ``project_id`` that names no project.

    ``app_core.scenario.project_id`` is NOT NULL with an FK, so this arrived
    as a raw ``ForeignKeyViolation`` and ``errors.database_error_handler``
    turned it into a 500 — a 500 for a client's typo. The field is read from
    the constraint's own name rather than echoed from the driver message,
    which is internal and must not reach a client.
    """

    def _raise(conn, **kwargs):
        exc = psycopg2.errors.ForeignKeyViolation(
            'insert or update on table "scenario" violates foreign key constraint'
        )
        raise exc

    _patch(monkeypatch, create_scenario=_raise)

    response = client.post("/scenarios", json={"project_id": PROJECT_ID, "name": "N"})

    assert response.status_code == 404
    assert "violates foreign key" not in json.dumps(response.json())


# ── values: fields, not flags ────────────────────────────────────────────


@pytest.mark.parametrize("field", ["levels", "unit_size", "people_per_household"])
@pytest.mark.parametrize("value", [0, -3])
def test_a_degenerate_value_is_400_naming_the_json_field(
    client, no_connection, field, value
):
    """⚠ The HTTP mirror of ``test_the_domain_messages_name_fields_not_flags``.

    ``proposal`` states the breach in field names and says nothing about how
    to act, because a 400 that tells an API client to lower ``--levels`` and
    run ``scenario recompute`` describes a command line the client does not
    have. The field it names must be the field the client sent, so it is
    asserted against the request model rather than against a literal.
    """
    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits", json={**PROPOSAL, field: value}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["field"] == field
    assert field in mod.ProposalBody.model_fields
    text = json.dumps(response.json())
    assert "--" not in text
    assert "scenario recompute" not in text


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_value_is_refused_before_it_reaches_the_database(
    client, no_connection, literal
):
    """⚠ ``value_numeric`` is a bare ``double precision``.

    Postgres stores ``NaN`` and ``Infinity`` in a float column without
    complaint, ``check_ceilings`` skips them (its kernels cannot breach on a
    non-positive or non-finite input), and ``json.loads`` — which Starlette
    uses — parses both literals happily. So nothing between the wire and the
    override row refuses them except this.
    """
    body = (
        '{"area": %s, "levels": %s, "unit_size": 60, "people_per_household": 3.6}'
        % (json.dumps(POLYGON), literal)
    )

    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["field"] == "levels"


def test_the_json_parser_really_does_accept_those_literals():
    """The negative control for the test above.

    If ``json.loads`` refused ``NaN``, the 400 would be coming from the
    parser and the test would be pinning nothing about this router.
    """
    parsed = json.loads('{"levels": NaN, "u": Infinity}')
    assert math.isnan(parsed["levels"])
    assert math.isinf(parsed["u"])


def test_a_density_breach_is_422_with_the_allowance_the_form_needs(
    client, no_connection
):
    """Refused before a connection, because the bound needs no block.

    422 rather than 400: every number parses and is individually plausible;
    what cannot be processed is the combination, against a bound the catalog
    declares. The response carries ``allowed_levels`` so a form can clamp its
    own slider instead of re-deriving the rule.
    """
    allowed = math.floor(max_levels_for(60.0))

    response = client.post(
        f"/scenarios/{SCENARIO_ID}/edits", json={**PROPOSAL, "levels": allowed + 1}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["allowed_levels"] == allowed
    assert DENSITY in detail["message"]
    # The domain's own sentence survives, including what it cannot check.
    assert "housing" in detail["message"] and "population" in detail["message"]
    text = json.dumps(response.json())
    assert "--" not in text
    assert "scenario recompute" not in text


def test_the_boundary_is_the_catalog_bound_at_every_unit_size(
    client, events, monkeypatch
):
    """⚠ One unit size cannot tell a derived bound from a hardcoded 12.

    ``max_levels_for`` is ``valid_max × unit_size / 10000``, so at 60 m² the
    honest answer and a constant agree exactly. 30 m² allows 6 storeys and
    120 allows 24.
    """
    _patch(monkeypatch, add_edit=lambda c, **kw: EDIT_ID)
    _type_id(monkeypatch)

    for unit_size in (30.0, 60.0, 120.0):
        allowed = math.floor(max_levels_for(unit_size))
        ok = client.post(
            f"/scenarios/{SCENARIO_ID}/edits",
            json={**PROPOSAL, "levels": allowed, "unit_size": unit_size},
        )
        over = client.post(
            f"/scenarios/{SCENARIO_ID}/edits",
            json={**PROPOSAL, "levels": allowed + 1, "unit_size": unit_size},
        )
        assert ok.status_code == 201, (unit_size, ok.text)
        assert over.status_code == 422, (unit_size, over.text)


# ── malformed ids: 404 in a path, 422 in a filter ────────────────────────

BAD = "not-a-uuid"


@pytest.mark.parametrize("route", UNKNOWN_SCENARIO_ROUTES)
def test_a_malformed_scenario_id_is_404_without_touching_the_database(
    client, no_connection, route
):
    """⚠ The typo must not become SQL.

    ``app_core.scenario.id`` is ``uuid``, so a malformed id reached Postgres,
    came back a ``psycopg2.DataError`` and landed on the 500 handler — which
    also aborted the request's transaction on the way out. 404 rather than
    422 because this id *names* the resource rather than filtering one, and
    an id no scenario can have names no scenario: the same answer, and the
    same reasoning, as ``/jobs/{run_id}`` in ``test_malformed_ids.py``.
    """
    response = _call(client, route, scenario_id=BAD)

    assert response.status_code == 404, response.text


FILTERS = [
    ("GET", f"/scenarios?project_id={BAD}"),
    ("GET", f"/scenarios?aoi_id={BAD}"),
    (
        "GET",
        f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug={SLUG}&aoi_id={BAD}",
    ),
    ("DELETE", f"/scenarios/{SCENARIO_ID}/edits?edit_id={BAD}"),
]


@pytest.mark.parametrize(("method", "url"), FILTERS, ids=[u for _, u in FILTERS])
def test_a_malformed_id_in_a_filter_is_422(client, no_connection, method, url):
    """The other half of the convention: a filter is rejected at the boundary."""
    response = client.request(method, url)

    assert response.status_code == 422, response.text


def test_a_malformed_body_reference_is_422(client, no_connection):
    response = client.post("/scenarios", json={"project_id": BAD, "name": "N"})

    assert response.status_code == 422


# ── one edit belongs to one scenario ─────────────────────────────────────


def test_an_edit_of_another_scenario_cannot_be_removed_through_this_one(
    client, events, monkeypatch
):
    """⚠ ``remove_edit`` is keyed by ``edit_id`` alone.

    Nothing in the data layer ties the edit to the scenario in the path, so
    without this check ``DELETE /scenarios/<A>/edits?edit_id=<an edit of B>``
    would delete B's rule and report success — and the scenario id in the URL
    would be decoration.
    """
    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL, edits=[]),
        remove_edit=lambda c, **kw: pytest.fail("must not delete another's edit"),
    )

    response = client.delete(f"/scenarios/{SCENARIO_ID}/edits?edit_id={EDIT_ID}")

    assert response.status_code == 404


def test_removing_an_edit_of_this_scenario_is_204(client, events, monkeypatch):
    removed: list[dict] = []
    _patch(
        monkeypatch,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL, edits=[dict(EDIT_ROW)]),
        remove_edit=lambda c, **kw: (removed.append(kw), True)[1],
    )

    response = client.delete(f"/scenarios/{SCENARIO_ID}/edits?edit_id={EDIT_ID}")

    assert response.status_code == 204
    assert response.content == b""
    assert removed == [{"edit_id": EDIT_ID}]


# ── comparison ───────────────────────────────────────────────────────────


def test_the_comparison_reports_its_three_states_and_no_total(
    client, events, monkeypatch
):
    """⚠ There is no total, and inventing one would be a lie.

    ``changed + unchanged + new == len(rows)`` always, but that sum is *less*
    than the AOI's block count whenever the baseline indicator has gaps: a
    feature with no numeric value on either side is absent from the rows by
    design. A ``total`` here would be read as the territory's size. If the
    frontend needs the gap it is a ``no_value`` key counted deliberately,
    not a subtraction nobody can see.
    """
    rows = [
        {
            "feature_id": FEATURE_ID,
            "baseline": 10.0,
            "scenario": 14.0,
            "delta": 4.0,
            "state": "changed",
        },
        {
            "feature_id": str(uuid4()),
            "baseline": None,
            "scenario": 7.0,
            "delta": None,
            "state": "new",
        },
    ]
    _patch(monkeypatch, compare=lambda c, **kw: _comparison(rows))

    body = client.get(
        f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug={SLUG}"
    ).json()

    summary = body["summary"]
    assert (summary["changed"], summary["unchanged"], summary["new"]) == (1, 0, 1)
    assert summary["higher_is_better"] is True
    assert body["row_count"] == 2
    assert (
        summary["changed"] + summary["unchanged"] + summary["new"] == body["row_count"]
    )
    assert "total" not in json.dumps(body)
    assert "total" not in mod.ComparisonSummary.model_fields
    assert "total" not in mod.ComparisonResponse.model_fields


def test_the_comparison_truncates_rows_without_truncating_the_counts(
    client, events, monkeypatch
):
    rows = [
        {
            "feature_id": str(uuid4()),
            "baseline": 1.0,
            "scenario": 2.0,
            "delta": 1.0,
            "state": "changed",
        }
        for _ in range(5)
    ]
    _patch(monkeypatch, compare=lambda c, **kw: _comparison(rows))

    body = client.get(
        f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug={SLUG}&limit=2"
    ).json()

    assert len(body["rows"]) == 2
    assert body["row_count"] == 5
    assert body["rows_truncated"] is True
    assert body["summary"]["changed"] == 5


def test_the_comparison_returns_every_row_by_default(client, events, monkeypatch):
    """No silent truncation: a half-drawn territory read as the whole one."""
    rows = [
        {
            "feature_id": str(uuid4()),
            "baseline": 1.0,
            "scenario": 1.0,
            "delta": 0.0,
            "state": "unchanged",
        }
        for _ in range(300)
    ]
    _patch(monkeypatch, compare=lambda c, **kw: _comparison(rows))

    body = client.get(
        f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug={SLUG}"
    ).json()

    assert len(body["rows"]) == 300
    assert body["rows_truncated"] is False


def test_the_comparison_passes_its_aoi_scope_through(client, events, monkeypatch):
    seen: dict = {}
    _patch(
        monkeypatch,
        compare=lambda c, **kw: (seen.update(kw), _comparison())[1],
    )

    client.get(
        f"/scenarios/{SCENARIO_ID}/comparison?indicator_slug={SLUG}&aoi_id={AOI_ID}"
    )

    assert seen == {
        "scenario_id": SCENARIO_ID,
        "indicator_slug": SLUG,
        "aoi_id": AOI_ID,
    }


# ── no credential in any response ────────────────────────────────────────

PASSWORD = "s3cr3t-pa55word"
DSN_WITH_PASSWORD = f"postgresql://reimagina:{PASSWORD}@db.internal:5432/reimagina_app"


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_no_response_carries_the_dsn_or_its_password(
    client, events, monkeypatch, route
):
    """⚠ Task 8 found ``recompute`` printing a full DSN, password included.

    The CLI's fix was a recursive mask in its own printer. These responses are
    JSON built here, so that mask protects nothing: the only thing that keeps
    the credential out is that no payload carries the DSN at all. The CLI's
    own ``{"dsn": dsn}`` key is precisely the habit this asserts against.
    """
    monkeypatch.setattr(mod, "api_dsn", lambda: DSN_WITH_PASSWORD)
    _patch(
        monkeypatch,
        create_scenario=lambda c, **kw: SCENARIO_ID,
        get_scenario=lambda c, sid: dict(SCENARIO_DETAIL, edits=[dict(EDIT_ROW)]),
        list_scenarios=lambda c, **kw: [dict(SCENARIO_ROW)],
        add_edit=lambda c, **kw: EDIT_ID,
        remove_edit=lambda c, **kw: True,
        materialize_scenario=lambda c, **kw: MATERIALIZED,
        delete_scenario=lambda c, **kw: DELETED,
        compare=lambda c, **kw: _comparison(),
    )
    _type_id(monkeypatch)
    _orchestrator(monkeypatch)

    response = _call(client, route)

    assert response.status_code < 300, response.text
    assert PASSWORD not in response.text
    assert "postgresql://" not in response.text
    assert "dsn" not in response.text.lower()


def test_a_refusal_carries_no_dsn_either(client, monkeypatch):
    """The failure path, where a driver message would be echoed if any were."""
    monkeypatch.setattr(mod, "api_dsn", lambda: DSN_WITH_PASSWORD)

    def _raise(dsn=None):
        raise psycopg2.OperationalError(
            f'could not connect to server: "{DSN_WITH_PASSWORD}"'
        )

    monkeypatch.setattr(mod, "connect", _raise)

    response = client.get(f"/scenarios/{SCENARIO_ID}")

    assert response.status_code == 503
    assert PASSWORD not in response.text


# ── the dependency direction ─────────────────────────────────────────────


def test_the_router_does_not_import_the_cli():
    """⚠ The same mistake as a backend importing an app, in the other direction.

    ``proposal.py`` exists so this router can reach the rules of a proposal
    without importing ``apps.cli``, whose helpers add a command line's
    dialect to them. A router that imported it would ship ``--levels`` to an
    HTTP client and make a deployable app depend on another deployable app.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(mod.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # The imports, not the prose: naming the CLI in a docstring is how the two
    # surfaces stay legible to each other.
    assert not [name for name in imported if name.split(".")[:2] == ["apps", "cli"]]
    assert "reimagina.scenarios.proposal" in imported
