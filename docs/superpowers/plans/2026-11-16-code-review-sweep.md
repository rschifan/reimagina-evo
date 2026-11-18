# Code-Review Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 12 latent defects an xhigh code review found on `origin/main`, so the commit tagged for the 20 November Tec delivery has no known defects.

**Architecture:** Six independent tasks, one commit each, on one branch off `origin/main` @ `a89fe83`. Two fixes are generalisations rather than patches: a single app-level exception handler replaces a per-route `try/except` that three call sites needed, and a status-based filter replaces a class-based special case. The rest are local.

**Tech Stack:** Python 3.12 (FastAPI, psycopg2, SQLAlchemy via DBOS, pytest), Svelte 5 + Vite + TypeScript (vitest), PostgreSQL 17 + PostGIS, pgbouncer, ruff, uv, npm.

**Spec:** `docs/superpowers/specs/2026-11-16-code-review-sweep-design.md`

## Global Constraints

- Base branch: `origin/main` @ `a89fe83`. Not `delivery/schedule-a` (72 behind).
- **Authorship, non-negotiable** (`AGENTS.md`): author and committer must be `Rossano <rossano.schifanella@gmail.com>`. No `Co-Authored-By`. No agent attribution, session URL or AI mention in commit messages, code comments, or anything written into the repo.
- **No new dependencies.** `uv.lock` and `apps/web/package-lock.json` must be byte-identical at the end. No PyYAML.
- **No refactoring beyond what a fix needs.**
- `docs/delivery/**` must stay byte-identical. It is hash-locked in `docs/delivery/SHA256SUMS` and `tests/contract/test_repository_structure.py:449` recomputes it.
- Node 24 for anything under `apps/web` — Node 22 produces false vitest failures.
- Deviation from the spec's grouping: the spec put #2 and #3 in one commit. They are Tasks 3 and 4 here, because Task 4's mechanism has to be verified against a running pooler first and can be rejected while Task 3 stands.

## Review Focus

Five conditions the spec implies that no existing test exercises. Each has a test assigned to the task owning the code.

1. **A `RasterioIOError` must not echo its own message to the client.** It carries the COG path, and for a remote COG the URL with any SAS token in it. This is the whole point of #4 and nothing currently asserts it. → Task 2.
2. **`POST /jobs/{id}/rerun` and `POST /scenarios/{id}/recompute` must answer 503 on an unprovisioned ops plane.** Only `POST /jobs` is tested today; those two are the finding. → Task 1.
3. **A bare `bootstrap-db` must touch no second database.** The default flips in Task 3, and nothing asserts the absence of a side effect. → Task 3.
4. **`VITE_PORT=""` must give 5173, not 0.** `apps/web` has no test of its Vite config at all. → Task 6.
5. **`preflight_ports.py` with a non-numeric argument must exit 2 with a message, not a traceback.** → Task 5.

---

### Task 0: Branch and baseline

No code. This task exists because every later task compares against its output, and because a red baseline means stop rather than fix.

**Files:** none.

**Interfaces:**
- Produces: branch `fix/code-review-sweep`; a recorded baseline of pass/fail counts for each gate command below.

- [ ] **Step 1: Fetch and branch**

```bash
git fetch origin
git switch -c fix/code-review-sweep a89fe83
git log --oneline -1   # expect: a89fe83 feat(scenarios): scenario authoring and delta tiles (#94)
```

- [ ] **Step 2: Rebuild the environment**

This machine's `.venv` is broken — `import numpy` raises `ModuleNotFoundError: No module named 'numpy._core._multiarray_umath'`.

```bash
uv sync --locked --extra dev
```

Then `cd apps/web && npm ci` under Node 24.

- [ ] **Step 3: Record the baseline**

Run each and write the summary line into the task's notes:

```bash
uv run pytest tests/unit tests/contract -q
cd apps/web && npx tsc --noEmit && npx vitest run
uv run ruff check && uv run ruff format --check
```

Expected: all green. **If any is red at `a89fe83`, stop and report** — a pre-existing failure is not this branch's to absorb silently.

- [ ] **Step 4: Confirm the lockfiles are clean**

```bash
git status --porcelain uv.lock apps/web/package-lock.json   # expect: empty
```

---

### Task 1: One 503 handler for every submit path (#1)

