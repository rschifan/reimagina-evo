"""The read path — where a run's status comes from, and what it costs to get it.

Hatchet's status enum has five values and cannot say `partial` or `skipped`.
Neither of those needs a second status store, and both used to have one:

* `partial` is a *roll-up* of unit outcomes, not a stored fact. `runs.get`
  reports each task's own status, including CANCELLED, so `overall_status` —
  the same function the `sync` implementation uses — derives it on read.
* `skipped` is invisible *in the status enum* — a unit skipped for being
  non-dirty reports COMPLETED — but not invisible in the engine. It emits a
  `SKIPPED` task event, and `runs.get` returns those alongside the tasks. The
  finalizer that used to publish `ctx.was_skipped` is deleted; it existed only
  because `V1TaskEvent.task_id` looked unjoinable, and it is not.

These pin the reading half. Engine behaviour itself belongs in the qualification
suite — a unit test that mocked the engine into agreeing with us proves nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hatchet_sdk.clients.rest.models.v1_task_event_type import V1TaskEventType
from hatchet_sdk.clients.rest.models.v1_task_status import V1TaskStatus

from reimagina.orchestration.hatchet_orchestrator import (
    HatchetOrchestrator,
    HatchetUnavailable,
    _skipped_task_ids,
    _units_of,
)


def _details(
    status: str,
    output: object = None,
    units: list | None = None,
    skipped: list[str] | None = None,
):
    """Shaped like `V1WorkflowRunDetails`: `.run`, `.shape`, `.tasks`, `.task_events`.

    ⚠ **`shape` carries the scaffolding too**, and the previous version of this
    helper did not — which is exactly how a real defect shipped. The engine's
    `GetWorkflowShape` joins every Job of the workflow version, so `plan` and,
    for runs created before the finalizers were deleted, both `-on-*` jobs
    appear alongside the indicators under the names the SDK registered them
    with. Verified against a live run.

    The finalizers are gone from the current graph, but the engine retains runs
    for 720 h — so a replica reading a run submitted before the change still
    sees them, and the structural filter still has to hold. That is why they
    stay in this fixture.

    ⚠ `skipped` names units by slug and emits a real `SKIPPED` **task event**
    against their external ids, which is how the engine reports a skip. It used
    to be `output={"skipped": [...]}`, a finalizer's return value; that hook is
    gone, because the event was always there to read.
    """
    units = units or []
    skipped = skipped or []
    scaffolding = [
        ("plan", "COMPLETED", None),
        ("summarise_success-on-success", "COMPLETED", None),
        ("summarise_failure-on-failure", "COMPLETED", None),
    ]
    rows = [*scaffolding, *units]
    external = {name: f"ext-{i}" for i, (name, _s, _e) in enumerate(rows)}
    return SimpleNamespace(
        # ⚠ Statuses are the real `V1TaskStatus` members, not the uppercase
        # strings they used to be. Callers still pass strings for readability
        # and this is the one place they are coerced — which also makes a typo
        # (`"COMPLETE"`) raise here, at fixture build, instead of quietly
        # missing every lookup and reading as `queued`.
        run=SimpleNamespace(status=V1TaskStatus(status), output=output),
        shape=[
            SimpleNamespace(task_name=name, task_external_id=external[name])
            for name, _s, _e in rows
        ],
        tasks=[
            SimpleNamespace(
                task_external_id=external[name],
                status=V1TaskStatus(status_),
                error_message=err,
            )
            for name, status_, err in rows
        ],
        task_events=[
            # ⚠ `task_id` carries the *external* id. The field is misnamed in
            # the API — `StrictStr` next to `V1TaskSummary.task_id`'s
            # `StrictInt` — and that is what hid this signal for two rewrites.
            # Verified live: 489 events, every one joining by external id.
            SimpleNamespace(event_type=V1TaskEventType.SKIPPED, task_id=external[slug])
            for slug in skipped
            if slug in external
        ],
    )


def _orchestrator():
    """A plain orchestrator.

    ⚠ No catalog allowlist. Units are selected structurally — scaffolding is
    `plan` and anything the SDK suffixed `-on-success`/`-on-failure` — so a
    fixture no longer has to enumerate the slugs it invents. The allowlist this
    replaces silently *dropped* unrecognised units, which turned a failed run
    into a successful one whenever an API replica ran a graph behind its
    workers.
    """
    return HatchetOrchestrator()


@pytest.fixture
def patched(monkeypatch):
    """Swap the engine for a canned answer; everything else is real code."""

    def _install(details):
        import reimagina.orchestration.hatchet_app as app

        monkeypatch.setattr(
            app,
            "hatchet",
            SimpleNamespace(runs=SimpleNamespace(get=lambda _id: details)),
        )

    return _install


# ── The claim the redesign rests on ──────────────────────────────────────────


def test_partial_is_rolled_up_from_native_task_outcomes(patched):
    """A FAILED run with one surviving unit reports `partial`.

    No finalizer verdict involved: the engine says one task COMPLETED and one
    FAILED, and `overall_status` — shared with `sync` and Track A — turns that
    into `partial`. The enum never had to express it.
    """
    patched(
        _details(
            "FAILED",
            units=[("a", "COMPLETED", None), ("b", "FAILED", "RuntimeError: boom")],
        )
    )
    run = _orchestrator().get_run("r1")

    assert run.status == "partial"
    assert {u.indicator: u.status for u in run.units} == {
        "a": "succeeded",
        "b": "failed",
    }
    assert run.units[1].detail == "RuntimeError: boom"


def test_a_cancelled_dependent_is_read_not_inferred(patched):
    """The engine states CANCELLED; nothing has to deduce it.

    This used to be inferred from "no error, not skipped, and `task_output`
    raised" — and the inference was wrong once, reporting a composite that never
    ran as `succeeded`, which is a value that was never computed presented as a
    good one.
    """
    patched(
        _details(
            "FAILED",
            units=[
                ("doomed", "FAILED", "boom"),
                ("dependent", "CANCELLED", None),
                ("independent", "COMPLETED", None),
            ],
        )
    )
    run = _orchestrator().get_run("r1")

    assert {u.indicator: u.status for u in run.units} == {
        "doomed": "failed",
        "dependent": "cancelled",
        "independent": "succeeded",
    }
    assert run.status == "partial"  # `independent` survived


def test_skipped_survives_a_completed_task_status(patched):
    """The one fact the engine will not hand back: a skip reads as COMPLETED."""
    patched(
        _details(
            "COMPLETED",
            skipped=["not_dirty"],
            units=[("computed", "COMPLETED", None), ("not_dirty", "COMPLETED", None)],
        )
    )
    run = _orchestrator().get_run("r1")

    assert {u.indicator: u.status for u in run.units} == {
        "computed": "succeeded",
        "not_dirty": "skipped",
    }


def test_an_all_skipped_run_is_not_reported_as_success(patched):
    """Nothing ran, so the run did not achieve its purpose.

    ⚠ Behaviour change. The deleted in-finalizer roll-up folded "no failures
    seen" straight to `succeeded`, so an all-skipped run was `succeeded` here
    and `failed` under `sync` — the same product API answering two ways. There
    is one roll-up now, and `port.overall_status` is explicit that all-skipped
    is `failed`.
    """
    patched(
        _details(
            "COMPLETED",
            skipped=["a", "b"],
            units=[("a", "COMPLETED", None), ("b", "COMPLETED", None)],
        )
    )
    run = _orchestrator().get_run("r1")

    assert [u.status for u in run.units] == ["skipped", "skipped"]
    assert run.status == "failed"


# ── While the run is still open ──────────────────────────────────────────────


def test_an_in_flight_run_reports_the_engine_status_not_a_rollup(patched):
    """⚠ The reason the status branch cannot be collapsed.

    `overall_status` is a *terminal* roll-up with no `running` case: given
    `{"running"}` it falls through every branch and returns **failed**. Rolling
    up an open run would report every one of them as failed.
    """
    patched(
        _details(
            "RUNNING",
            units=[("compactness", "COMPLETED", None), ("density", "RUNNING", None)],
        )
    )
    run = _orchestrator().get_run("r1")

    assert run.status == "running"
    assert {u.indicator: u.status for u in run.units} == {
        "compactness": "succeeded",
        "density": "running",
    }


def test_a_unit_with_no_task_row_yet_is_queued_not_missing(patched):
    """`shape` is the declared graph, so the unit list is stable from the start.

    Building it from `tasks` alone meant units blinked into existence as the
    engine created rows, and a caller polling early saw a shorter run than the
    one it submitted.
    """
    details = _details("RUNNING", units=[("early", "RUNNING", None)])
    details.shape.append(
        SimpleNamespace(task_name="not_started_yet", task_external_id="ext-99")
    )
    patched(details)

    run = _orchestrator().get_run("r1")

    assert {u.indicator: u.status for u in run.units} == {
        "early": "running",
        "not_started_yet": "queued",
    }


def test_scaffolding_is_not_reported_as_an_indicator(patched):
    """`plan` is ours, not a unit a caller asked for.

    The finalizers no longer need filtering by name: `shape` reports declared
    names, so the engine's `-on-failure` rename never reaches this code.
    """
    patched(
        _details(
            "RUNNING",
            units=[("plan", "COMPLETED", None), ("compactness", "RUNNING", None)],
        )
    )
    run = _orchestrator().get_run("r1")

    assert [u.indicator for u in run.units] == ["compactness"]


# ── The helpers, where the sharp edges are ───────────────────────────────────


def test_only_skipped_events_are_read_as_skips():
    """Every other event type shares the field and must not be mistaken for one.

    `task_events` carries the whole log — ASSIGNED, STARTED, FINISHED, RETRYING,
    CANCELLED and a dozen more — all with the same `task_id` field. Reading any
    of them as a skip would mark a unit that ran perfectly well as never having
    run.
    """
    details = SimpleNamespace(
        task_events=[
            SimpleNamespace(event_type=V1TaskEventType.STARTED, task_id="ext-1"),
            SimpleNamespace(event_type=V1TaskEventType.SKIPPED, task_id="ext-2"),
            SimpleNamespace(event_type=V1TaskEventType.FINISHED, task_id="ext-3"),
            SimpleNamespace(event_type=V1TaskEventType.CANCELLED, task_id="ext-4"),
        ]
    )
    assert _skipped_task_ids(details) == frozenset({"ext-2"})


def test_a_run_with_no_events_yet_has_no_skips():
    """In flight and mid-dispatch: a real, reachable empty list.

    ⚠ Two sibling cases were deleted rather than ported — `task_events=None`
    and the attribute missing entirely. `V1WorkflowRunDetails.task_events` is a
    **required** `List[V1TaskEvent]`, so neither state can arrive from the
    engine, and asserting them only pinned defensive code into place. The
    companion test for a `SKIPPED` event with `task_id=None` went the same way:
    `V1TaskEvent.task_id` is a required `str`. Its collision-into-one-bucket
    failure was reachable solely through the `getattr(..., None)` defaults that
    have now been removed.
    """
    assert _skipped_task_ids(SimpleNamespace(task_events=[])) == frozenset()


def test_units_join_shape_to_tasks_by_external_id_not_by_order():
    """The join key is explicit, because list order is not a contract."""
    details = SimpleNamespace(
        shape=[
            SimpleNamespace(task_name="first", task_external_id="ext-a"),
            SimpleNamespace(task_name="second", task_external_id="ext-b"),
        ],
        # Deliberately reversed relative to `shape`.
        tasks=[
            SimpleNamespace(
                task_external_id="ext-b",
                status=V1TaskStatus.FAILED,
                error_message="b",
            ),
            SimpleNamespace(
                task_external_id="ext-a",
                status=V1TaskStatus.COMPLETED,
                error_message=None,
            ),
        ],
        # ⚠ Required, and it was absent until the read path stopped defaulting
        # it away. `V1WorkflowRunDetails.task_events` is not optional, so a
        # fixture without it was describing a response the engine cannot send —
        # and the old `getattr(..., None) or []` absorbed the discrepancy in
        # silence. Now it is an AttributeError naming the missing field.
        task_events=[],
    )

    units, _scaffolding_failed = _units_of(details)

    assert {u.indicator: u.status for u in units} == {
        "first": "succeeded",
        "second": "failed",
    }


def test_get_run_only_treats_a_real_404_as_unknown(monkeypatch):
    """An engine outage must not be converted into a misleading API 404."""
    import reimagina.orchestration.hatchet_app as app

    monkeypatch.setattr(
        app,
        "hatchet",
        SimpleNamespace(
            runs=SimpleNamespace(get=lambda _id: (_ for _ in ()).throw(OSError("down")))
        ),
    )
    with pytest.raises(HatchetUnavailable):
        _orchestrator().get_run("r1")


def test_retry_taxonomy_excludes_deterministic_runner_errors():
    """Only database transport/contention failures should spend retry budget."""
    import psycopg2

    from reimagina.orchestration.hatchet_app import _is_retryable

    assert _is_retryable(psycopg2.OperationalError("connection reset"))
    assert not _is_retryable(ValueError("bad indicator definition"))


# ── The status table, exhaustively ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("units", "engine_status", "expected"),
    [
        # in flight — the engine's own status, never a roll-up
        ([("a", "RUNNING", None)], "RUNNING", "running"),
        ([("a", "QUEUED", None)], "QUEUED", "queued"),
        # ordinary terminal outcomes
        ([("a", "COMPLETED", None)], "COMPLETED", "succeeded"),
        ([("a", "COMPLETED", None), ("b", "FAILED", "x")], "FAILED", "partial"),
        ([("a", "FAILED", "x")], "FAILED", "failed"),
        ([("a", "CANCELLED", None)], "CANCELLED", "cancelled"),
        ([("a", "COMPLETED", None), ("b", "CANCELLED", None)], "CANCELLED", "partial"),
        # ⚠ cancelled before any task settled. `shape` is the declared graph and
        # is always populated, so these units are `queued`, not absent — the case
        # a `not units` guard could never reach, which reported `failed`.
        ([("a", "QUEUED", None), ("b", "QUEUED", None)], "CANCELLED", "cancelled"),
        # ⚠ a terminal run with one unit still unsettled: the settled units
        # answer, and a stray `queued` must not drag it to `failed`.
        ([("a", "COMPLETED", None), ("b", "QUEUED", None)], "FAILED", "succeeded"),
    ],
)
def test_the_run_status_table(patched, units, engine_status, expected):
    """Every (unit states x engine status) combination, in one place.

    ⚠ This table exists because two separate wrong fixes passed the suite. The
    roll-up is terminal-only and skip-blind, and both properties were broken by
    changes that looked obviously correct and had no case to fail against.
    """
    patched(_details(engine_status, units=units))

    assert _orchestrator().get_run("r1").status == expected


def test_an_all_skipped_terminal_run_is_failed(patched):
    """Distinct from "nothing settled" — everything settled, nothing ran.

    A run only exists when the dirty set was non-empty, so the engine skipped
    work that was asked for. Sharing a branch with the unsettled case above
    would report this `succeeded`.
    """
    patched(
        _details(
            "COMPLETED",
            skipped=["a", "b"],
            units=[("a", "COMPLETED", None), ("b", "COMPLETED", None)],
        )
    )

    assert _orchestrator().get_run("r1").status == "failed"


def test_a_failed_plan_task_is_not_reported_as_a_cancellation(patched):
    """⚠ `plan` is filtered from the unit list, so its failure must not vanish.

    When `plan` fails the engine cancels every indicator, so the units show only
    cancellations and the roll-up returned `cancelled` — a run nobody cancelled,
    reported to an operator as though someone had.
    """
    details = _details(
        "FAILED", units=[("a", "CANCELLED", None), ("b", "CANCELLED", None)]
    )
    # `plan` is the first scaffolding row `_details` injects.
    details.tasks[0].status = "FAILED"
    patched(details)

    assert _orchestrator().get_run("r1").status == "failed"


def test_an_unrecognised_unit_is_reported_not_dropped(patched):
    """A graph one release ahead of this replica must not lose units.

    The allowlist this replaces filtered by catalog membership, so an indicator
    the API had never heard of was silently deleted from `run.units` *and* from
    the roll-up — turning a run whose only failure was that unit into a
    `succeeded` one. Scaffolding is recognised structurally instead.
    """
    patched(
        _details(
            "FAILED",
            units=[
                ("a", "COMPLETED", None),
                ("indicator_from_a_newer_graph", "FAILED", "boom"),
            ],
        )
    )
    run = _orchestrator().get_run("r1")

    assert "indicator_from_a_newer_graph" in {u.indicator for u in run.units}
    assert run.status == "partial"


def test_scaffolding_is_recognised_by_shape_not_by_a_name_list(patched):
    """Any future `-on-success`/`-on-failure` hook is excluded by construction."""
    details = _details("COMPLETED", units=[("a", "COMPLETED", None)])
    details.shape.append(
        SimpleNamespace(task_name="audit-on-success", task_external_id="ext-99")
    )
    patched(details)

    assert [u.indicator for u in _orchestrator().get_run("r1").units] == ["a"]
