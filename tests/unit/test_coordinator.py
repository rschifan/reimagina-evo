"""Unit tests for the rung-1 coordinator — pure, fake runners, no DB."""

from __future__ import annotations

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.coordinator import UnitStatus, run_dirty_set
from reimagina.orchestration.impact import compute_dirty_set


def _spec(slug, *, reads=(), depends_on=(), kind="direct", applies_to=("block",)):
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind=kind,
        value_type="numeric",
        aggregation_method="weighted_mean",
        applies_to=applies_to,
        reads=reads,
        depends_on=depends_on,
    )


GREEN = _spec("green_coverage", reads=("land_use",))
WALK = _spec("walkability", reads=("land_use",))
LIVEABILITY = _spec(
    "liveability_index",
    kind="composite",
    depends_on=(("walkability", 0.5), ("green_coverage", 0.5)),
)
ROLLUP = _spec(
    "liveability_rollup",
    kind="composite",
    depends_on=(("liveability_index", None),),
    applies_to=("ageb",),
)
REGISTRY = (GREEN, WALK, LIVEABILITY, ROLLUP)


def _runner(log, name, *, fail=False):
    def run(conn, *, features, scenario_id, code_version, aoi_id):
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")

    return run


class _InFailedSqlTransaction(Exception):
    """Stand-in for psycopg2's InFailedSqlTransaction."""