`OpsPlaneNotProvisioned` is caught at `apps/api/routers/jobs.py:225` and nowhere else. `jobs.py:585` (rerun) and `apps/api/routers/scenarios.py:800` (recompute) both let it reach `errors.unhandled_error_handler` as a 500.

**Files:**
- Modify: `apps/api/errors.py` — add `ops_plane_not_provisioned_handler`
- Modify: `apps/api/app.py:145` — register it
- Modify: `apps/api/routers/jobs.py:216-227` — delete the per-route `except`
- Modify: `apps/api/routers/scenarios.py:34-66` — docstring status table
- Modify: `docs/architecture/API.md:110`, `docs/architecture/JOB_LAYER.md:442`
- Test: `tests/contract/test_jobs_api.py`, `tests/contract/test_scenarios_api.py`

**Interfaces:**
- Consumes: `reimagina.orchestration.port.OpsPlaneNotProvisioned` (a `RuntimeError`); `apps/api/errors._error_response(*, status_code: int, error_code: str, message: str, request_id: str) -> JSONResponse`; `errors._request_id(request) -> str`.
- Produces: 503 responses shaped `{"error": {"code": "ops_plane_not_provisioned", "message": str(exc), "request_id": ...}}` with the `X-Request-Id` header, from every route.

- [ ] **Step 1: Write the failing tests**

Three. The first replaces the assertion in the existing `test_an_unprovisioned_ops_plane_is_503_and_names_the_remedy` (`tests/contract/test_jobs_api.py:705`) — keep its name and docstring, change only the body's last two lines:

```python
    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "ops_plane_not_provisioned"
    assert "bootstrap-db" in body["message"], (
        "the error must name the command that fixes it"
    )
    assert body["request_id"]
```

The second and third are new, in the same file and in `tests/contract/test_scenarios_api.py`, following the `_fake_connections` / `_install` fixtures the first one uses:

```python
def test_rerun_answers_503_when_the_ops_plane_is_unprovisioned(monkeypatch):
    """The rerun path submits too, and went uncaught — a 500 on a deployment state."""
```

```python
def test_recompute_answers_503_when_the_ops_plane_is_unprovisioned(monkeypatch):
    """Materialisation counts are deliberately absent: see the design, #1."""
```

Each drives its route with a fake orchestrator whose `submit_run` raises `OpsPlaneNotProvisioned()`, then asserts `503` and `resp.json()["error"]["code"] == "ops_plane_not_provisioned"`. The recompute one additionally asserts `"materialized" not in resp.json()`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/contract/test_jobs_api.py -k ops_plane tests/contract/test_scenarios_api.py -k unprovisioned -v`
Expected: the rerun and recompute tests FAIL with 500; the `/jobs` one FAILS with `KeyError: 'error'`.

- [ ] **Step 3: Add `ops_plane_not_provisioned_handler` to `apps/api/errors.py`**

```python
async def ops_plane_not_provisioned_handler(
    request: Request, exc: OpsPlaneNotProvisioned
) -> JSONResponse
```

503, `error_code="ops_plane_not_provisioned"`, `message=str(exc)`. `log.warning`, not `log.exception` — a skipped provisioning step is not a fault with a stack to read.

The docstring must say why `str(exc)` is echoed here when this module's own header says "The internal message is never echoed back": the message is this project's own sentence naming `bootstrap-db`, with nothing internal in it. Import `OpsPlaneNotProvisioned` at module scope — `reimagina/orchestration/port.py` imports only `collections.abc`, `dataclasses` and `typing`, so it does not pull the engine in.

- [ ] **Step 4: Register it in `apps/api/app.py`**

Immediately before `app.add_exception_handler(Exception, errors.unhandled_error_handler)` at line 145. Starlette dispatches by MRO, so a specific class registered alongside the catch-all wins regardless of order; put it before for readability.

- [ ] **Step 5: Delete the per-route `except` in `apps/api/routers/jobs.py`**

Remove the `except OpsPlaneNotProvisioned as exc:` clause and its comment block (216-227), leaving the bare `with ... as conn, conn:` and the `orchestrator.submit_run(...)` call. Remove the now-unused `from reimagina.orchestration.port import OpsPlaneNotProvisioned` at line 187.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/contract/test_jobs_api.py tests/contract/test_scenarios_api.py -q`
Expected: PASS.

