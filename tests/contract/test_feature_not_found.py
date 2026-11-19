"""``/features/{id}/indicators`` answers 404 only for an unknown feature.

The route used to catch ``LookupError``, which also catches the
``KeyError``/``IndexError`` of a genuine defect anywhere in the repository
call and turned it into "Unknown feature". It now catches ``FeatureNotFound``
alone. No Postgres: the backend is faked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from reimagina.storage.interfaces import FeatureNotFound

URL = "/features/00000000-0000-0000-0000-000000000000/indicators"


def _client(monkeypatch, exc: Exception) -> TestClient:
    class _Repo:
        def feature_indicator_ranks(self, **_kwargs):
            raise exc

    class _Backend:
        def indicator_result_repo(self):
            return _Repo()

    monkeypatch.setattr(
        "apps.api.routers.features.get_backend", lambda conn: _Backend()
    )
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])
    return TestClient(app)


def test_an_unknown_feature_is_404(monkeypatch):
    with _client(monkeypatch, FeatureNotFound("x")) as c:
        assert c.get(URL).status_code == 404


def test_a_key_error_in_the_repository_is_not_turned_into_404(monkeypatch):
    with _client(monkeypatch, KeyError("some_key")) as c, pytest.raises(KeyError):
        c.get(URL)
