"""Procrastinate app for the async orchestration ops-plane (Phase 3, 6/7/8).

Postgres-only. Procrastinate's own tables live in the ``procrastinate`` schema
(migration r029), so connections set ``search_path`` accordingly. LISTEN/NOTIFY
needs a direct (non-PgBouncer-transaction-mode) connection, hence
``DATABASE_URL_DIRECT``.

Tasks register on ``app`` (see ``reimagina.orchestration.tasks``); the worker is
``python -m apps.worker``. Tests swap in an in-memory connector via
``app.replace_connector(InMemoryConnector())``.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

from procrastinate import App, PsycopgConnector

_SEARCH_PATH = "-csearch_path=procrastinate,public"

# `replace_connector` mutates the module-global app, so two threads doing it
# concurrently interleave their save/restore and leave the global holding a
# connector that belongs to a finished request. Re-entrant because the embedded
# orchestrator binds around a whole submit+drain and the submit inside it binds
# again.
_CONNECTOR_LOCK = threading.RLock()


def queue_dsn() -> str:
    """The direct Postgres DSN the queue + worker use. Fails loudly if unset."""
    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        raise RuntimeError(
            "async orchestration requires a direct Postgres DSN — set "
            "DATABASE_URL_DIRECT (the pooled app DSN can't hold LISTEN/NOTIFY)"
        )
    return dsn


def _connector() -> PsycopgConnector:
    # conninfo may be empty at import; the connector is lazy and only opens on
    # worker start / defer. Anything that defers must go through `bound()`
    # rather than trusting this one — see below.
    return PsycopgConnector(
        conninfo=os.environ.get("DATABASE_URL_DIRECT", ""),
        kwargs={"options": _SEARCH_PATH},
    )


_IMPORT_DSN = os.environ.get("DATABASE_URL_DIRECT", "")
_DEFAULT_CONNECTOR = _connector()

app = App(connector=_DEFAULT_CONNECTOR, import_paths=["reimagina.orchestration.tasks"])


@contextmanager
def bound():
    """Ensure the app has a usable connector before deferring, then restore.

    The module-global connector reads ``DATABASE_URL_DIRECT`` once, at import.
    A process that imports this module before that variable is set keeps a
    connector with an empty conninfo, which libpq resolves to its own defaults —
    a database where the ``procrastinate`` schema does not exist, so the first
    defer fails with ``type "procrastinate_job_to_defer_v1[]" does not exist``.
    Import order is not something a orchestrator can be asked to reason about,
    so it asks here instead.

    Deliberately narrow. It rebinds **only** the blind-import case: if the app
    is holding any other connector, someone installed it on purpose — a test's
    ``InMemoryConnector``, or an outer ``bound()`` — and replacing it would
    silently redirect their jobs to a different broker.

    Serialized: see ``_CONNECTOR_LOCK``.
    """
    if _IMPORT_DSN or app.connector is not _DEFAULT_CONNECTOR:
        yield app
        return
    connector = PsycopgConnector(conninfo=queue_dsn(), kwargs={"options": _SEARCH_PATH})
    with _CONNECTOR_LOCK, app.replace_connector(connector):
        yield app
