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
