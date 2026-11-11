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
        ROOT / "infra" / "db" / "migrations",
        ROOT / "infra" / "db" / "migrations" / "postgres",
        ROOT / "infra" / "db" / "seeds",
        ROOT / "infra" / "tiles",
        ROOT / "docs" / "architecture",
        ROOT / "tests" / "contract",
        ROOT / "tests" / "conformance",
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
        ("OpsRunReader", "ops_run_reader"),
    ],
)
@pytest.mark.parametrize(
    "backend_module, backend_cls",
    [
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
    assert return_cls is not None, (
        f"{backend_cls}.{repo_getter} is missing a return annotation"
    )
    if isinstance(return_cls, str):
        return_cls = eval(return_cls, backend_mod.__dict__)

    missing = _protocol_methods(protocol_cls) - _protocol_methods(return_cls)
    assert not missing, (
        f"{return_cls.__name__} missing Protocol methods from {protocol_name}: {sorted(missing)}"
    )


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
        assert not disallowed_import.search(content), (
            f"Unexpected explore dependency in {path}"
        )


# ── Dependency pinning ───────────────────────────────────────────────────────
#
# CI used to run `pip install -e '.[dev]'`, which re-resolved every dependency
# on every run and ignored the committed `uv.lock` entirely. A green build
# therefore said nothing about the versions that would actually deploy, and an
# upstream release could turn CI red on a PR that changed no code. `dbos` makes
# that concrete: its constraint is `>=2.28,<3` precisely because breaking
# changes ship inside its minor releases.
#
# Asserted as text rather than parsed YAML on purpose — these are claims about
# literal command strings, and a YAML parse would add a dependency this file
# does not otherwise need.

CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_installs_python_dependencies_from_the_lockfile():
    """No unpinned install may reach CI, and the lock must be enforced.

    ``--locked``, not ``--frozen``: both install from ``uv.lock``, but
    ``--frozen`` merely declines to update it, so a lock left stale by a
    pyproject edit is used silently. ``--locked`` asserts the lock would not
    change, which is what makes a pyproject edit without a re-lock a CI failure
    instead of a divergence nobody notices.
    """
    assert (ROOT / "uv.lock").is_file(), "the lockfile CI installs from must exist"
    ci = CI_WORKFLOW.read_text()

    installs = re.findall(r"run: (uv sync[^\n]*)", ci)
    assert installs, "no `uv sync` install step found in CI"
    for step in installs:
        assert "--locked" in step, (
            f"{step!r} does not assert the lockfile; `--frozen` and a bare sync "
            "both accept a stale lock"
        )

    # Executable lines only. A comment is allowed to name `pip install` — the
    # one above this test does, explaining what it replaced — and a check that
    # cannot tell an instruction from a description of one would forbid saying
    # why the rule exists.
    offenders = [
        stripped
        for line in ci.splitlines()
        if not (stripped := line.strip()).startswith("#") and "pip install" in stripped
    ]
    assert not offenders, (
        f"CI installs Python dependencies outside the lockfile: {offenders}"
    )


def test_the_linters_ci_gates_on_are_locked():
    """A gate whose *output* is the contract cannot float.

    Two ruff versions disagree about how to wrap the same line, so an unpinned
    one means ``ruff format --check`` fails CI on code the author's own ruff had
    just formatted. Both linters are declared in the dev extra so ``uv.lock``
    governs them, and both the workflow and the Makefile run them through ``uv``
    — which is what makes "it passed locally" mean something.
    """
    pyproject = (ROOT / "pyproject.toml").read_text()
    for tool in ("ruff", "mypy"):
        assert re.search(rf'"{tool}==[\d.]+"', pyproject), (
            f"{tool} gates CI but is not exact-pinned in the dev extra, so the "
            "lockfile does not govern the version that runs"
        )

    ci = CI_WORKFLOW.read_text()
    makefile = (ROOT / "Makefile").read_text()
    for tool in ("ruff", "mypy"):
        assert f"uv run {tool}" in ci, f"CI must invoke {tool} through uv"
    assert "RUFF := uv run ruff" in makefile
    assert "MYPY := uv run mypy" in makefile


def test_no_makefile_recipe_invokes_a_python_entry_point_bare():
    """A bare tool name runs whatever is first on PATH, not the project's.

    `make dev-api` shipped as bare `uvicorn` and was simply broken on any
    machine whose PATH offers a system-wide Python first: the server started
    under an interpreter without the project's dependencies and died on
    `import dbos`. The same shape in `ruff` was subtler and worse — a formatter
    that runs at the wrong version does not fail, it reformats, so `make lint`
    and CI disagreed about the same file.

    Both are the same rule: anything installed by `uv.lock` is invoked through
    `uv`, so the version that runs is the version the lockfile pins.
    """
    makefile = (ROOT / "Makefile").read_text()

    # Recipe lines are the tab-indented ones; everything else is a variable
    # definition, a comment, or a target.
    recipes = [
        line.lstrip("\t")
        for line in makefile.splitlines()
        if line.startswith("\t") and not line.lstrip("\t").startswith("@")
    ]
    # Entry points that come from the Python environment rather than the OS.
    managed = ("python", "pytest", "ruff", "mypy", "uvicorn", "alembic")
    offenders = [
        line
        for line in recipes
        for tool in managed
        if line.split(" ")[0] == tool  # first word, so `uv run ruff` is fine
    ]
    assert not offenders, (
        "Makefile recipes invoke a lockfile-managed tool without `uv run`, so "
        f"they resolve against PATH instead of the project: {offenders}"
    )


def test_every_ci_job_that_runs_the_orchestrator_sets_a_direct_dsn():
    """A suite that reaches the real orchestrator needs ``DATABASE_URL_DIRECT``.

    ``reimagina.orchestration.dbos_app.dbos_dsn`` raises when it is unset,
    deliberately, because DBOS otherwise falls back to a private SQLite file —
    a process that looks healthy and checkpoints nowhere recoverable. The guard
    is right; a CI job that exercises the orchestrator without the variable is
    not.

    This is asserted because omitting it was not loud. ``tests/integration``
    went red on a single end-to-end 404 whose own subject is route matching, and
    ``pytest (conformance)`` — a later step in the same job — was reported
    ``skipped`` rather than failed, so the port-contract suite stopped running
    on main for months without anything saying so.
    """
    ci = CI_WORKFLOW.read_text()

    # Split into job blocks on two-space-indented keys, the one indent level
    # jobs live at. Parsing YAML would be heavier and this file deliberately
    # treats the workflow as text (see the note above CI_WORKFLOW).
    blocks = re.split(r"\n  (?=[a-z][a-z0-9-]*:)", ci)
    suites_needing_it = ("tests/integration", "tests/conformance", "tests/recovery")

    for block in blocks:
        runs = [s for s in suites_needing_it if f"pytest {s}" in block]
        if not runs:
            continue
        assert "DATABASE_URL_DIRECT:" in block, (
            f"the CI job running {runs} constructs the real orchestrator, which "
            "requires DATABASE_URL_DIRECT; without it dbos_dsn() raises and the "
            "job fails on a test whose subject is something else entirely"
        )
