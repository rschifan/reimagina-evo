"""Run bookkeeping for the ops plane.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class JobRunRepository(Protocol):
    """Provenance record of a compute run (jobs_runtime.job_run).

    A run is created via start_job (typically straight to 'running' by the
    job_run lifecycle helper) and terminated via complete_job with 'succeeded'
    or 'failed'. Every indicator_result row must cite a job_run by id.

    Ops-plane scheduling and claiming belong to the ops-plane, not here —
    there is no claim_job.
    """

    def start_job(
        self,
        *,
        model_family: str,
        code_version: str,
        scenario_id: str | None = None,
        status: str = "queued",
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> str: ...

    def complete_job(
        self,
        job_run_id: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None: ...

    def cancel_run_backends(self, run_id: str) -> int:
        """Cancel the in-flight query of every live backend for ``run_id``.

        The ops-plane's own cancel only stops *scheduling*; interrupting a
        statement already executing needs the database. Returns the number of
        backends signalled.
        """
        ...


class RunPlanRepository(Protocol):
    """The resolved feature set for a run (jobs_runtime.run_plan).

    Write-once. The submitter resolves the plan and stores it; each unit reads
    back only its own slice. Keeping it here rather than in the workflow input is
    what stops the payload scaling with the size of the city.
    """

    def write(self, *, plan_id: str, units: dict[str, list[str]]) -> None: ...

    def features_for(self, *, plan_id: str, indicator_slug: str) -> list[str]: ...

    def delete(self, *, plan_id: str) -> int: ...

    def sweep(self, *, older_than_hours: int) -> int: ...


class OpsRunReader(Protocol):
    """Read side of the run envelope, for the Ops dashboard.

    Reads ``jobs_runtime.orchestration_run`` / ``job_unit`` /
    ``ops_executor_heartbeat`` — the same tables ``orchestration.runstore``
    writes, and deliberately not through it. ``runstore`` belongs to the
    ops-plane and its own docstring lists it among the modules an ops-plane
    branch may delete outright; a dashboard the API serves must outlive that.
    Writes stay entirely in ``runstore``: this Protocol has no mutator.

    Every method that takes a ``run_id`` guards the id shape before it reaches
    Postgres. ``orchestration_run.id`` is a uuid, so a non-uuid arrives as a
    ``DataError``, which the API's error handler maps to a 500 *and* aborts the
    request transaction on the way out — a 404 has to be decided here.
    """

    def list_runs(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Newest runs first, each with its unit counts.

        The counts come from a lateral aggregate over the *already limited* run
        set. Joining first and grouping after would aggregate every ``job_unit``
        row ever written before the limit applied, on a query the dashboard
        polls every two seconds.
        """
        ...

    def count_runs(self, *, statuses: list[str] | None = None) -> int: ...

    def count_runs_by_status(self) -> dict[str, int]:
        """Unfiltered tallies, one per status that has rows.

        Unfiltered on purpose: counts that shrink to match the active filter
        make the filter chips useless for finding what you filtered away.
        Statuses with no rows are absent — the caller zero-fills.
        """
        ...

    def get_run_row(self, run_id: str) -> dict | None: ...

    def list_units_detailed(self, run_id: str) -> list[dict]: ...

    def delete_run(self, run_id: str) -> bool:
        """Delete one run's envelope and its unit rows. True if a row went.

        History only. ``job_unit`` cascades, but the computed values in
        ``indicator_result`` and the ``job_run`` provenance rows are separate and
        deliberately untouched — forgetting that a run happened is not the same
        as undoing what it produced.
        """
        ...

    def delete_terminal_runs(self) -> int:
        """Delete every finished run, returning how many went.

        Live runs are left alone: their workflow is still writing to these rows,
        so deleting one would have it recreate a half-run envelope behind you.
        """
        ...

    def list_executor_health(self) -> list[dict]:
        """Live executors with their heartbeat age in seconds.

        Reads the ``jobs_runtime`` base table rather than
        ``explore.ops_executor_health_v``: ``explore`` is a human-facing
        contract, not an application dependency (AGENTS.md), and the view emits
        an ``interval`` where this consumer needs a float.
        """
        ...
