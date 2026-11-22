"""Capped indicator reads say when they were capped.

``/values`` and the three downloads used to answer 200 with exactly ``limit``
rows and nothing saying more existed — a short file that looks complete.
Each now fetches one row past the cap and reports ``X-Truncated``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

import apps.api.routers.indicators as indicators
from apps.api.app import create_app
from apps.api.deps import get_db_conn

SLUG = "x"
NOW = datetime(2026, 11, 22, tzinfo=UTC)
TOTAL = 3


def _fid(i: int) -> str:
    return f"00000000-0000-0000-0000-00000000000{i}"


class _Repo:
    def __init__(self, calls):
        self.calls = calls

    def get_indicator(self, slug):
        return {"slug": slug}

    def list_current_for_indicator(self, **kw):
        self.calls.append(kw)
        after = kw.get("after_feature_id")
        rows = [
            {
                "feature_id": _fid(i),
                "feature_type": "block",
                "indicator_slug": SLUG,
                "quality_flag": "ok",
                "computed_at": NOW,
            }
            for i in range(TOTAL)
            if after is None or _fid(i) > after
        ]
        return rows[: kw["limit"]]

    def list_current_with_geometry(self, **kw):
        self.calls.append(kw)
        return [
            {
                "feature_id": _fid(i),
                "type_slug": "block",
                "geometry_wkb": Point(0, 0).wkb,
                "value_numeric": 1.0,
                "quality_flag": "ok",
                "computed_at": NOW,
            }
            for i in range(TOTAL)
        ][: kw["limit"]]


class _Backend:
    def __init__(self, calls):
        self._repo = _Repo(calls)

    def indicator_catalog_repo(self):
        return self._repo

    def indicator_result_repo(self):
        return self._repo


@pytest.fixture
def client(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(indicators, "get_backend", lambda _c: _Backend(calls))
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: None
    return TestClient(app), calls


@pytest.mark.parametrize("suffix", ["", ".csv", ".geojson", ".parquet"])
def test_a_capped_read_says_it_was_capped(client, suffix):
    http, _ = client
    cut = http.get(f"/indicators/{SLUG}/values{suffix}?limit=2")
    whole = http.get(f"/indicators/{SLUG}/values{suffix}?limit={TOTAL}")

    assert cut.status_code == whole.status_code == 200
    assert cut.headers["X-Truncated"] == "true"
    assert cut.headers["X-Row-Limit"] == "2"
    assert whole.headers["X-Truncated"] == "false"


def test_values_is_capped_at_limit_and_pages_by_feature_id(client):
    http, _ = client
    first = http.get(f"/indicators/{SLUG}/values?limit=2").json()
    assert [r["feature_id"] for r in first] == [_fid(0), _fid(1)]

    rest = http.get(
        f"/indicators/{SLUG}/values?limit=2&after_feature_id={first[-1]['feature_id']}"
    )
    assert [r["feature_id"] for r in rest.json()] == [_fid(2)]
    assert rest.headers["X-Truncated"] == "false"


def test_the_browser_may_read_the_truncation_headers(client):
    http, _ = client
    r = http.get(
        f"/indicators/{SLUG}/values?limit=1",
        headers={"Origin": "http://localhost:5173"},
    )
    exposed = r.headers["access-control-expose-headers"]
    assert "X-Truncated" in exposed and "X-Row-Limit" in exposed
