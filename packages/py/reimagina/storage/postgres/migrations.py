"""Postgres migration runner built on the existing Alembic setup."""

from __future__ import annotations

from pathlib import Path

from reimagina.paths import REPO_ROOT

from alembic import command
from alembic.config import Config


def _repo_root() -> Path:
    return REPO_ROOT


def postgres_alembic_config(dsn: str) -> Config:
    cfg = Config(str(_repo_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def run_postgres_migrations(dsn: str) -> None:
    command.upgrade(postgres_alembic_config(dsn), "head")