class _PgLikeConn:
    """Just enough of a Postgres transaction to show what a failure poisons.

    A failed statement aborts the transaction: every later statement raises
    until a ``ROLLBACK TO SAVEPOINT``, and ``commit()`` on an aborted
    transaction silently rolls *everything* back — what psycopg2 really does.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.aborted = False
        self._savepoints: list[int] = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None) -> None:
        if sql.startswith("ROLLBACK TO SAVEPOINT"):
            del self.writes[self._savepoints[-1] :]
            self.aborted = False
            return
        if self.aborted:
            raise _InFailedSqlTransaction("current transaction is aborted")
        if sql.startswith("SAVEPOINT"):
            self._savepoints.append(len(self.writes))
        elif sql.startswith("RELEASE SAVEPOINT"):
            self._savepoints.pop()
        elif sql.startswith("WRITE "):
            self.writes.append(sql.removeprefix("WRITE "))
        elif sql == "FAIL":
            self.aborted = True
            raise RuntimeError("constraint violation")

    def commit(self) -> None:
        if self.aborted:
            self.writes.clear()


def _dirty(changed=("land_use",), edited=("b1",)):
    return compute_dirty_set(set(changed), set(edited), REGISTRY)


def test_all_succeed_runs_in_dependency_order():
    log: list[str] = []
    runners = {
        s: _runner(log, s)
        for s in (
            "green_coverage",
            "walkability",
            "liveability_index",
            "liveability_rollup",
        )
    }

    report = run_dirty_set(None, _dirty(), REGISTRY, runners)

    assert report.status == "succeeded"
    # inputs ran before dependents.
    assert log.index("green_coverage") < log.index("liveability_index")
    assert log.index("walkability") < log.index("liveability_index")
    assert log.index("liveability_index") < log.index("liveability_rollup")


def test_failed_input_skips_its_dependents_only():
    log: list[str] = []
    runners = {
        "green_coverage": _runner(log, "green_coverage"),
        "walkability": _runner(log, "walkability", fail=True),
        "liveability_index": _runner(log, "liveability_index"),
        "liveability_rollup": _runner(log, "liveability_rollup"),
    }

    report = run_dirty_set(None, _dirty(), REGISTRY, runners)
    by = report.by_indicator()

    assert (
        by["green_coverage"].status is UnitStatus.SUCCEEDED
    )  # independent → still runs
    assert by["walkability"].status is UnitStatus.FAILED
    assert by["liveability_index"].status is UnitStatus.SKIPPED
    assert by["liveability_rollup"].status is UnitStatus.SKIPPED
    assert "walkability" in by["liveability_index"].detail
    # dependents never executed.
    assert "liveability_index" not in log
    assert report.status == "partial"


def test_missing_runner_skips_unit_and_cascades():
    log: list[str] = []
    runners = {  # green_coverage intentionally absent
        "walkability": _runner(log, "walkability"),
        "liveability_index": _runner(log, "liveability_index"),
        "liveability_rollup": _runner(log, "liveability_rollup"),
    }

    by = run_dirty_set(None, _dirty(), REGISTRY, runners).by_indicator()

    assert by["green_coverage"].status is UnitStatus.SKIPPED
    assert by["green_coverage"].detail == "no runner registered"
    assert by["walkability"].status is UnitStatus.SUCCEEDED  # ran despite sibling gap
    assert (
        by["liveability_index"].status is UnitStatus.SKIPPED
    )  # green_coverage unsatisfied


def test_independent_units_do_not_block_each_other():
    # Two unrelated direct indicators; one fails, the other still runs.
    a = _spec("a", reads=("x",))
    b = _spec("b", reads=("x",))
    registry = (a, b)
    dirty = compute_dirty_set({"x"}, {"f1"}, registry)
    log: list[str] = []
    runners = {"a": _runner(log, "a", fail=True), "b": _runner(log, "b")}

    by = run_dirty_set(None, dirty, registry, runners).by_indicator()

    assert by["a"].status is UnitStatus.FAILED
    assert by["b"].status is UnitStatus.SUCCEEDED
    assert "b" in log


def test_status_failed_when_nothing_succeeds():
    a = _spec("a", reads=("x",))
    dirty = compute_dirty_set({"x"}, {"f1"}, (a,))
    report = run_dirty_set(None, dirty, (a,), {"a": _runner([], "a", fail=True)})
    assert report.status == "failed"


def test_features_are_passed_to_the_runner():
    seen: dict[str, frozenset[str]] = {}

    def capture(conn, *, features, scenario_id, code_version, aoi_id):
        seen["green_coverage"] = features

    dirty = compute_dirty_set({"land_use"}, {"b1", "b2"}, REGISTRY)
    run_dirty_set(None, dirty, REGISTRY, {"green_coverage": capture})
    assert seen["green_coverage"] == frozenset({"b1", "b2"})


def test_a_failed_unit_does_not_poison_the_rest_of_the_transaction():
    """`--sync` runs every unit on one connection and commits once at the end.

    Without a per-unit savepoint, one SQL failure aborted the transaction: every
    later unit failed with "current transaction is aborted", and the final
    commit silently rolled back the units already reported ``succeeded``.
    """
    a = _spec("a", reads=("x",))
    b = _spec("b", reads=("x",))
    c = _spec("c", reads=("x",))
    registry = (a, b, c)
    dirty = compute_dirty_set({"x"}, {"f1"}, registry)

    def _sql(statement):
        def run(conn, *, features, scenario_id, code_version, aoi_id):
            with conn.cursor() as cur:
                cur.execute(statement)

        return run

    conn = _PgLikeConn()
    runners = {"a": _sql("WRITE a"), "b": _sql("FAIL"), "c": _sql("WRITE c")}

    by = run_dirty_set(conn, dirty, registry, runners).by_indicator()
    conn.commit()

    assert by["a"].status is UnitStatus.SUCCEEDED
    assert by["b"].status is UnitStatus.FAILED
    assert by["c"].status is UnitStatus.SUCCEEDED
    # What was reported succeeded is what is actually committed.
    assert conn.writes == ["a", "c"]


class _AutocommitConn(_PgLikeConn):
    """Autocommit: every statement commits itself; SAVEPOINT is an error."""

    autocommit = True

    def execute(self, sql: str, params=None) -> None:
        if "SAVEPOINT" in sql:
            raise RuntimeError("SAVEPOINT can only be used in transaction blocks")
        super().execute(sql, params)


def test_an_autocommit_connection_runs_without_savepoints():
    """The conformance suite drives `sync` on an autocommit connection.

    There is no shared transaction to poison there, and Postgres refuses
    SAVEPOINT outside a transaction block, so the guard must stand down.
    """
    a = _spec("a", reads=("x",))
    registry = (a,)
    dirty = compute_dirty_set({"x"}, {"f1"}, registry)

    def _write(conn, *, features, scenario_id, code_version, aoi_id):
        with conn.cursor() as cur:
            cur.execute("WRITE a")

    by = run_dirty_set(_AutocommitConn(), dirty, registry, {"a": _write}).by_indicator()

    assert by["a"].status is UnitStatus.SUCCEEDED


def test_a_unit_whose_scenario_was_deleted_is_cancelled_not_succeeded():
    """``runners._fenced`` rolls such a unit back and writes nothing.

    Reporting it ``succeeded`` claimed results that were never written; the
    scenario delete cancels the run's other units, and this one is the same.
    """
    from reimagina.orchestration.coordinator import SCENARIO_DELETED

    a = _spec("a", reads=("x",))
    dirty = compute_dirty_set({"x"}, {"f1"}, (a,))

    def gone(conn, **kwargs):
        return SCENARIO_DELETED

    report = run_dirty_set(None, dirty, (a,), {"a": gone})

    assert report.by_indicator()["a"].status is UnitStatus.CANCELLED
    assert report.status == "cancelled"
