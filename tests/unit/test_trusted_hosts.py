"""The API refuses a Host it does not serve (DNS rebinding).

Loopback binding is the documented guard for an unauthenticated API, and it
is not enough on its own: a page on ``evil.example`` that rebinds its name to
127.0.0.1 is same-origin with the API and can call every route. The browser
still sends ``Host: evil.example``, so checking Host closes it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("REIMAGINA_API_ALLOWED_HOSTS", raising=False)
    app = create_app(initialise_pool=False)

    @app.get("/__ping")
    def _ping():
        return {}

    return TestClient(app)


@pytest.mark.parametrize(
    "host", ["localhost:5173", "localhost:8001", "127.0.0.1:8001", "[::1]:8001"]
)
def test_the_dev_hosts_are_served(client, host):
    assert client.get("/__ping", headers={"Host": host}).status_code == 200


def test_a_rebound_host_is_refused(client):
    r = client.get("/__ping", headers={"Host": "evil.example:8001"})
    assert r.status_code == 400


def test_the_allow_list_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("REIMAGINA_API_ALLOWED_HOSTS", "api.internal, localhost")
    app = create_app(initialise_pool=False)

    @app.get("/__ping")
    def _ping():
        return {}

    http = TestClient(app)
    assert http.get("/__ping", headers={"Host": "api.internal"}).status_code == 200
    assert http.get("/__ping", headers={"Host": "127.0.0.1"}).status_code == 400
