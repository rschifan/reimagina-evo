"""With no environment set, the API and the CLI open the same database.

The API defaulted to `reimagina_app` while the CLI, migrations and loaders
default to `reimagina`, so a bare `make dev` served an empty or missing
database next to the one the load had just filled.
"""

from __future__ import annotations

from apps.api.deps import api_dsn
from reimagina.storage.manager import DEFAULT_DEV_DSN


def test_the_api_defaults_to_the_cli_database(monkeypatch):
    monkeypatch.delenv("REIMAGINA_APP_DSN", raising=False)
    assert api_dsn() == DEFAULT_DEV_DSN
