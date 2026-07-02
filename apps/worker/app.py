"""Worker application — the Procrastinate worker runtime for async orchestration.

``python -m apps.worker`` drains the ``compute`` + ``control`` queues (see
``reimagina.orchestration.tasks``). Requires ``DATABASE_URL_DIRECT``.
"""

from __future__ import annotations


def create_worker() -> dict[str, str]:
    """Scaffold descriptor (kept for the boot smoke-test / health surface)."""
    return {"app": "worker", "namespace": "reimagina", "status": "ready"}


def run_worker(queues: list[str] | None = None) -> None:
    """Run the Procrastinate worker (blocking) over the given queues."""
    from reimagina.orchestration.queue_app import app, queue_dsn

    queue_dsn()  # fail loudly if DATABASE_URL_DIRECT is unset
    with app.open():
        app.run_worker(queues=queues or ["compute", "control"])