- [ ] **Step 7: Update the three docs**

- `apps/api/routers/scenarios.py:34-66` — add a row to the status table for the 503, saying it carries no materialisation counts and why (the counts are useless until an operator runs `bootstrap-db`, same reasoning the module already applies to connection-class failures).
- `docs/architecture/API.md:110` — move the 503 row out of the "Refusals that name their own remedy (`detail`)" table into the envelope description, and say every submit path answers it, not only `POST /jobs`.
- `docs/architecture/JOB_LAYER.md:442` — "which `POST /jobs` answers as **503**" → every submit path.

- [ ] **Step 8: Commit**

```bash
git add apps/api/errors.py apps/api/app.py apps/api/routers/jobs.py apps/api/routers/scenarios.py docs/architecture/API.md docs/architecture/JOB_LAYER.md tests/contract/test_jobs_api.py tests/contract/test_scenarios_api.py
git commit -m "fix(api): answer 503 for an unprovisioned ops plane on every submit path"
```

---

### Task 2: Stop the raster handlers echoing exception text (#4)

`apps/api/app.py:200-202` filters only `Exception` out of titiler's `DEFAULT_STATUS_CODES`, leaving `RasterioIOError`, `RasterioError` and `RioTilerError` mapped to 500 with titiler's `{"detail": str(exc)}`.

**Files:**
- Modify: `apps/api/app.py:200-202`
- Modify: `docs/architecture/API.md:97`
- Test: `tests/unit/test_api_error_envelope.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: no new symbols. After this, every 5xx in the app answers `errors.unhandled_error_handler`'s envelope.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_api_error_envelope.py`, beside the two tests already there:

```python
def test_raster_5xx_handlers_are_not_registered_at_all():
    """`RasterioIOError` carries the COG path — a SAS-tokened URL for a remote one.

    titiler's `exception_handler_factory` answers `{"detail": str(exc)}`, so
    mapping these to 500 handed the client the message verbatim and dropped the
    request id with it. Unregistered, they reach `Exception` by MRO and get the
    envelope.
    """
    from rasterio.errors import RasterioError, RasterioIOError
    from rio_tiler.errors import RioTilerError

    app = create_app(initialise_pool=False)
    for exc in (RasterioIOError, RasterioError, RioTilerError):
        assert exc not in app.exception_handlers, (
            f"{exc.__name__} still carries titiler's detail handler"
        )
```

The non-5xx side is already covered: `test_titilers_raster_specific_handlers_are_still_registered` (line 30) asserts `TileOutsideBounds` stays registered, and it must keep passing.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_api_error_envelope.py -v`
Expected: the first FAILS — the three classes still map to titiler's handler.

- [ ] **Step 3: Filter by status in `apps/api/app.py`**

```python
    raster_status_codes = {
        exc: code for exc, code in DEFAULT_STATUS_CODES.items() if code < 500
    }
```

Rewrite the `⚠ Exception is filtered out` comment above it: the rule is now "titiler's map is wanted only where it says something other than 'internal error'", the `is not Exception` special case was a symptom of that, and 5xx classes fall through to `errors.unhandled_error_handler` for the envelope and the request id. Record the accepted consequence: those three now log a traceback per request via `log.exception`, which is correct for a COG the process cannot open.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_api_error_envelope.py tests/integration/test_api_routes.py -q`
Expected: PASS.

- [ ] **Step 5: Update `docs/architecture/API.md:97`**

