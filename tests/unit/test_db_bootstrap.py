from __future__ import annotations

from reimagina.db.bootstrap import REQUIRED_RELATIONS, bootstrap_local_platform


def test_required_relations_cover_first_spatial_layer():
    assert "curated_geo.data_source_catalog" in REQUIRED_RELATIONS
    assert "curated_geo.ingestion_run" in REQUIRED_RELATIONS
    assert "curated_geo.feature_type_catalog" in REQUIRED_RELATIONS
    assert "curated_geo.feature" in REQUIRED_RELATIONS
    assert "serving_geo.feature_v" in REQUIRED_RELATIONS


def test_bootstrap_local_platform_runs_create_then_migrate(monkeypatch):
    monkeypatch.setattr(
        "reimagina.db.bootstrap.bootstrap_postgres",
        lambda **kwargs: {
            "app": {"created": False, "dsn": kwargs["app_dsn"], "migrated": True},
            "test": {"created": True, "dsn": kwargs["test_dsn"], "migrated": True},
        },
    )

    # bootstrap_local_platform seeds the indicator catalog and the asset registry
    # after migrating, so a fresh DB isn't empty. Stub the DB round-trip to keep
    # this test offline.
    seeded: dict[str, bool] = {}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _FakeConn())
    monkeypatch.setattr(
        "reimagina.indicators.catalog.seed_catalog",
        lambda conn: seeded.setdefault("called", True),
    )
    monkeypatch.setattr(
        "reimagina.assets.registry.seed_assets",
        lambda conn: seeded.setdefault("assets_called", True),
    )
    # Bootstrap also provisions the ops plane's system schema, which is a real
    # connection to a real server. Stubbed for the same reason as the seeds:
    # this test is about the order of the steps, not about any of them working.
    monkeypatch.setattr(
        "reimagina.orchestration.dbos_app.ensure_system_database",
        lambda dsn: seeded.setdefault("ops_plane_called", True),
    )

    result = bootstrap_local_platform(
        app_dsn="postgresql://localhost/reimagina",
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
    )

    assert result["app"]["created"] is False
    assert result["test"]["created"] is True
    assert seeded.get("called") is True  # catalog seeded on bootstrap
    # The asset registry is seeded from code rather than from a migration body,
    # so bootstrap is the only thing that guarantees a fresh DB has it.
    assert seeded.get("assets_called") is True


def _provisioning_stubs(monkeypatch) -> list[str]:
    """Stub bootstrap's collaborators; return the DSNs provisioning is asked for."""
    monkeypatch.setattr(
        "reimagina.db.bootstrap.bootstrap_postgres",
        lambda **kwargs: {
            "app": {"created": True, "dsn": kwargs["app_dsn"], "migrated": True},
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
    monkeypatch.setattr("reimagina.indicators.catalog.seed_catalog", lambda conn: None)
    monkeypatch.setattr("reimagina.assets.registry.seed_assets", lambda conn: None)

    provisioned: list[str] = []
    monkeypatch.setattr(
        "reimagina.orchestration.dbos_app.ensure_system_database",
        lambda dsn: provisioned.append(dsn),
    )
    return provisioned


def _bootstrap() -> None:
    bootstrap_local_platform(
        app_dsn="postgresql://pooler/reimagina",
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
    )


def test_bootstrap_provisions_the_ops_plane_system_schema(monkeypatch):
    """`make migrate` must leave a database the API can actually submit to.

    The API submits through `DBOSClient`, which never runs DBOS's schema
    migrations — only a worker's `launch()` creates them. Without this step a
    freshly-bootstrapped database answers every `POST /jobs` with a 500 until
    someone starts a worker, which is both an undocumented ordering dependency
    and the first thing a new installation hits.

    With no `DATABASE_URL_DIRECT` there is only one DSN to use, and it is
    already a direct one — the local `make migrate` case.
    """
    provisioned = _provisioning_stubs(monkeypatch)
    monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)

    _bootstrap()

    assert provisioned == ["postgresql://pooler/reimagina"]


def test_bootstrap_provisions_over_the_direct_dsn_when_there_is_one(monkeypatch):
    """A schema migration must not be sent through the pooled DSN.

    `.env.example` assigns the roles: `REIMAGINA_APP_DSN` is the one production
    "points at pgbouncer", `DATABASE_URL_DIRECT` the one for "Alembic,
    LISTEN/NOTIFY". This call is both of those things — DBOS serialises
    concurrent deploys with a *session* advisory lock on an AUTOCOMMIT
    connection, which transaction-mode pooling can scatter across backends so
    that it guards nothing and is never released, and the same migration
    installs the LISTEN/NOTIFY triggers the progress stream reads.
    """
    provisioned = _provisioning_stubs(monkeypatch)
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql://direct:5432/reimagina")

    _bootstrap()

    assert provisioned == ["postgresql://direct:5432/reimagina"]
