"""URL resolution for Alembic — the function *and* the wiring that feeds it.

The three `resolve_migration_url` tests below were green while
`DATABASE_URL=... alembic downgrade ...` was silently rewriting the production
database, because every one of them passes `configured_url=None` — a value the
real wiring never produced. `alembic.ini` always supplied a URL, so the
env-var branch they exercise was unreachable outside this file.

The function was never wrong. The configuration was. So the last test here
asserts the configuration, which is the only thing that would have caught it.
"""

from __future__ import annotations

import configparser

from reimagina.paths import REPO_ROOT

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


def test_alembic_ini_does_not_hardcode_a_database():
    """The wiring test. `sqlalchemy.url` must stay empty in `alembic.ini`.

    Any value here beats `DATABASE_URL`, because resolution is
    `configured_url or env_url or default`. A value naming a real database
    therefore turns every `DATABASE_URL=... alembic ...` invocation into a
    silent write to that database instead — which is exactly what happened.

    Empty is not a missing setting: programmatic callers still win by passing a
    DSN through `set_main_option`, and only the bare CLI falls through to the
    environment.
    """
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "alembic.ini")

    configured = parser.get("alembic", "sqlalchemy.url", fallback="")

    assert configured.strip() == "", (
        f"alembic.ini pins sqlalchemy.url to {configured!r}, which silently "
        "overrides DATABASE_URL for every CLI invocation"
    )
    # And the resolution it feeds must then defer to the environment.
    assert (
        resolve_migration_url(configured, "postgresql://localhost/somewhere_else")
        == "postgresql://localhost/somewhere_else"
    )