"`create_app` now filters that one entry out and keeps the raster-specific ones" → the `code < 500` rule, and what it means for the three 5xx classes.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app.py docs/architecture/API.md tests/unit/test_api_error_envelope.py
git commit -m "fix(api): keep titiler's non-5xx status map only, so raster errors use the envelope"
```

---

### Task 3: Make the test database opt-in (#2)

`packages/py/reimagina/storage/postgres/bootstrap.py:81,83` create and migrate the test database unconditionally. `docs/architecture/OPS_PLANE_RUNBOOK.md:42` prints `bootstrap-db --app-dsn "$REIMAGINA_APP_DSN"` as the production remedy, so following it on a production host creates `reimagina_test` there.

**Files:**
- Modify: `packages/py/reimagina/storage/postgres/bootstrap.py:36-95` (`bootstrap_postgres`)
- Modify: `packages/py/reimagina/db/bootstrap.py:42-60` (`bootstrap_local_platform`)
- Modify: `apps/cli/app.py:283-343` (`bootstrap_db`)
- Modify: `Makefile:99`, `scripts/acceptance_run.sh:84`, `scripts/mexicali_run.sh:214`
- Test: `tests/unit/test_db_bootstrap.py`, `tests/unit/test_cli_entrypoint.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `bootstrap_postgres(*, app_dsn: str, test_dsn: str | None = None, admin_dsn: str, direct_dsn: str | None = None)` and `bootstrap_local_platform(*, app_dsn=..., test_dsn: str | None = None, admin_dsn=..., direct_dsn=None)`. Both omit the `"test"` key from the returned dict when `test_dsn is None`. CLI flag `--with-test-db / --no-test-db`, default off.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_db_bootstrap.py`:

```python
def test_bootstrap_postgres_without_a_test_dsn_touches_one_database(monkeypatch):
    """`bootstrap-db` is the production remedy the ops runbook prints.

    Creating and migrating `reimagina_test` wherever that is run put a test
    database on a production server — `test_dsn` defaulted to localhost and the
    production host *is* localhost to itself.
    """
```

It records every `(admin_dsn, target_dsn)` passed to `ensure_postgres_database` and every DSN passed to `run_postgres_migrations`, calls `bootstrap_postgres(app_dsn=..., admin_dsn=..., test_dsn=None)`, and asserts each list has exactly one entry naming the app database, and that `"test" not in result`.

In `tests/unit/test_cli_entrypoint.py`, beside the existing `bootstrap-db` tests:

```python
def test_bootstrap_db_passes_no_test_dsn_unless_asked(monkeypatch):
    """Default off, so the runbook's own invocation creates nothing extra."""

def test_bootstrap_db_with_test_db_passes_one(monkeypatch):
    """`make migrate` is the caller that wants both."""
```

Both patch `apps.cli.app.bootstrap_local_platform` and assert on the `test_dsn` kwarg: `None` for `main(["bootstrap-db"])`, the default test DSN for `main(["bootstrap-db", "--with-test-db"])`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_db_bootstrap.py tests/unit/test_cli_entrypoint.py -k "test_db or test_dsn" -v`
Expected: FAIL — `bootstrap_postgres` has no optional `test_dsn`, the CLI has no flag.

- [ ] **Step 3: Make `test_dsn` optional in both bootstrap functions**

`bootstrap_postgres`: `test_dsn: str | None = None`; skip `ensure_postgres_database(admin_dsn, test_dsn)` and `run_postgres_migrations(test_dsn)` when `None`, and build the result dict without the `"test"` key. Extend the existing docstring paragraph about the test database's own DSN to say why it is now opt-in.

`bootstrap_local_platform`: same parameter change; guard the `backend_name_for_dsn(test_dsn)` validation at line 50 on `test_dsn is not None`.

- [ ] **Step 4: Add the CLI flag**

In `apps/cli/app.py`, change `test_dsn` to `Optional[str] = typer.Option(None, "--test-dsn", envvar="TEST_DATABASE_URL", ...)` and add:

```python
    with_test_db: bool = typer.Option(
        False, "--with-test-db/--no-test-db",
        help="Also create and migrate the test database. `make migrate` passes this.",
    ),
```

Resolve as `test_dsn = (test_dsn or DEFAULT_TEST_DSN) if with_test_db else None`, keeping the existing empty-string-is-unset reasoning. Extend the command's docstring: default off because the ops runbook's own invocation is a production host.

- [ ] **Step 5: Pass the flag from the three local callers**

`Makefile:99`, `scripts/acceptance_run.sh:84` and `scripts/mexicali_run.sh:214` all become `bootstrap-db --with-test-db`.

The two scripts are not a hedge: `docs/delivery/05_capability_proof.md:142` quotes `acceptance_run.sh` step 1's output as capability evidence, including the `"test"` key, and that file is hash-locked. With the flag the transcript is byte-identical.

- [ ] **Step 6: Fix `_stub_collaborators`**

