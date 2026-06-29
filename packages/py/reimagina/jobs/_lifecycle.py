"""Shared job_run lifecycle for compute workers.

Collapses the identical ``start_job(running) → try → complete_job(succeeded)
/ except → complete_job(failed) → raise`` block that every worker (rollup,
building density, compactness, …) was hand-writing.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def job_run(
    backend: Any,
    *,
    model_family: str,
    code_version: str,
    scenario_id: str | None,
) -> Iterator[str]:
    """Open a ``job_run`` row, yield its id, and finalise its status.

    On clean exit the row is marked ``succeeded``; on any exception it is marked
    ``failed`` (with ``"<ExcType>: <msg>"`` as the error) and the exception is
    re-raised so the caller still sees the failure.
    """
    repo = backend.job_run_repo()
    job_run_id = repo.start_job(
        model_family=model_family,
        code_version=code_version,
        scenario_id=scenario_id,
        status="running",
    )
    try:
        yield job_run_id
    except Exception as exc:
        repo.complete_job(
            job_run_id,
            status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        repo.complete_job(job_run_id, status="succeeded")
