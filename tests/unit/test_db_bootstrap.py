from __future__ import annotations

from reimagina.db.bootstrap import REQUIRED_RELATIONS, bootstrap_local_platform

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
