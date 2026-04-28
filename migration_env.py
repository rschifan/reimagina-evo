"""Helpers shared by the Alembic environment and migration-related tests."""

from __future__ import annotations


def resolve_migration_url(
    configured_url: str | None,
    env_url: str | None,
    default_url: str = "postgresql://localhost/reimagina",
) -> str:
    """Resolve the database URL for Alembic."""
    return configured_url or env_url or default_url