`tests/unit/test_db_bootstrap.py:53` reads `kwargs["test_dsn"]` unconditionally. Use `kwargs.get("test_dsn")`, and have the stub omit the `"test"` key when it is `None` so it models the real return shape.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/test_db_bootstrap.py tests/unit/test_cli_entrypoint.py tests/contract/test_runbook_scripts_match_the_cli.py -q`
Expected: PASS.

- [ ] **Step 8: Prove the hashed transcript still holds**

Run: `uv run pytest tests/contract/test_repository_structure.py -q` and `git status --porcelain docs/delivery/`
Expected: PASS, and empty.

- [ ] **Step 9: Update `docs/architecture/OPS_PLANE_RUNBOOK.md:42`**

Confirm the printed `bootstrap-db --app-dsn "$REIMAGINA_APP_DSN"` needs no flag — under default-off it is now correct as written. Add one sentence saying the test database is opt-in and this command does not create it. `tests/contract/test_runbook_scripts_match_the_cli.py` reads only the two shell scripts (`SCRIPTS`, line 31), never this markdown, so nothing will catch drift here.

- [ ] **Step 10: Commit**

```bash
git add packages/py/reimagina/storage/postgres/bootstrap.py packages/py/reimagina/db/bootstrap.py apps/cli/app.py Makefile scripts/acceptance_run.sh scripts/mexicali_run.sh docs/architecture/OPS_PLANE_RUNBOOK.md tests/unit/test_db_bootstrap.py tests/unit/test_cli_entrypoint.py
git commit -m "fix(bootstrap): make the test database opt-in, so the ops remedy creates nothing extra"
```

---

### Task 4: Refuse the ops-plane migration on a pooled DSN (#3)

`packages/py/reimagina/db/bootstrap.py:132` falls back to `app_dsn` when no direct DSN was given. `SystemDatabase.run_migrations()` needs a *session* advisory lock held across statements on an autocommit connection; a transaction-mode pooler hands the session away between them, so the migration half-lands and reports `migrated: true`.

**Step 1 can reject this task's mechanism.** Do not skip it.

**Files:**
- Modify: `packages/py/reimagina/orchestration/dbos_app.py:275-357` (`ensure_system_database`)
- Modify: `docs/architecture/JOB_LAYER.md:444`, `docs/architecture/OPS_PLANE_RUNBOOK.md:49`
- Test: `tests/integration/test_dbos_system_schema_bootstrap.py`

**Interfaces:**
- Consumes: `dbos_app._assert_domain_database(dsn) -> None` (the guard this sits beside, `dbos_app.py:194`).
- Produces: `dbos_app._assert_session_state_survives(dsn) -> None`, raising `RuntimeError` naming `--direct-dsn` / `$DATABASE_URL_DIRECT` when it does not.

- [ ] **Step 1: Verify the probe discriminates, before writing it**

```bash
cd infra/docker && docker compose up -d postgres pgbouncer
```

Through the pooled port (5432) and the direct port (5433), on one autocommit connection each, run as two separate statements:

```sql
SET reimagina.pooling_probe = '1';
SELECT current_setting('reimagina.pooling_probe', true);
```

Expected: `'1'` direct, `NULL` pooled — the compose file sets `PGBOUNCER_SERVER_RESET_QUERY: "DISCARD ALL"`, which runs when the backend returns to the pool.

Record both answers.
- **They differ:** continue to Step 2.
- **They do not:** stop and report. The fallback is to refuse when `direct_dsn` is absent and the environment's `DATABASE_URL_DIRECT` names a different host or port than `app_dsn` — a different fix in a different place, which needs its own approval. Do not substitute it silently.

Note for the record: the spec's first draft probed `LISTEN` instead, on the premise that a pooler refuses it. `.env.example:56-57` and the compose file's pgbouncer comment both say transaction pooling *hands the session away* rather than refusing, so that probe would have passed against the configuration it was meant to catch.

- [ ] **Step 2: Write the failing test**

In `tests/integration/test_dbos_system_schema_bootstrap.py`:

```python
def test_ensure_system_database_refuses_a_pooled_dsn(pooled_dsn: str):
    """The migration's session advisory lock does not survive transaction pooling.

    It does not fail either: the lock covers none of the DDL that follows it and
    is never released by the backend holding it, so a half-migration reports
    `migrated: true`. Refusing is the only honest answer.
    """
    with pytest.raises(RuntimeError, match="direct"):
        ensure_system_database(pooled_dsn)
