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

from procrastinate import App, PsycopgConnector

_SEARCH_PATH = "-csearch_path=procrastinate,public"


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
    # worker start / defer, by which point DATABASE_URL_DIRECT must be set.
    return PsycopgConnector(
        conninfo=os.environ.get("DATABASE_URL_DIRECT", ""),
        kwargs={"options": _SEARCH_PATH},
    )


app = App(connector=_connector(), import_paths=["reimagina.orchestration.tasks"])
