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


def test_the_shared_backend_never_imports_a_deployable_app():
    """`packages/py/reimagina` is below `apps/`, and the arrow points one way.

    Written down because the first thing that tempts anyone to turn it round
    is a rule the CLI implemented first and the API router then needs: the
    four edits one proposal writes, the density precheck, the refusal of a
    non-finite value. Importing those from `apps.cli` would make the HTTP
    surface depend on the command line, and the cheap-looking alternative —
    the router re-deriving them — is how `floors` and `num_levels` drift
    apart. They live in `reimagina.scenarios.proposal` instead, and this is
    what stops the shortcut being taken the other way.
    """
    disallowed_import = re.compile(r"^\s*(from\s+apps[\s.]|import\s+apps[\s.])", re.M)
    offenders = [
        path
        for path in (ROOT / "packages" / "py" / "reimagina").rglob("*.py")
        if disallowed_import.search(path.read_text())
    ]
    assert offenders == [], (
        "the shared backend namespace must not import a deployable app: "
        f"{[str(p.relative_to(ROOT)) for p in offenders]}"
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


def _ci_jobs(ci: str) -> dict[str, str]:
    """Split the workflow's ``jobs:`` mapping into its job bodies, by name.

    Text rather than parsed YAML, for the reason given at the top of this
    section. Job headers are the two-space keys *after* the ``jobs:`` line —
    ``on:`` has two-space keys of its own, and matching those would invent jobs
    called ``push`` and ``schedule``.
    """
    assert "\njobs:\n" in ci, "CI workflow has no `jobs:` block"
    jobs: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in ci.split("\njobs:\n", 1)[1].splitlines():
        if line[:1] not in ("", " ", "#"):
            break  # back to column 0: the jobs mapping has ended
        header = re.fullmatch(r"  ([A-Za-z0-9_-]+):[ \t]*", line)
        if header:
            current = jobs.setdefault(header.group(1), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(body) for name, body in jobs.items()}


def test_ci_gives_the_db_backed_suites_a_direct_postgres_dsn():
    """A suite that reaches the real ops plane needs ``DATABASE_URL_DIRECT``.

    This is the one missing line that kept CI red for months. The integration
    suite reaches the real orchestrator through the API's job routes, and
    ``dbos_dsn()`` raises rather than let DBOS fall back to SQLite and write
    checkpoints somewhere unrecoverable. The job set ``TEST_DATABASE_URL`` and
    ``REIMAGINA_APP_DSN`` but not this one, so the step failed on a single
    test — and because ``pytest (conformance)`` is a *later step in the same
    job*, the Orchestrator port contract was reported skipped rather than run,
    and had not executed on main in months.

    A red build is visible; a suite that silently never runs is not. Asserting
    the variable here is what turns removing it back into one failing test.
    """
    ci = CI_WORKFLOW.read_text()
    jobs = _ci_jobs(ci)
    assert jobs, "no CI jobs found to check"

    # A workflow-level `env:` reaches every job, so the variable is in scope
    # whether it is declared there or on the job itself.
    workflow_env = re.search(r"\nenv:\n((?:[ \t]+[^\n]*\n|\n)*)", ci)
    in_workflow_env = bool(
        workflow_env and "DATABASE_URL_DIRECT:" in workflow_env.group(1)
    )

    db_backed = {
        name: body
        for name, body in jobs.items()
        if re.search(r"pytest tests/(integration|conformance)", body)
    }
    assert db_backed, "no CI job runs the DB-backed suites"

    for name, body in db_backed.items():
        assert in_workflow_env or "DATABASE_URL_DIRECT:" in body, (
            f"CI job {name!r} runs a suite that reaches the DBOS ops plane but "
            "does not set DATABASE_URL_DIRECT. dbos_dsn() raises without it, so "
            "the step fails and every later step in the job — conformance "
            "included — never runs at all"
        )


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


# Documents that describe the system as built. A path or a command named in one
# of these has to resolve. Architecture *proposals*, which legitimately name
# files that do not exist yet, are kept out of the repository and out of this
# list: adding one would force it to be implemented before it could be written
# down.
AS_BUILT_DOCS = (
    "README.md",
    "docs/*.md",
    "docs/architecture/*.md",
    "docs/delivery/*.md",
)

# Repo-relative source paths, as the docs write them in links and prose.
_DOC_PATH_RE = re.compile(
    r"(?:packages/py|apps|infra|tests|scripts)/[A-Za-z0-9_./-]*"
    r"\.(?:py|yml|yaml|json|sql|ts|tsx)"
)

# `python -m apps.cli.app <group> <command>` as the docs spell it, including
# across a trailing backslash continuation.
_DOC_CLI_RE = re.compile(
    r"python -m apps\.cli\.app\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?"
)


def _as_built_doc_files() -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in AS_BUILT_DOCS:
        for path in sorted(ROOT.glob(pattern)):
            seen.setdefault(path, None)
    assert seen, "no as-built docs matched; the glob list is wrong"
    return list(seen)


def test_every_source_path_the_as_built_docs_name_exists():
    """A dead path in a document that claims to describe the system is a lie.

    Two of these were found at once, and they had the same cause: `39ab70a`
    deleted `jobs/rollup.py` and four documents went on pointing at it, while
    `r003_feature_schema.py` had been `r003_feature_tables.py` since it was
    written. Neither is caught by any linter, because the reference lives in
    prose; both are caught by reading the filesystem.
    """
    missing: list[str] = []
    for doc in _as_built_doc_files():
        for match in sorted(set(_DOC_PATH_RE.findall(doc.read_text()))):
            if not (ROOT / match).exists():
                missing.append(f"{doc.relative_to(ROOT)} -> {match}")
    assert not missing, (
        "as-built docs name source files that do not exist. Either the file "
        "moved and the reference needs updating, or the feature was removed "
        "and the passage describing it is stale:\n  " + "\n  ".join(missing)
    )


def test_every_cli_command_the_as_built_docs_quote_is_registered():
    """A documented command that does not exist sends the reader nowhere.

    A guide quoted `indicators rollup` for three months after `39ab70a`
    removed it, so what a reader first learned about the indicator layer was
    false.

    Resolved against Typer's registry rather than by running the CLI: a
    subprocess per command would make this slow enough to skip, and `--help`
    exits 0 for a group whose subcommand is wrong.
    """
    from apps.cli.app import app as cli_app

    groups = {
        group.name: {cmd.name for cmd in group.typer_instance.registered_commands}
        | {sub.name for sub in group.typer_instance.registered_groups}
        for group in cli_app.registered_groups
        if group.name and group.typer_instance is not None
    }
    top_level = {
        cmd.name or (cmd.callback.__name__.replace("_", "-") if cmd.callback else "")
        for cmd in cli_app.registered_commands
    }

    unknown: list[str] = []
    for doc in _as_built_doc_files():
        text = doc.read_text()
        for group, command in sorted(set(_DOC_CLI_RE.findall(text))):
            if group in top_level and not command:
                continue
            if group not in groups:
                if group not in top_level:
                    unknown.append(f"{doc.relative_to(ROOT)} -> {group}")
                continue
            # A group named with no subcommand (`... app explore`) is a usage
            # listing, not a claim that a command exists.
            if command and command not in groups[group]:
                unknown.append(f"{doc.relative_to(ROOT)} -> {group} {command}")
    assert not unknown, (
        "as-built docs quote CLI commands that are not registered:\n  "
        + "\n  ".join(unknown)
    )


def test_the_acceptance_run_exercises_every_shipped_indicator():
    """A capability the acceptance run skips is a capability nobody checks.

    `scripts/acceptance_run.sh` is the evidence behind Schedule A's "the code
    builds and runs" criterion, and its value rests entirely on being
    exhaustive. An indicator added to `SHIPPED_INDICATORS` and not to the
    script would leave a documented capability unobserved while the run still
    came out green — the quietest way for the proof to stop being one.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    script = (ROOT / "scripts" / "acceptance_run.sh").read_text()
    missing = [spec.slug for spec in SHIPPED_INDICATORS if spec.slug not in script]
    assert not missing, (
        "scripts/acceptance_run.sh does not compute these shipped indicators, "
        "so the acceptance run would pass without ever exercising them: "
        f"{missing}"
    )


def test_ci_requires_the_database_it_attaches_rather_than_skipping():
    """A job with a Postgres service must not pass by skipping past it.

    pytest exits 0 when every test skips, so a run whose database never came
    up reports green having executed nothing — and "322 skipped, 6 passed"
    reads as success in every summary anyone looks at. The counts are the only
    place it shows.

    This is the same failure that kept `pytest (conformance)` green and empty
    for months on a missing `DATABASE_URL_DIRECT`. That was fixed by supplying
    the variable; this asserts the *class* is closed, by requiring the job
    that attaches a Postgres service to say so, so `conftest.migrated_db`
    fails instead of skipping when it cannot connect.
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    jobs = _ci_jobs(ci)
    db_jobs = {
        name: body
        for name, body in jobs.items()
        if "TEST_DATABASE_URL" in body and "services:" in body
    }
    assert db_jobs, "no CI job attaches a database; this test is watching nothing"

    missing = [
        name
        for name, body in db_jobs.items()
        if 'REIMAGINA_REQUIRE_DB: "1"' not in body
    ]
    assert not missing, (
        "these CI jobs attach a Postgres service but do not set "
        'REIMAGINA_REQUIRE_DB: "1", so an unreachable database would make '
        f"them skip their way to a green result: {missing}"
    )


def test_ci_gives_the_integration_job_what_its_conditional_tests_need():
    """Two integration tests skip on prerequisites other than the database.

    ``REIMAGINA_REQUIRE_DB`` only turns an unreachable database into a
    failure. The PMTiles end-to-end export skips without ``ogr2ogr`` and the
    pooler guard skips without ``REIMAGINA_POOLED_TEST_DSN``, so in a job that
    provides neither they never ran anywhere automated: every run reported
    them SKIPPED and the export's connection-string bug shipped green.
    """
    body = _ci_jobs((ROOT / ".github" / "workflows" / "ci.yml").read_text())[
        "test-py-integration"
    ]
    assert "gdal-bin" in body, "ogr2ogr ships in gdal-bin, not libgdal-dev"
    assert "REIMAGINA_POOLED_TEST_DSN:" in body
    assert "edoburu/pgbouncer" in body, "the pooler guard needs a real pooler"


def test_the_api_and_martin_resolve_to_the_same_live_tile_url_shape():
    """Switching tile servers must be one environment variable, or nothing.

    This is the pass/fail criterion the API-served MVT route was built
    against: *if switching between Martin and the API is anything more
    than changing one environment variable, this is not worth building.* A
    second tile implementation the frontend, the manifest or the resolver has
    to know about is two code paths to keep in step, and the one that is not
    in production rots.

    So the assertion is not that both work — it is that both produce the
    **same URL shape** through the same resolver, differing only in origin.
    `tiles.sources._martin_base_and_default_function` treats a trailing path
    segment as a function name only when it is a known one, which is what
    makes `…:8001/tiles` resolve as a base rather than as a function called
    "tiles". That behaviour predates this route and is load-bearing for it,
    so it is pinned here rather than left as luck.
    """
    from reimagina.tiles.sources import _martin_base_and_default_function

    martin_base, martin_fn = _martin_base_and_default_function("http://localhost:3000")
    api_base, api_fn = _martin_base_and_default_function("http://localhost:8001/tiles")

    assert martin_fn == api_fn == "get_tile", (
        "the two deployments disagree about the default tile function, so the "
        "same manifest would resolve to different layers"
    )
    assert martin_base == "http://localhost:3000"
    assert api_base == "http://localhost:8001/tiles", (
        "'/tiles' was consumed as a function name, so every API-served tile "
        "URL would be missing its path prefix and 404"
    )

    # The shape the resolver builds from each, which is what the frontend
    # fetches. Identical but for the origin.
    def _template(base: str, function: str) -> str:
        return f"{base}/{function}/{{z}}/{{x}}/{{y}}"

    assert _template(martin_base, martin_fn) == (
        "http://localhost:3000/get_tile/{z}/{x}/{y}"
    )
    assert _template(api_base, api_fn) == (
        "http://localhost:8001/tiles/get_tile/{z}/{x}/{y}"
    )


def test_the_tile_function_registry_is_the_only_list_of_servable_functions():
    """One list, or the two servers drift.

    `tiles.sources` decides whether a URL's last segment is a function name;
    `apps.api.routers.mvt` decides whether it will call one. A function the
    resolver advertises but the route refuses is a 404 in a map; a function
    the route serves but the resolver never names is dead code. Two
    frozensets in two modules is how that happens, so there is one, and this
    asserts neither consumer has quietly grown its own.
    """
    from reimagina.tiles import functions as tile_functions
    from reimagina.tiles import sources

    assert sources._KNOWN_MARTIN_FUNCTION_TAILS is tile_functions.TILE_FUNCTIONS, (
        "tiles.sources has its own copy of the servable-function list again"
    )

    mvt = (ROOT / "apps" / "api" / "routers" / "mvt.py").read_text()
    assert "TILE_FUNCTIONS" in mvt, "the MVT route no longer reads the registry"
    for name in tile_functions.TILE_FUNCTIONS:
        assert f'"{name}"' not in mvt, (
            f"{name!r} is hard-coded in the MVT route as well as being in the "
            "registry, so the allowlist now has two sources of truth"
        )


def test_a_relative_tile_base_url_resolves_same_origin():
    """The form that needs no CORS, and the one the docs recommend for dev.

    Tiles served by the API are cross-origin from the web dev server
    (`:5173` to `:8001`), so they depend on the API's CORS allowlist — which
    names `http://localhost:5173` and therefore rejects a browser that
    reached the page as `http://127.0.0.1:5173`. Measured: 53 tile requests,
    53 `net::ERR_FAILED`, and a map that renders nothing with no console
    error to explain it.

    The web app already proxies `/api` to the API, so a **relative** base
    makes tiles same-origin and the whole question disappears. Verified in a
    real browser from an origin deliberately absent from the allowlist: 38
    tile responses, 200s and 204s, map drawn.

    This pins the resolver behaviour that makes it work — a base with no
    scheme falls to the rsplit branch, and `tiles` is not a known function
    tail, so the whole string stays the base.
    """
    from reimagina.tiles.sources import _martin_base_and_default_function

    base, function = _martin_base_and_default_function("/api/tiles")
    assert base == "/api/tiles", (
        "'tiles' was taken for a function name, so every tile URL would lose "
        "its proxy prefix and 404 against the dev server"
    )
    assert function == "get_tile"
    assert f"{base}/{function}/{{z}}/{{x}}/{{y}}" == ("/api/tiles/get_tile/{z}/{x}/{y}")


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
    #
    # ⚠ Sliced to the `jobs:` section first, because "two-space-indented key"
    # only means "a job" inside it. Top-level `on:` has two-space-indented
    # children too, so `push:` and `pull_request:` were arriving here as job
    # blocks — and a block is only inspected when it contains `pytest <suite>`,
    # so the failure mode was a non-job block satisfying the assertion for a
    # real job, or a real job's text split across two of them. Cutting at the
    # next top-level key keeps this correct whatever order the file's top-level
    # keys are written in.
    assert "\njobs:\n" in ci, (
        "the workflow has no bare `jobs:` line at column zero, so the split "
        "below cannot find the jobs section — if the key grew a trailing "
        "comment or the file arrived with CRLF endings, fix the pattern here "
        "rather than letting this read as an IndexError with no reason"
    )
    jobs_section = ci.split("\njobs:\n", 1)[1]
    jobs_section = re.split(r"\n(?=[a-z])", jobs_section)[0]
    blocks = re.split(r"\n  (?=[a-z][a-z0-9-]*:)", jobs_section)
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


def test_apt_steps_cannot_fail_the_build():
    """A package-mirror outage must not read as a verdict on this code.

    On 2026-11-11 the GitHub runners could not resolve `security.ubuntu.com`.
    `apt-get` retried for 1h40m and failed, and because the system-dependency
    step precedes `uv sync`, `pytest` never ran in either job that has one —
    two of the four. The suite was green locally throughout.

    The step is a documented *fallback*: fiona and rasterio ship cp312
    manylinux wheels carrying their own GDAL, so it is not normally used at
    all. Anything that cannot fail the build should not be able to fail the
    build, so every apt step carries `continue-on-error` and a timeout.
    """
    ci = CI_WORKFLOW.read_text()
    blocks = re.split(r"\n      - name: ", ci)
    apt_steps = [b for b in blocks if "apt-get install" in b]

    assert apt_steps, "expected at least one apt step; update this test if they went"
    for step in apt_steps:
        name = step.splitlines()[0]
        assert "continue-on-error: true" in step, (
            f"apt step {name!r} can fail the build; a mirror outage would stop "
            "pytest from running at all"
        )
        assert "timeout-minutes:" in step, (
            f"apt step {name!r} has no timeout; apt retried for 1h40m before "
            "giving up once already"
        )
