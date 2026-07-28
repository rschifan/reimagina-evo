"""Shared job_run lifecycle for compute workers.

Collapses the identical ``start_job(running) → try → complete_job(succeeded)
/ except → complete_job(failed) → raise`` block that every worker (rollup,
building density, compactness, …) was hand-writing.

It also stamps each row with the orchestration run it belongs to, taken from an
ambient ``current_run`` rather than a parameter. That is deliberate: the run id
would otherwise have to be threaded through the ``Runner`` protocol, both
adapter closures, four compute-fn signatures and every test fake, to reach the
one place that writes a row — and a direct CLI compute, which belongs to no run,
would have to pass ``None`` through all of it. An executor sets it once around
the call; unset means "not part of an orchestrated run", which is a real case
and not an error.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

# The orchestration run whose dirty-set the current compute belongs to, or None
# for a standalone job (a direct `indicators compute` from the CLI).
_current_run: ContextVar[str | None] = ContextVar("current_run", default=None)


@contextmanager
def current_run(run_id: str | None) -> Iterator[None]:
    """Attribute every ``job_run`` opened in this scope to ``run_id``.

    Set it *inside* whatever executes the unit, never around a dispatch: a
    ContextVar does not cross into a thread the executor did not itself enter,
    and this must be visible on the thread that runs the compute.
    """
    token = _current_run.set(run_id)
    try:
        yield
    finally:
        # A run id that outlived its run would mis-attribute the next job on
        # this thread — the reason a bare `.set()` is not enough.
        _current_run.reset(token)


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

    The row records the ambient ``current_run`` and the backend pid of the
    connection doing the work, which together are what makes a run cancellable
    (see migration r031).
    """
    repo = backend.job_run_repo()
    job_run_id = repo.start_job(
        model_family=model_family,
        code_version=code_version,
        scenario_id=scenario_id,
        status="running",
        run_id=_current_run.get(),
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
