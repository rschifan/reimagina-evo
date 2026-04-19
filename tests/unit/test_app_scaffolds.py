from __future__ import annotations

from apps.api.app import create_app
from apps.cli.app import create_cli
from apps.worker.app import create_worker


def test_api_scaffold_boots():
    """create_app returns a FastAPI instance with the read-side routers mounted."""
    app = create_app()
    assert app.title == "reimagina API"
    paths = {getattr(route, "path", None) for route in app.routes}
    # Representative slice of the read-side surface — if these vanish, something
    # deeper than a rename has broken.
    assert "/health" in paths
    assert "/indicators" in paths
    assert "/tile-sources" in paths


def test_worker_scaffold_boots():
    assert create_worker()["app"] == "worker"


def test_cli_scaffold_boots():
    assert create_cli()["app"] == "cli"
