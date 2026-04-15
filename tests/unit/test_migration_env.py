from __future__ import annotations

from migration_env import resolve_migration_url


def test_resolve_migration_url_prefers_explicit_config():
    assert (
        resolve_migration_url(
            "postgresql://localhost/reimagina_test",
            "postgresql://localhost/reimagina_prod",
        )
        == "postgresql://localhost/reimagina_test"
    )


def test_resolve_migration_url_falls_back_to_env():
    assert (
        resolve_migration_url(None, "postgresql://localhost/reimagina_prod")
        == "postgresql://localhost/reimagina_prod"
    )


def test_resolve_migration_url_uses_default_last():
    assert resolve_migration_url(None, None) == "postgresql://localhost/reimagina"
