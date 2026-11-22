"""Where the API says a block's scenario inputs are estimated.

The fit marks a block whose lot coverage it had to guess
(``attributes.estimated``); the indicator stats count those rows
(``count_estimated``) and the overlay tile carries the reason per block
(``tests/integration/test_indicator_overlay_delta.py``). No Postgres here:
the route is driven with a fake backend.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import apps.api.routers.indicators as mod
from apps.api.app import create_app
from apps.api.deps import get_db_conn

_STATS = {
    "count": 3,
    "min": 1.0,
    "p05": 1.0,
    "p50": 2.0,
    "p95": 3.0,
    "max": 3.0,
    "sum": 6.0,
    "count_at_cap": None,
    "count_estimated": 2,
}


class _Repo:
    def value_stats(self, **_kw):
        return dict(_STATS)


class _Backend:
    def indicator_result_repo(self):
        return _Repo()


def test_stats_report_how_many_rows_are_estimated(monkeypatch):
    monkeypatch.setattr(mod, "ensure_indicator_exists", lambda conn, slug: None)
    monkeypatch.setattr(mod, "get_backend", lambda conn: _Backend())
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])
    with TestClient(app) as client:
        body = client.get("/indicators/edificability_block_density/stats").json()
    assert body["count_estimated"] == 2
    assert body["count"] == 3


def test_count_estimated_is_in_the_published_schema():
    schema = create_app(initialise_pool=False).openapi()
    stats = schema["components"]["schemas"]["IndicatorValueStats"]["properties"]
    assert stats["count_estimated"]["type"] == "integer"
