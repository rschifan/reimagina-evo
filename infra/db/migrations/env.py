"""Alembic environment — runs migrations against the configured database URL."""
from __future__ import annotations
import os
from dotenv import load_dotenv
from alembic import context
from sqlalchemy import create_engine, pool
from migration_env import resolve_migration_url

load_dotenv()

alembic_config = context.config


def _url() -> str:
    return resolve_migration_url(
        alembic_config.get_main_option("sqlalchemy.url"),
        os.environ.get("DATABASE_URL"),
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
