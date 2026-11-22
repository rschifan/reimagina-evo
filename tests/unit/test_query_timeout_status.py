"""A statement that hits its own timeout is not a database outage.

``QueryCanceled`` subclasses ``OperationalError`` in psycopg2, so it used to
reach the outage handler: 503 ``database_unavailable`` and a "database
unavailable" log line for what was one expensive tile.
"""

from __future__ import annotations

import psycopg2.errors
from fastapi.testclient import TestClient

from apps.api.app import create_app


def test_a_statement_timeout_answers_504_query_timeout():
    app = create_app()

    @app.get("/__slow")
    def _slow():
        raise psycopg2.errors.QueryCanceled(
            "canceling statement due to statement timeout"
        )

    response = TestClient(app, raise_server_exceptions=False).get("/__slow")

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "query_timeout"
