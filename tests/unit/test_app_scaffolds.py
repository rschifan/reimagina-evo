from __future__ import annotations

from apps.api.app import create_app
from apps.cli.app import create_cli


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


# The worker has no scaffold descriptor to boot. `create_worker()` returned a
# static dict that nothing consumed; `run_hatchet_worker` is the real entry
# point and is covered by `test_worker_entrypoint.py`.


def test_cli_scaffold_boots():
    assert create_cli()["app"] == "cli"
