"""``OpsRunReader`` against a real database — the Ops dashboard's only reader.

These are the properties the mocked contract tests cannot express, because every
one of them is a claim about SQL: that the counts are per-run rather than
global, that a run with no units reports zero rather than one, that an empty
status list means "no filter" rather than "no runs", and that a malformed id is
an empty answer rather than a ``DataError`` the API turns into a 500.

Writes go through ``orchestration.runstore`` — the single writer — so these also
pin the two modules to the same rows rather than to each other's assumptions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from reimagina.orchestration import runstore
from reimagina.storage import backend_for_connection


def _reader(conn):
    return backend_for_connection(conn).ops_run_reader()


def _make_run(conn, *, status="running", units=(), code_version="test"):
    """One run envelope plus its units, via the writer the workflow uses."""
    run_id = str(uuid4())
    runstore.create_run(
        conn,
        run_id=run_id,
        scenario_id=None,
        aoi_id=None,
        code_version=code_version,
        status=status,
    )
    runstore.record_units(
        conn,
        run_id=run_id,
        units=[
            {"indicator": slug, "features": [], "status": "queued", "detail": None}
            for slug, _ in units
        ],
    )
    for slug, unit_status in units:
        if unit_status != "queued":
            runstore.set_unit_status(
                conn, run_id=run_id, indicator_slug=slug, status=unit_status
            )
    return run_id


def test_counts_are_per_run_not_global(conn):
    """The lateral aggregate must be correlated — two runs must not share counts."""
    a = _make_run(conn, units=[("alpha", "succeeded"), ("beta", "failed")])
    b = _make_run(conn, units=[("gamma", "succeeded")])

    rows = {r["run_id"]: r for r in _reader(conn).list_runs(limit=50)}

    assert rows[a]["units_total"] == 2
    assert rows[a]["units_succeeded"] == 1
    assert rows[a]["units_failed"] == 1
    assert rows[b]["units_total"] == 1
    assert rows[b]["units_failed"] == 0


def test_a_run_with_no_units_counts_zero(conn):
    """``count(*)`` in the lateral would say 1 here. It must say 0."""
    run_id = _make_run(conn, units=[])

    row = _reader(conn).get_run_row(run_id)

    assert row["units_total"] == 0
    assert row["units_terminal"] == 0
    assert row["units_last_terminal_at"] is None


def test_empty_status_list_means_no_filter(conn):
    """``[]`` binds as an empty array, which matches nothing — it must not reach SQL.

    The failure this pins is silent: an empty list is what ``body.x or []``
    produces, and the dashboard would show no runs at all while reporting success.
    """
    run_id = _make_run(conn, status="running", units=[("alpha", "queued")])
    reader = _reader(conn)

    assert run_id in {r["run_id"] for r in reader.list_runs(statuses=[], limit=50)}
    assert reader.count_runs(statuses=[]) == reader.count_runs()


def test_status_filter_selects(conn):
    running = _make_run(conn, status="running", units=[("alpha", "queued")])
    failed = _make_run(conn, status="failed", units=[("beta", "failed")])

    ids = {r["run_id"] for r in _reader(conn).list_runs(statuses=["failed"], limit=50)}

    assert failed in ids
    assert running not in ids


def test_status_counts_are_unfiltered(conn):
    _make_run(conn, status="failed", units=[("alpha", "failed")])
    _make_run(conn, status="succeeded", units=[("beta", "succeeded")])

    counts = _reader(conn).count_runs_by_status()

    assert counts.get("failed", 0) >= 1
    assert counts.get("succeeded", 0) >= 1


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "", "1; DROP TABLE x", "42"])
def test_a_malformed_run_id_is_an_empty_answer_not_an_error(conn, bad_id):
    """Reaching Postgres with a non-uuid raises DataError → 500 + aborted txn."""
    reader = _reader(conn)

    assert reader.get_run_row(bad_id) is None
    assert reader.list_units_detailed(bad_id) == []


def test_units_carry_their_timings_in_dispatch_order(conn):
    """``started_at``/``completed_at`` are what the dashboard renders as duration."""
    run_id = _make_run(
        conn, units=[("beta", "queued"), ("alpha", "running")]
    )
    runstore.set_unit_status(
        conn, run_id=run_id, indicator_slug="alpha", status="succeeded"
    )

    units = {u["indicator"]: u for u in _reader(conn).list_units_detailed(run_id)}

    assert units["alpha"]["started_at"] is not None
    assert units["alpha"]["completed_at"] is not None
    assert units["beta"]["started_at"] is None
    assert units["beta"]["completed_at"] is None


def test_last_terminal_at_is_set_for_every_terminal_status(conn):
    """The stall grace reads this column; a NULL max would make the badge never fire.

    ``skipped`` and ``cancelled`` are terminal too — the interesting case, since
    neither goes through the ``running`` transition that stamps ``started_at``.
    """
    for terminal in ("succeeded", "failed", "skipped", "cancelled"):
        run_id = _make_run(conn, units=[("alpha", terminal)])
        row = _reader(conn).get_run_row(run_id)
        assert row["units_terminal"] == 1, terminal
        assert row["units_last_terminal_at"] is not None, terminal


def test_executor_health_reports_age_as_a_number(conn):
    """An ``interval`` here would be a 500 at the Pydantic boundary."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs_runtime.ops_executor_heartbeat
              (executor_id, role, app_version)
            VALUES ('test-exec', 'worker', 'v0')
            ON CONFLICT (executor_id) DO UPDATE SET heartbeat_at = clock_timestamp()
            """
        )

    rows = {r["executor_id"]: r for r in _reader(conn).list_executor_health()}

    assert isinstance(rows["test-exec"]["heartbeat_age_s"], float)
    assert rows["test-exec"]["heartbeat_age_s"] >= 0