```

Add a `pooled_dsn` fixture that skips when no pooler is reachable (`pytest.skip`), so the suite still runs on a plain Postgres.

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/integration/test_dbos_system_schema_bootstrap.py -k pooled -v`
Expected: FAIL — `ensure_system_database` runs the migration through the pooler instead of raising.

- [ ] **Step 4: Add `_assert_session_state_survives` to `dbos_app.py`**

```python
def _assert_session_state_survives(dsn: str) -> None
```

One autocommit psycopg2 connection, `closing()`-wrapped like `_assert_domain_database` at line 207. Two separate `execute` calls: `SET reimagina.pooling_probe = '1'`, then `SELECT current_setting('reimagina.pooling_probe', true)`. Raise `RuntimeError` on a falsy result, naming `--direct-dsn` and `$DATABASE_URL_DIRECT` and saying the DSN appears to reach a transaction-mode pooler. Mask the DSN the way `_assert_domain_database` does (`dsn.rsplit('@', 1)[-1]`) — never print credentials.

A namespaced custom GUC so the probe clobbers nothing. The docstring must say why session persistence and not `LISTEN`, per Step 1's note.

- [ ] **Step 5: Call it from `ensure_system_database`**

Immediately after `_assert_domain_database(dsn)` at line 334, before `SystemDatabase.create(...)`. Both guards run before DBOS touches the database at all, which is what `test_ensure_system_database_refuses_a_database_that_is_not_ours` already asserts for the first one.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/integration/test_dbos_system_schema_bootstrap.py -v`
Expected: PASS, including the three existing tests against a direct DSN.

- [ ] **Step 7: Update the two docs**

`docs/architecture/JOB_LAYER.md:444` and `docs/architecture/OPS_PLANE_RUNBOOK.md:49` both say the ops-plane migration "falls back to `--app-dsn` when neither is set". It still does — and now refuses if that DSN turns out to be pooled. Say both.

- [ ] **Step 8: Commit**

```bash
git add packages/py/reimagina/orchestration/dbos_app.py docs/architecture/JOB_LAYER.md docs/architecture/OPS_PLANE_RUNBOOK.md tests/integration/test_dbos_system_schema_bootstrap.py
git commit -m "fix(ops-plane): refuse to migrate the system schema through a transaction pooler"
```

---

### Task 5: Scripts (#9, #5, #11, #13)

Four independent small fixes. One commit: no reviewer would accept one and reject another, and none shares a file with another task.

**Files:**
- Modify: `packages/py/reimagina/orchestration/dbos_orchestrator.py:287,386`
- Modify: `scripts/dependency_inventory.py:55-62,65-73,118-134`
- Modify: `scripts/preflight_ports.py:112-122`
- Test: `tests/unit/test_dependency_inventory.py` (create), `tests/unit/test_preflight_ports.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `dbos_orchestrator._is_undefined_table(exc: BaseException) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dependency_inventory.py`:

```python
def test_a_nested_package_resolves_its_licence_file(tmp_path, monkeypatch):
    """A lockfile key for a nested dependency is `node_modules/a/node_modules/b`.

    The old lookup joined `node_modules` to the bare name, so it checked
    `node_modules/b` — often absent — and reported UNRESOLVED with the licence
    file sitting at the key's real path. Unreachable on today's lockfile (one
    package omits `license` and it is top-level), so this test is the proof.
    """

def test_two_interpreter_directories_are_an_error(tmp_path, monkeypatch):
    """`sorted()[0]` picked `python3.12` over `python3.13` after a bump.

    An ambiguous venv is an operator problem with a one-line fix; generating the
    inventory from the stale tree silently is not.
    """
```

The first writes a `package-lock.json` with a nested key whose entry omits `license`, and a `LICENCE` file (British spelling) at the key's path, then asserts `npm_packages()` returns a row whose licence names the file. The second builds two `.venv/lib/python*/site-packages` directories and asserts `_site_packages()` raises `SystemExit` whose message names both.

In `tests/unit/test_preflight_ports.py` — its import line currently names only `PortInUse, check_ports, describe_holder`, so add `main`:

```python
def test_a_non_numeric_argument_is_a_message_not_a_traceback():
    """This script exists to replace an opaque failure with a clear one.

    `ValueError: invalid literal for int()` out of its own argument parsing is
    the exact failure mode it was written to remove.
    """
    assert main(["notaport:API"]) == 2
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_dependency_inventory.py tests/unit/test_preflight_ports.py -v`
Expected: the two inventory tests FAIL; the preflight one FAILS with `ValueError`.

