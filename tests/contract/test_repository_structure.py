from __future__ import annotations

import inspect
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
PY_PACKAGES_DIR = ROOT / "packages" / "py"
if str(PY_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PY_PACKAGES_DIR))


def test_expected_top_level_structure_exists():
    expected = [
        ROOT / "apps",
        ROOT / "packages" / "py" / "reimagina",
        ROOT / "packages" / "py" / "reimagina" / "storage",
        ROOT / "packages" / "py" / "reimagina" / "perf",
        ROOT / "packages" / "py" / "reimagina" / "tiles",
        ROOT / "packages" / "ts",
        ROOT / "infra" / "db" / "migrations",
        ROOT / "infra" / "db" / "migrations" / "common",
        ROOT / "infra" / "db" / "migrations" / "duckdb",
        ROOT / "infra" / "db" / "migrations" / "postgres",
        ROOT / "infra" / "db" / "seeds",
        ROOT / "infra" / "db" / "capability_matrix.md",
        ROOT / "infra" / "tiles",
        ROOT / "docs" / "architecture",
        ROOT / "tests" / "contract",
        ROOT / "tests" / "performance",
        ROOT / "tests" / "e2e",
    ]
    for path in expected:
        assert path.exists(), f"Missing expected path: {path}"


def test_api_worker_cli_scaffolds_exist():
    assert (ROOT / "apps" / "api" / "app.py").exists()
    assert (ROOT / "apps" / "worker" / "app.py").exists()
    assert (ROOT / "apps" / "cli" / "app.py").exists()


def _protocol_methods(protocol_cls: type) -> set[str]:
    """Return the set of non-dunder abstract-ish method names on a Protocol."""
    members = set()
    for name, member in inspect.getmembers(protocol_cls):
        if name.startswith("_"):
            continue
        if callable(member):
            members.add(name)
    return members


@pytest.mark.parametrize(
    "protocol_name, repo_getter",
    [
        ("SourceCatalogRepository", "catalog_repo"),
        ("FeatureRepository", "feature_repo"),
        ("ObservationRepository", "observation_repo"),
        ("FileAssetRepository", "file_asset_repo"),
        ("RasterLayerRepository", "raster_layer_repo"),
        ("AOIRepository", "aoi_repo"),
        ("OverlapRepository", "overlap_repo"),
        ("OverlapTypePairRepository", "overlap_type_pair_repo"),
        ("ServingReader", "serving_reader"),
        ("ExploreReader", "explore_reader"),
        ("TileSnapshotRepository", "tile_repo"),
        ("AOISummaryRepository", "aoi_summary_repo"),
        ("JobRunRepository", "job_run_repo"),
        ("IndicatorCatalogRepository", "indicator_catalog_repo"),
        ("IndicatorResultRepository", "indicator_result_repo"),
    ],
)
@pytest.mark.parametrize(
    "backend_module, backend_cls",
    [
        ("reimagina.storage.duckdb.backend", "DuckDBStorageBackend"),
        ("reimagina.storage.postgres.backend", "PostgresStorageBackend"),
    ],
)
def test_backends_implement_every_protocol_method(
    protocol_name, repo_getter, backend_module, backend_cls
):
    """Every Protocol method must have a same-named callable on the backend's repo class.

    This is a structural conformance check — no instances are constructed.
    It catches repos that forget to implement a Protocol method after the
    Protocol was extended.
    """
    import importlib

    from reimagina.storage import interfaces as interfaces_module

    protocol_cls = getattr(interfaces_module, protocol_name)
    backend_mod = importlib.import_module(backend_module)
    backend = getattr(backend_mod, backend_cls)

    # Resolve the concrete repo class from the backend method's return annotation.
    getter = getattr(backend, repo_getter)
    annotations = getattr(getter, "__annotations__", {})
    return_cls = annotations.get("return")
    assert (
        return_cls is not None
    ), f"{backend_cls}.{repo_getter} is missing a return annotation"
    if isinstance(return_cls, str):
        return_cls = eval(return_cls, backend_mod.__dict__)  # noqa: S307

    missing = _protocol_methods(protocol_cls) - _protocol_methods(return_cls)
    assert not missing, f"{return_cls.__name__} missing Protocol methods from {protocol_name}: {sorted(missing)}"


def test_explore_module_is_not_used_by_domain_logic_outside_cli():
    disallowed_import = re.compile(
        r"(from\s+reimagina\.data\s+import\s+explore|import\s+reimagina\.data\.explore)"
    )
    allowed_paths = {
        ROOT / "packages" / "py" / "reimagina" / "data" / "explore.py",
        ROOT / "apps" / "cli" / "app.py",
        ROOT / "apps" / "cli" / "explore.py",
    }
    for path in (ROOT / "packages" / "py" / "reimagina").rglob("*.py"):
        if path in allowed_paths:
            continue
        content = path.read_text()
        assert not disallowed_import.search(
            content
        ), f"Unexpected explore dependency in {path}"
