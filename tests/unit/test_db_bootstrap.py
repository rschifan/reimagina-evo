from __future__ import annotations

import pytest

from reimagina.db.bootstrap import REQUIRED_RELATIONS, bootstrap_local_platform
from reimagina.storage.postgres.bootstrap import bootstrap_postgres

# Deliberately pooled-looking: production points `REIMAGINA_APP_DSN` at
# pgbouncer, so this is the shape the app DSN really has when the two tests
# below ask which one a schema migration goes to.
POOLED_APP_DSN = "postgresql://pooler/reimagina"
DIRECT_DSN = "postgresql://direct:5432/reimagina"


def test_required_relations_cover_first_spatial_layer():
    assert "curated_geo.data_source_catalog" in REQUIRED_RELATIONS
    assert "curated_geo.ingestion_run" in REQUIRED_RELATIONS
    assert "curated_geo.feature_type_catalog" in REQUIRED_RELATIONS
    assert "curated_geo.feature" in REQUIRED_RELATIONS
    assert "serving_geo.feature_v" in REQUIRED_RELATIONS


class _Recorder:
    """What bootstrap asked each of its collaborators to do.

    `bootstrap_local_platform` is a sequence of side effects on a real server,
    so every test here stubs the same five collaborators. Recording rather than
    asserting inline keeps each test's assertions to the one step it is about.
    """

    def __init__(self) -> None:
        self.seeded_catalog = False
        self.seeded_assets = False
        self.provisioned: list[str] = []


def _stub_collaborators(monkeypatch, *, app_created: bool = True) -> _Recorder:
    """Make `bootstrap_local_platform` run offline; return what it did."""
    recorder = _Recorder()

    monkeypatch.setattr(
        "reimagina.db.bootstrap.bootstrap_postgres",
        lambda **kwargs: {
            "app": {"created": app_created, "dsn": kwargs["app_dsn"], "migrated": True},
            "test": {"created": True, "dsn": kwargs["test_dsn"], "migrated": True},
        },
    )

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _FakeConn())

    def _seed_catalog(conn):
        recorder.seeded_catalog = True

    def _seed_assets(conn):
        recorder.seeded_assets = True

    def _provision(dsn):
        recorder.provisioned.append(dsn)

    monkeypatch.setattr("reimagina.indicators.catalog.seed_catalog", _seed_catalog)
    monkeypatch.setattr("reimagina.assets.registry.seed_assets", _seed_assets)
    # Provisioning the ops plane is a real connection to a real server, stubbed
    # for the same reason as the seeds: these tests are about which steps run
    # and with what, not about any of them working.
    monkeypatch.setattr(
        "reimagina.orchestration.dbos_app.ensure_system_database", _provision
    )
    return recorder


def _bootstrap(**overrides):
    return bootstrap_local_platform(
        app_dsn=POOLED_APP_DSN,
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
        **overrides,
    )


def test_bootstrap_local_platform_runs_create_then_migrate(monkeypatch):
    recorder = _stub_collaborators(monkeypatch, app_created=False)

    result = _bootstrap()

    assert result["app"]["created"] is False
    assert result["test"]["created"] is True
    assert recorder.seeded_catalog is True  # catalog seeded on bootstrap
    # The asset registry is seeded from code rather than from a migration body,
    # so bootstrap is the only thing that guarantees a fresh DB has it.
    assert recorder.seeded_assets is True


def test_bootstrap_provisions_the_ops_plane_system_schema(monkeypatch):
    """`make migrate` must leave a database the API can actually submit to.

    The API submits through `DBOSClient`, which never runs DBOS's schema
    migrations — only a worker's `launch()` creates them. Without this step a
    freshly-bootstrapped database answers every `POST /jobs` with a 500 until
    someone starts a worker, which is both an undocumented ordering dependency
    and the first thing a new installation hits.

    With no direct DSN named there is only one to use — the local install,
    where that one is already direct.
    """
    recorder = _stub_collaborators(monkeypatch)

    _bootstrap()

    assert recorder.provisioned == [POOLED_APP_DSN]


def test_bootstrap_provisions_over_the_direct_dsn_when_given_one(monkeypatch):
    """A schema migration must not be sent through the pooled DSN.

    See `ensure_system_database` for why a pooler cannot carry it. Asserted as
    a parameter rather than an environment variable so the caller stays in
    charge: `--direct-dsn` beats `$DATABASE_URL_DIRECT`, matching the two CLI
    commands that already take this flag.
    """
    recorder = _stub_collaborators(monkeypatch)

    _bootstrap(direct_dsn=DIRECT_DSN)

    assert recorder.provisioned == [DIRECT_DSN]


def test_alembic_runs_over_the_direct_dsn_too(monkeypatch):
    """Both DDL steps in one bootstrap must take the same route.

    `.env.example` names Alembic *first* among the things `DATABASE_URL_DIRECT`
    exists for. Migrating the app database over the pooled DSN while the
    ops-plane schema went direct left one function answering "which DSN carries
    DDL" two different ways, fourteen lines apart — the next person adding a
    step there had no rule to follow.

    (Alembic itself survives a transaction-mode pooler: every migration here
    deliberately avoids `CONCURRENTLY` so it runs inside one transaction, and a
    pooler pins a backend for a transaction's duration. This is consistency,
    not a repair.)
    """
    migrated: list[str] = []
    monkeypatch.setattr(
        "reimagina.storage.postgres.bootstrap.ensure_postgres_database",
        lambda admin_dsn, target_dsn: False,
    )
    monkeypatch.setattr(
        "reimagina.storage.postgres.bootstrap.run_postgres_migrations",
        lambda dsn: migrated.append(dsn),
    )

    bootstrap_postgres(
        app_dsn=POOLED_APP_DSN,
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
        direct_dsn=DIRECT_DSN,
    )

    assert migrated[0] == DIRECT_DSN, "the app database migrates over the direct route"


def test_bootstrap_postgres_reports_the_app_dsn_not_the_migration_route(monkeypatch):
    """The direct DSN is a route, not the application's identity.

    Callers print this dict; `make migrate` should still name the DSN the app
    will use, not the one the migration happened to travel over.
    """
    monkeypatch.setattr(
        "reimagina.storage.postgres.bootstrap.ensure_postgres_database",
        lambda admin_dsn, target_dsn: False,
    )
    monkeypatch.setattr(
        "reimagina.storage.postgres.bootstrap.run_postgres_migrations",
        lambda dsn: None,
    )

    result = bootstrap_postgres(
        app_dsn=POOLED_APP_DSN,
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
        direct_dsn=DIRECT_DSN,
    )

    assert result["app"]["dsn"] == POOLED_APP_DSN


def test_a_direct_dsn_naming_another_database_is_refused(monkeypatch):
    """A route to the same database may change host and port — not the name.

    Pooled and direct differ by port, so host/port cannot be compared. The
    database name can be, for free and without connecting, and getting it wrong
    is the failure this whole flag exists to prevent: Alembic migrating one
    database while the ops-plane schema lands in another.
    """
    monkeypatch.setattr(
        "reimagina.storage.postgres.bootstrap.ensure_postgres_database",
        lambda admin_dsn, target_dsn: False,
    )
    monkeypatch.setattr(
        "reimagina.storage.postgres.bootstrap.run_postgres_migrations",
        lambda dsn: None,
    )

    with pytest.raises(ValueError, match="different database"):
        bootstrap_postgres(
            app_dsn=POOLED_APP_DSN,
            test_dsn="postgresql://localhost/reimagina_test",
            admin_dsn="postgresql://localhost/postgres",
            direct_dsn="postgresql://direct:5432/some_other_db",
        )
