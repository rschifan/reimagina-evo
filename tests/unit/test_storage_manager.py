from __future__ import annotations

import pytest

from reimagina.storage.manager import backend_name_for_dsn


def test_backend_name_for_dsn_supports_postgres():
    assert backend_name_for_dsn("postgresql://localhost/reimagina") == "postgres"
    assert backend_name_for_dsn("postgres://localhost/reimagina") == "postgres"


def test_backend_name_for_dsn_rejects_unsupported_scheme():
    with pytest.raises(ValueError):
        backend_name_for_dsn("sqlite:///data/local/reimagina.db")