- [ ] **Step 3: Extract `_is_undefined_table` in `dbos_orchestrator.py`**

Module-level:

```python
def _is_undefined_table(exc: BaseException) -> bool
```

Returns whether `exc.orig.sqlstate` is `"42P01"`. Its docstring carries, once, the reason both sites match by SQLSTATE rather than exception class: the error arrives wrapped by SQLAlchemy around a psycopg3 error, and psycopg3 reaches this package only transitively through DBOS.

Both sites (lines 287 and 386) become `if not _is_undefined_table(exc): raise`. Trim their comments to what is specific to each side — the write path's "a client never creates this schema, so submits die where reads answer no such run", the read path's "the port promises `RunStatus | None`".

- [ ] **Step 4: Fix the two `dependency_inventory.py` defects**

`_site_packages()` (line 55): when `len(candidates) > 1`, `sys.exit` naming every candidate and the remedy (`rm -rf .venv && uv sync`). Keep the existing no-venv exit.

`_licence_from_files` (line 66): patterns become `("licenses/LICEN[CS]E*", "LICEN[CS]E*", "COPYING*")`.

`npm_packages()` (lines 118-134): replace the inline `modules / name / "LICENSE"` block with `_licence_from_files(ROOT / "apps" / "web" / key)` — the lockfile key is the path relative to `apps/web`. Delete the now-unused `modules` local if nothing else uses it.

- [ ] **Step 5: Fix `preflight_ports.main`**

