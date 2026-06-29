from __future__ import annotations

from apps.api.app import create_app
from apps.cli.app import create_cli
from apps.worker.app import create_worker


def test_api_scaffold_boots():
    """create_app returns a FastAPI instance with the read-side routers mounted."""
    app = create_app(initialise_pool=False)
    assert app.title == "reimagina API"
    # Read paths from the OpenAPI schema, not app.routes: FastAPI >=0.135
    # stops flattening included routers into app.routes (they become a
    # single _IncludedRouter with path=None), so the shallow walk misses
    # every router-mounted route. The schema is stable across versions.
    paths = set(app.openapi()["paths"])
    # Representative slice of the read-side surface — if these vanish, something
    # deeper than a rename has broken.
    assert "/health" in paths  # alias kept for backwards compatibility
    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert "/indicators" in paths
    assert "/tile-sources" in paths


def test_worker_scaffold_boots():
    assert create_worker()["app"] == "worker"


def test_cli_scaffold_boots():
    assert create_cli()["app"] == "cli"
