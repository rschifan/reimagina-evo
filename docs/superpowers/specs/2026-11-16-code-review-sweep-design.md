# Code-review sweep — 12 findings on `origin/main`

Status: Draft for approval
Date: 2026-11-16
Base: `origin/main` @ `a89fe83`

## Intent

An xhigh code review of the current diff reported 13 findings. One (#8) is
already fixed on `origin/main`. The remaining 12 are latent: every one was
confirmed present at `a89fe83` by reading the code, and the gate was green on
the branch the review ran against. Nothing here is a red test; everything here
is a defect that waits for a particular input, host or deployment shape.

**The gate has not been measured at `a89fe83`** — that commit is 17 ahead of
the local `main` the review used, and this machine's `.venv` is currently
broken (`import numpy` fails). Measuring it is the branch's first step, before
any fix. No test count is quoted here on purpose: the delivery documents
already disagree with each other on that figure (see Non-goals), and adding a
fourth unverified number would be the same mistake.

The why: this repository *is* Schedule A Work 2 of the Tec agreement, due
20 November 2026, accepted on "the repository is delivered and accessible, and
the code builds and runs following the included README". Four of the twelve
are reachable by the people who will run that acceptance: a reviewer clicking
Rerun on an unprovisioned database (#1), an operator following the ops runbook
on a production host (#2, and #3 on the same line of that runbook), and a
client reading a raster the deployment cannot open (#4). The other eight are
the kind of thing a technically competent reader of the code would reasonably
flag. Fixing all twelve before the tag means the delivered commit has no known
defects.

(The first pass of this review called it three, leaving #3 among the
cleanups. #3 is reached by the same runbook command as #2, so it belongs in
the reachable set.)

## Goals

- All 12 findings fixed on one branch off `origin/main`.
- Each non-trivial fix leaves one runnable check that fails if the logic breaks.
- The full local gate (the CI steps, Node 24) green before the branch is offered.
- Fixed at the depth where every caller benefits, not patched per call site.

## Non-goals

- No refactoring beyond what a fix needs.
- No new dependencies. No PyYAML for the CI-workflow test, no licence-scanner
  library for the inventory script.
- Not the delivery mechanics (tag, PDFs, `MANIFEST.md`, transmittal to Tec).
  That is separate work, after this lands.
- Not finding #8 — already fixed upstream, reported as `no_change_needed`.
- Not `scripts/third_party_licences.py` or `docs/THIRD_PARTY_LICENSES.md` —
  the Article 4.1 generator and artefact. Checked: it walks
  `node_modules/**/package.json` directly, so it has no nested-path bug, and
  its licence-file glob at line 220 already covers `LICENCE*`. Nothing to fix,
  and any change to its output would stale a hashed deliverable.
- Not the test-count figures that disagree across the hashed delivery
  documents (`01_architecture_map.md:414` says 546 unit+contract / 322
  integration; `04_technical_documentation.md:674-678` says 954 and 1,473).
  That drift predates this sweep and adding tests changes no byte of either
  file, so the hash check stays green. Raised separately as delivery work.

## Anti-goals

What would make this a failure even if every finding were closed:

- A fix that changes an API response shape the web app or a contract test
  depends on without that dependency being updated in the same commit.
- A bootstrap change that breaks `make migrate` for a developer on a fresh
  clone. That path is the README's first instruction and the acceptance
  criterion names the README.
- Landing four days before the tag with the gate red or unmeasured.

## Constraints

- Base is `origin/main` @ `a89fe83`. The current checkout (`delivery/schedule-a`)
  is 72 behind and its 5 commits are already upstream; it cannot be the base.
- Authorship per `AGENTS.md`: every commit authored and committed by
  `Rossano <rossano.schifanella@gmail.com>`, no `Co-Authored-By`, no agent
  attribution anywhere in the repo.
- Deadline 20 November 2026. Four days.
- `uv.lock` / `package-lock.json` untouched — see "no new dependencies".

## Approaches

**A — The reachable four before the tag, eight after.** Land #1, #2, #3 and
#4; defer the eight cleanups. Smallest risk surface before an immutable hashed
delivery. Trades away the property that makes the sweep worth doing: the
delivered commit would ship with eight known findings, and Article 2.3 gives
Tec ten days to write up non-conformities.

**B — One sweep, one commit.** All 12 in a single commit. Shortest path, but
one gate run covers twelve unrelated changes; a regression in the bootstrap
semantics and a typo in a licence-glob are indistinguishable in a bisect.

**C — One sweep, grouped commits (chosen).** One branch, commits grouped by
subject so each is separately revertable and separately verifiable:

1. API error surfaces — findings #1, #4 (one `OpsPlaneNotProvisioned` handler,
   `str(exc)` leak closed)
2. Bootstrap safety — #2, #3 (test DB opt-in, pooled-DSN refusal)
3. Orchestrator dedup — #9
4. Scripts — #5, #11, #13
5. Tests and config — #6, #7, #10, #12

**The user chose the scope — "fix all 12 in one sweep" — which rules out A.**
The grouping into five commits is my call, not theirs: the fixes are already
independent, so grouping costs nothing and gives five revert points without
five branches. Say so if you would rather have one commit.

## The fixes

Numbers are the review's. Each entry states the design call, not just the
location.

### Group 1 — API error surfaces

**#1 — `OpsPlaneNotProvisioned` escapes two of three submit paths.**
`apps/api/routers/jobs.py:216` catches it and answers 503. Two other call
sites do not: `jobs.py:585` (rerun) and `scenarios.py:800` (scenario
evaluation). On a database Alembic migrated but no DBOS worker ever launched
against, both answer 500 — the exact "a provisioning step was skipped read as
a code defect" that the guarded path exists to prevent.

**Decision: one app-level handler, not three try/excepts.**
`app.add_exception_handler(OpsPlaneNotProvisioned, errors.ops_plane_handler)`
in `apps/api/app.py`, returning 503; drop the per-route `except` in
`submit_run`. Every present and future submit path is covered, and the diff is
smaller than three copies.

Import it at module scope in `app.py`, not lazily. Every router imports
`reimagina.orchestration` inside its function bodies because `dbos_app` pulls
the engine's client library in at import time — but `orchestration/port.py`
imports only `collections.abc`, `dataclasses` and `typing`, which is why
`OpsPlaneNotProvisioned` was put there. Copying the lazy pattern here would
suggest a constraint that does not exist.

**On the scenario recompute path, the envelope carries no materialisation
counts, and that is deliberate.** `scenarios.py`'s `_enqueue_failure` wraps
the enqueue and reports `materialized: {overrides_written, overrides_removed,
results_superseded}` so a client knows what stands — but only for *constraint*
failures. Its own docstring excludes connection-class failures from that
treatment and re-raises them bare, because the counts are of no use to a
client that has to wait for the database before it can read anything back
anyway. An unprovisioned ops plane is that second kind: nothing the client can
do with the counts until an operator runs `bootstrap-db`. So
`OpsPlaneNotProvisioned` passes through `_enqueue_failure` untouched to the
app-level handler, exactly as `OperationalError` does. Add a row for it to the
module docstring's status table (`scenarios.py:34-66`).

**Consequence the builder must carry:** the handler returns the project's
error envelope (`{"error": {code, message, request_id}}`), not
`{"detail": ...}`. `tests/contract/test_jobs_api.py:728` reads
`resp.json()["detail"]`; it must be updated to read the envelope and keep
asserting that the message names `bootstrap-db`. The web app is unaffected —
`describeApiError` (`apps/web/src/ops/store.svelte.ts:428`) already handles
both shapes. The message stays `str(exc)`: it is the project's own remedy
text, not a driver message, so the "never echo the internal message" rule in
`errors.py` is not in tension here. Say so in the handler's docstring.

**#4 — three raster exception classes still answer 500 with `str(exc)`.**
`apps/api/app.py:200` filters `Exception` out of titiler's
`DEFAULT_STATUS_CODES` but keeps `RasterioIOError`, `RasterioError` and
`RioTilerError` mapped to 500. titiler's `exception_handler_factory` answers
`{"detail": str(exc)}`, and a `RasterioIOError` for a remote COG carries the
full URL — including a SAS token when one is in the query string. It also
drops the request id.

**Decision: filter by status, not by class.**
`{exc: code for exc, code in DEFAULT_STATUS_CODES.items() if code < 500}`.
The general rule is "titiler's map is wanted only where it says something
other than 'internal error'"; the existing `is not Exception` special case was
the symptom. Everything 5xx falls through to `errors.unhandled_error_handler`,
which already logs with the traceback and answers the envelope. Replaces the
special case rather than adding a second one.

**Two things this fix is not.** It does not change what happens when a browser
pans past a raster's footprint: `TileOutsideBounds` is already 404 in
`DEFAULT_STATUS_CODES` and `PointOutsideBounds` is already pinned to 404 at
`apps/api/app.py:203`, and the `code < 500` filter keeps both. The exposure
being closed is narrower — `RasterioIOError` is "this file could not be
opened", and `str(exc)` on it carries the full path or remote URL, SAS token
included.

And it moves these three onto `errors.unhandled_error_handler`, which calls
`log.exception` where titiler's factory logged nothing — so a deployment whose
COG cannot be opened now writes one traceback per tile request. **Accepted, no
fourth handler.** A COG the process cannot open is an operator fault that
should be loud, the volume is bounded by how long the broken state lasts, and
a handler existing only to quieten a correct signal is the kind of special
case the filter change just removed.

`tests/unit/test_api_error_envelope.py:38` asserts `TileOutsideBounds` stays
registered; it survives the filter (404) and needs no change.

### Group 2 — Bootstrap safety

**#2 — `bootstrap-db` creates and migrates `reimagina_test` wherever it runs.**
`packages/py/reimagina/storage/postgres/bootstrap.py:81,83` create and migrate
the test database unconditionally, and `test_dsn` defaults to
`DEFAULT_TEST_DSN` (localhost `reimagina_test`). `docs/architecture/OPS_PLANE_RUNBOOK.md:42`
prints `bootstrap-db --app-dsn "$REIMAGINA_APP_DSN"` as *the* production
remedy. Following that runbook on the production host creates and migrates a
`reimagina_test` database on the production server.

**Decision: the test database becomes opt-in, default off.**
`bootstrap_postgres(test_dsn: str | None = None)` and
`bootstrap_local_platform` likewise; skip both calls when `None`, and omit the
`"test"` key from the returned dict. The CLI gains
`--with-test-db/--no-test-db`, **default off**, and `make migrate` (the one
caller that wants it) passes `--with-test-db`.

Off by default because the fail-safe direction matters more than the
convenience: an operator running the command as the runbook prints it should
not create a database.

**`scripts/acceptance_run.sh:84` and `scripts/mexicali_run.sh:214` get
`--with-test-db` too, and that is not a hedge — it keeps a hashed deliverable
true.** `docs/delivery/05_capability_proof.md:142` quotes, as capability
evidence, the transcript of the acceptance run's step 1:
`{"app": {…}, "test": {"created": false, "migrated": true}}`. That file is
hashed in `docs/delivery/SHA256SUMS` and
`tests/contract/test_repository_structure.py:449` recomputes it. Leaving the
script bare would make a hash-locked contract document quote output the code
can no longer produce — four days before delivery, inside the ten-day Article
2.3 window. With the flag, the behaviour and the transcript are byte-identical
to today and nothing needs regenerating. `04_technical_documentation.md:137`
quotes the same shape under `make migrate`, which keeps the flag, so it
survives either way.

Both scripts are local acceptance/demo runs, not the production remedy path,
so a test database there costs nothing — and the production path, the runbook
line, now creates none. That is the finding.

**Builder notes:**
- `tests/unit/test_db_bootstrap.py` asserts on the returned dict and the
  create/migrate call sequence; update those, and add one test that a bare
  `bootstrap-db` touches no second database.
- its `_stub_collaborators` reads `kwargs["test_dsn"]` unconditionally — needs
  `.get()` once the parameter is optional.
- `tests/contract/test_runbook_scripts_match_the_cli.py` reads only the two
  shell scripts (its `SCRIPTS` tuple, line 31), **not**
  `OPS_PLANE_RUNBOOK.md`. It will catch script drift and nothing will catch
  markdown drift, so the runbook edit is manual — see "Documentation" below.
- CI calls `bootstrap-db` nowhere: its Postgres service creates
  `reimagina_test` itself (`ci.yml:168`) and `conftest`'s `migrated_db` runs
  Alembic. `README.md:57` ("creates both DBs") stays true via `make migrate`.

**#3 — the ops-plane migration can cross a transaction-mode pooler silently.**
`packages/py/reimagina/db/bootstrap.py:132` calls
`ensure_system_database(direct_dsn or app_dsn)`. `SystemDatabase.run_migrations()`
takes a *session* advisory lock on an AUTOCOMMIT connection and installs
LISTEN/NOTIFY triggers; neither survives pgbouncer in transaction mode. The
fallback is correct for the common local install (one DSN, already direct) and
wrong — silently, with `migrated: true` — for a pooled deployment whose
operator did not export `DATABASE_URL_DIRECT`.

**Decision: probe the capability, do not guess from the DSN.** No DSN string
tells you whether a pooler is in front, so the guard has to ask the connection
a question whose answer differs. Inside `ensure_system_database`, before
anything is created:

1. `SET reimagina.pooling_probe = '1'` on an autocommit connection — its own
   transaction, which is how `run_migrations()` runs too.
2. In a **separate** statement on that same connection, read
   `current_setting('reimagina.pooling_probe', true)`.
3. Direct Postgres returns `'1'`. A transaction-mode pooler returns `NULL`,
   because the backend went back to the pool between the two and
   `SERVER_RESET_QUERY` (`DISCARD ALL` in this project's compose file) cleared
   it. On `NULL`, refuse with an error naming `--direct-dsn` /
   `$DATABASE_URL_DIRECT`.

A namespaced custom GUC, so the probe clobbers nothing.

**Why session persistence and not `LISTEN` — this is a correction to the first
draft of this design.** That draft asserted "a transaction-mode pooler refuses
`LISTEN`" and built the probe on it. This repository's own documentation says
otherwise, in two places: `.env.example:56-57` — "LISTEN/NOTIFY is a
session-scoped protocol and transaction pooling **hands the session away**" —
and the compose file's pgbouncer comment, which says transaction mode is
incompatible with code that *uses* LISTEN/NOTIFY rather than that it rejects
it. pgbouncer forwards `LISTEN` and loses the subscription. A probe waiting for
a refusal would therefore have passed against the exact configuration it
exists to catch, and shipped as proof of a safety it did not provide.

Session persistence is also the requirement itself rather than a proxy for it.
`run_migrations()` needs a *session* advisory lock held across statements on an
autocommit connection; "does session state survive between transactions here"
is that question, asked directly.

Placed in `ensure_system_database` beside the existing `_assert_domain_database`
guard — same shape, same reason, and it covers every caller rather than only
`bootstrap_local_platform`. The fallback at `db/bootstrap.py:132` then stays as
it is, because it is now safe: on a pooled DSN it refuses instead of
half-migrating.

**⚠ Still verify before relying on it.** `infra/docker/docker-compose.yml`
runs `bitnami/pgbouncer:1.23.1` with `PGBOUNCER_POOL_MODE: transaction` and
`PGBOUNCER_SERVER_RESET_QUERY: "DISCARD ALL"`. First plan step for this fix:
bring that up and run the two statements through the pooled port (5432) and the
direct port (5433), and confirm the answers differ. The reset query should run
on release even when the same backend is handed back, but that is the one link
in the chain I have not watched happen.

- **Probe discriminates:** build as described; the check is an integration test
  against the compose stack, both ports. A monkeypatched `psycopg2.connect`
  would only re-assert the code just written.
- **Probe does not discriminate:** fall back to requiring an explicit direct
  DSN — refuse when `direct_dsn` is absent and the environment defines
  `DATABASE_URL_DIRECT` naming a different host or port than `app_dsn`. Narrower
  (it cannot catch a pooled deployment that defines no direct DSN at all) but
  it has no false positives on a plain local install, where `make migrate`
  passes no direct DSN. Report back before building it rather than substituting
  it silently.

**Reachability, stated accurately:** `.env.example:52-62` points
`DATABASE_URL` — what `--app-dsn` reads via `envvar` — at the **direct** port
5433, and only `REIMAGINA_APP_DSN` at the pooler. `bootstrap-db` never reads
`REIMAGINA_APP_DSN`. So the hazard is reachable on exactly one invocation: the
runbook's explicit `--app-dsn "$REIMAGINA_APP_DSN"`, with
`DATABASE_URL_DIRECT` unexported. Narrower than a blanket "pooled deployments
are broken", and still the one path an operator is told to type.

**Builder note:** the probe must not itself fail on a plain Postgres where the
role lacks some unrelated privilege — `LISTEN` needs none. Keep the error
message distinct from `_assert_domain_database`'s.

### Group 3 — Orchestrator

**#9 — the 42P01 probe is written twice.**
`dbos_orchestrator.py:287` and `:386` both carry
`if getattr(getattr(exc, "orig", None), "sqlstate", None) != "42P01": raise`,
each under its own paragraph of explanation for the same mechanism.

**Decision:** one module-level `def _is_undefined_table(exc) -> bool`, holding
the "SQLSTATE not class, because SQLAlchemy wraps psycopg3 and psycopg3 is
undeclared here" explanation once. Both sites become
`if not _is_undefined_table(exc): raise`. The surrounding comments keep only
what is specific to their own side (read vs write).

### Group 4 — Scripts

**#5 — the npm licence lookup misses every nested package, and re-implements a
helper that is in the same file.** `scripts/dependency_inventory.py:124` builds
`modules / name / "LICENSE"`, but a lockfile key for a nested dependency is
`node_modules/foo/node_modules/bar` and `name` is `bar` — so the path checked
is `node_modules/bar`, which may not exist, and the row reports `UNRESOLVED`
while the licence file sits at the key's real path. The block then repeats what
`_licence_from_files` (line 65) already does.

**Decision:** call `_licence_from_files(ROOT / "apps" / "web" / key)` — the
lockfile key *is* the path relative to `apps/web` — and delete the inline
block. Widen that helper's patterns to `LICEN[CS]E*` so British spellings
resolve on both sides.

**Not an Article 4.1 gap, and the first draft of this design said it was.**
Measured on `origin/main`: the contract artefact is
`docs/THIRD_PARTY_LICENSES.md` from `scripts/third_party_licences.py`, a
different script which walks `node_modules/**/package.json` directly and
already globs `LICENCE*`. `dependency_inventory.py` writes
`docs/DEPENDENCIES.md`, which is not hashed and not named by any contract
test. And the bug is unreachable today: exactly 1 of 194 lockfile packages
omits `license` (`node_modules/svelte-toolbelt`), it is a *top-level* key
where the current path is correct, and it already resolves. All 11 nested keys
declare licences.

So this is dedup plus robustness against the next nested package that omits a
licence — worth doing, not load-bearing.

**Its check, since the UNRESOLVED count is already zero and cannot drop:** one
unit test that feeds a synthetic lockfile entry under
`node_modules/a/node_modules/b` with no `license` field and a `LICENCE` file
on disk, and asserts the row resolves.

**#11 — `int(port)` on a malformed argument prints a traceback.**
`scripts/preflight_ports.py:116`. The whole point of this script is to replace
an opaque errno with a clear message; `ValueError: invalid literal for int()`
is the failure mode it exists to remove, in its own argument handling.

**Decision:** wrap the parse, print a usage line naming the bad argument to
stderr, return 2.

**#13 — `_site_packages()` silently picks one of several interpreter dirs.**
`scripts/dependency_inventory.py:62` returns `candidates[0]` of a sorted glob.
After a Python bump, `.venv/lib` holds both `python3.12` and `python3.13`;
string sort puts `python3.12` first, so the inventory is generated from the
stale tree.

Latent, not live: `.venv/lib` holds one interpreter directory today.

**Decision:** exit with an error naming every candidate when there is more than
one. An ambiguous venv is an operator problem with a one-line fix (`rm -rf
.venv && uv sync`), and the alternative — picking by `sys.version_info` —
hides the staleness rather than reporting it. The script already exits with
advice when there is no venv; this is the same shape.

### Group 5 — Tests and config

**#6 — `test_free_ports_pass` races.** `tests/unit/test_preflight_ports.py:28`
binds port 0, reads the ephemeral port, closes, then asserts it is free.
Anything on the machine can take that port in the window — and `_is_free`
deliberately sets no `SO_REUSEADDR`, so it does not forgive a transient state
either.

**Decision:** retry. Try up to five ephemeral ports and pass on the first one
`check_ports` accepts; fail with the last error if none do. Keeps the test's
subject (a free port passes the preflight) and removes the race, without
weakening `_is_free`, whose strictness is documented and deliberate.

**#7 — `_regclass` leaks a socket per call.**
`tests/integration/test_dbos_system_schema_bootstrap.py:30` uses
`psycopg2.connect(dsn) as conn`, which scopes the *transaction* and leaves the
socket open. The same file's `_run` wraps its connection in `closing()` and its
docstring spells out exactly this distinction, so the file contradicts itself.

**Decision:** `closing(psycopg2.connect(dsn))` as the outer context, matching
`_run`. Four calls per run against a fixture database that is then dropped
`WITH (FORCE)` — the FORCE is partly there to paper over this.

Do **not** also keep `conn` as a second context manager (the `closing(...) as
conn, conn, conn.cursor()` shape). `_regclass` only reads, so there is nothing
to commit, and the connection closes a statement later.

**#10 — `VITE_PORT=""` starts the dev server on a random port.**
`apps/web/vite.config.ts:41`: `Number(process.env.VITE_PORT ?? 5173)`. `??`
only catches `undefined`; an exported-but-empty variable gives `Number("")`
= `0`, and with `strictPort: true` the server binds an arbitrary free port
while the banner promises 5173. The same empty-string-is-unset reasoning is
already written out in the CLI's `bootstrap_db`.

**Decision:** `??` → `||`. One character, and it makes the file agree with the
convention the Python side already states.

Same fix four lines down, at `vite.config.ts:45`,
`target: process.env.REIMAGINA_API_URL ?? "http://127.0.0.1:8001"` — `make dev`
exports that variable, so an empty value silently points the dev server's proxy
at nothing. Same character, same consequence the comment above
it already describes, and the Goals' "fixed at the depth where every caller
benefits" covers it.

**#12 — the CI job-block split matches more than jobs.**
`tests/contract/test_repository_structure.py:661` splits the whole workflow
text on two-space-indented keys. Top-level `on:` has two-space-indented
children too, so `push:` and `pull_request:` become "job blocks", and anything
after `jobs:` that is not a job leaks in. The test can pass because a
non-job block contained the string it looked for, or miss a real job.

**Decision:** slice to the `jobs:` section first — take the text after
`\njobs:\n`, cut it at the next top-level key (`\n(?=[a-z])`), then split as
now. Three lines, no YAML parser, and the file's deliberate "treat the
workflow as text" note still holds. PyYAML is not a dependency of this project
and this is not the reason to add one.

Line 689's `re.split(r"\n      - name: ", ci)` is a different pattern at a
different indent and is not in scope.

## Documentation that goes stale with these fixes

In scope. None of these are hashed, all of them describe behaviour this branch
changes, and this repository's acceptance criterion is that a reader can follow
its docs. Each edit rides in the commit that causes it.

- `docs/architecture/API.md:110` — the 503 row sits under **"Refusals that
  name their own remedy (`detail`)"**, the shape #1 moves it off, and it names
  only `POST /jobs` where every submit path now answers it.
- `docs/architecture/API.md:97` — "`create_app` now filters that one entry
  out"; #4 replaces that with the `code < 500` rule.
- `docs/architecture/JOB_LAYER.md:442` — "`POST /jobs` answers as **503**";
  now every submit path does.
- `docs/architecture/JOB_LAYER.md:444` and
  `docs/architecture/OPS_PLANE_RUNBOOK.md:49` — both repeat that the
  ops-plane migration "falls back to `--app-dsn` when neither is set". Under
  #3 it falls back *and then refuses* if that DSN is pooled.
- `docs/architecture/OPS_PLANE_RUNBOOK.md:42` — the `bootstrap-db` line needs
  no flag change under default-off; confirm rather than assume.
- `apps/api/routers/scenarios.py:34-66` — the module docstring's status table
  gains a row for the 503.

## What is left to the builder

- Commit grouping as above; one gate run at the end, plus whatever narrower
  runs each group needs while iterating. Group 2 now carries a verification
  step *before* its fix — the pgbouncer `LISTEN` probe under #3.
- Exact wording of the four new messages (ops-plane handler, pooled-DSN
  refusal, ambiguous venv, preflight usage). The design fixes what each must
  name, not the prose.
- Whether the pooled-DSN probe reuses an existing connection helper in
  `reimagina.db` or opens its own — whichever is shorter at the call site.
- Where the opt-in test-database flag's help text points (`make migrate`).

## Verification

`uv sync --locked --extra dev` first — this machine's `.venv` is broken
(`import numpy` fails), so nothing below can run until it is rebuilt. Then
measure the gate at `a89fe83` *before* the first fix, so every later run has a
baseline to compare against.

The local gate must mirror CI's steps, with Node 24 (see the project's own
notes on both). At minimum, before the branch is offered:

- `pytest tests/unit tests/contract`
- the integration suites that touch bootstrap and the ops plane, against local
  Postgres
- `tsc --noEmit` and `vitest run` in `apps/web`
- `ruff check` / `ruff format --check`
- `python scripts/dependency_inventory.py` runs and `docs/DEPENDENCIES.md` is
  unchanged — the fix is unreachable on today's lockfile, so a changed output
  would mean the rewrite broke something that worked. The nested-key unit test
  is what proves the fix.
- `pytest tests/contract/test_repository_structure.py` green, which is the
  guard that `docs/delivery/SHA256SUMS` still matches every artefact it names.
- `scripts/acceptance_run.sh` step 1 still prints the transcript quoted at
  `docs/delivery/05_capability_proof.md:142`.