Wrap the `int(port)` at line 116. On `ValueError`, print to stderr a line naming the offending argument and the expected form (`PORT:LABEL`, as the module docstring's `python scripts/preflight_ports.py 8001:API 5173:Web` shows) and return 2.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_dependency_inventory.py tests/unit/test_preflight_ports.py tests/unit -q`
Expected: PASS.

- [ ] **Step 7: Prove the inventory output did not move**

Run: `uv run python scripts/dependency_inventory.py && git status --porcelain docs/DEPENDENCIES.md`
Expected: empty. The fix is unreachable on today's lockfile, so a changed output means the rewrite broke something that worked. The nested-key test is what proves the fix.

- [ ] **Step 8: Commit**

```bash
git add packages/py/reimagina/orchestration/dbos_orchestrator.py scripts/dependency_inventory.py scripts/preflight_ports.py tests/unit/test_dependency_inventory.py tests/unit/test_preflight_ports.py
git commit -m "fix(scripts): resolve nested npm licences, refuse an ambiguous venv, dedup the 42P01 probe"
```

---

### Task 6: Tests and config (#6, #7, #10, #12)

**Files:**
- Modify: `tests/unit/test_preflight_ports.py:24-29`
- Modify: `tests/integration/test_dbos_system_schema_bootstrap.py:28-34`
- Modify: `apps/web/vite.config.ts:41,45`
- Modify: `tests/contract/test_repository_structure.py:655-661`
- Test: `apps/web/src/__tests__/vite-config.test.ts` (create)

**Interfaces:**
- Consumes: `scripts.preflight_ports.check_ports`, `PortInUse`.
- Produces: nothing other tasks use.

- [ ] **Step 1: Write the failing test for the Vite config**

Create `apps/web/src/__tests__/vite-config.test.ts`:

```ts
it("treats an empty VITE_PORT as unset", async () => {
  process.env.VITE_PORT = "";
  const config = await loadConfig();
  expect(config.server.port).toBe(5173);
});

it("treats an empty REIMAGINA_API_URL as unset", async () => {
  process.env.REIMAGINA_API_URL = "";
  const config = await loadConfig();
  expect(config.server.proxy["/api"].target).toBe("http://127.0.0.1:8001");
});
```

`loadConfig` imports `../../vite.config.ts` fresh (`vi.resetModules()`) so the env read at module scope re-runs. Restore both variables in `afterEach`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/web && npx vitest run src/__tests__/vite-config.test.ts`
Expected: FAIL — port `0`, target `""`.

- [ ] **Step 3: Fix `apps/web/vite.config.ts`**

Line 41 `??` → `||`; line 45 `??` → `||`. `??` catches only `undefined`, and `make dev` exports both variables, so an unset one in the shell arrives as `""`.

- [ ] **Step 4: Run it**

Run: `cd apps/web && npx vitest run && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: De-flake `test_free_ports_pass`**

`tests/unit/test_preflight_ports.py:24` binds port 0, closes, then asserts the port is free — anything on the machine can take it in that window, and `_is_free` sets no `SO_REUSEADDR` so it forgives nothing. Rewrite to try up to five ephemeral ports and pass on the first `check_ports` accepts, re-raising the last `PortInUse` if none do. Keep the test's name and its subject. Add a line to the docstring saying why the retry is there, so nobody "simplifies" it back.

- [ ] **Step 6: Close the socket leak in `_regclass`**

`tests/integration/test_dbos_system_schema_bootstrap.py:28` — `psycopg2.connect(dsn) as conn` scopes the transaction and leaves the socket open, which the same file's `_run` docstring spells out. Wrap it: `closing(psycopg2.connect(dsn)) as conn`. Do **not** also keep `conn` as a second context manager — `_regclass` only reads, so there is nothing to commit.

- [ ] **Step 7: Scope the CI job split to the `jobs:` section**

`tests/contract/test_repository_structure.py:661` splits the whole workflow on two-space-indented keys, so top-level `on:`'s children (`push:`, `pull_request:`) become "job blocks". Slice first:

```python
    jobs_section = ci.split("\njobs:\n", 1)[1]
    jobs_section = re.split(r"\n(?=[a-z])", jobs_section)[0]
    blocks = re.split(r"\n  (?=[a-z][a-z0-9-]*:)", jobs_section)
```

Update the comment above it: the split is still deliberate text handling, and the slice is what makes "two-space-indented key" mean "job". No YAML parser — PyYAML is not a dependency of this project.

- [ ] **Step 8: Run the affected suites**

Run: `uv run pytest tests/unit/test_preflight_ports.py tests/contract/test_repository_structure.py -v` and the integration file from Task 4.
Expected: PASS. The CI-job test must still find the integration job's `DATABASE_URL_DIRECT:` — it is job-scoped at `ci.yml:176`, inside the sliced section.

- [ ] **Step 9: Commit**

```bash
git add tests/unit/test_preflight_ports.py tests/integration/test_dbos_system_schema_bootstrap.py apps/web/vite.config.ts apps/web/src/__tests__/vite-config.test.ts tests/contract/test_repository_structure.py
git commit -m "fix(tests): de-flake the port preflight, close a socket leak, scope the CI job split"
```

---

### Task 7: Whole-branch gate

**Files:** none.

- [ ] **Step 1: Run the full gate, mirroring CI's steps**

```bash
uv run pytest tests/unit tests/contract -q
uv run pytest tests/integration -q
uv run ruff check && uv run ruff format --check
cd apps/web && npx tsc --noEmit && npx vitest run
```

Compare every count against Task 0's baseline. More passing is expected (new tests); anything newly failing is this branch's.

- [ ] **Step 2: Prove nothing hash-locked or pinned moved**

```bash
git diff --stat a89fe83 -- uv.lock apps/web/package-lock.json docs/delivery/   # expect: empty
```

- [ ] **Step 3: Check the acceptance transcript, or say you did not**

The spec asks that `scripts/acceptance_run.sh` step 1 still print the line quoted at `docs/delivery/05_capability_proof.md:142`. The full script is a long local run against a populated database. If it is run, confirm the line matches byte for byte. If it is not, say so plainly in the report: the practical proof is Task 3's CLI tests, which pin `--with-test-db` to today's exact `test_dsn`, and Task 3 Step 8, which shows no hashed file moved.

- [ ] **Step 4: Confirm authorship on every commit**

```bash
git log a89fe83..HEAD --format='%an <%ae> | %cn <%ce> | %s'
git log a89fe83..HEAD --format=%B | grep -i -e co-authored -e claude -e "generated with"   # expect: no matches
```

Every line must read `Rossano <rossano.schifanella@gmail.com>` twice.

- [ ] **Step 5: Report**

State for each of the 12 findings: fixed, and the test that proves it. Then call `ReportFindings` with all 13, each carrying its `outcome` — `fixed` for the 12, `no_change_needed` for #8.
