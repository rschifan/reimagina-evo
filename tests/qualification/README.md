# Qualification — does the engine's own lifecycle hold?

Everything on this track is built on the premise that Hatchet owns scheduling,
dispatch, retries and worker liveness, and that the application therefore writes
none of it. That premise is load-bearing and was never tested. This suite tests
it, against the pinned engine, and its result gates the track: if the engine
cannot carry worker lifecycle, the answer is a different engine release or no
Track B — **not** an application-side worker registry, reaper or recovery
controller. Building one of those would make the comparison meaningless, because
the whole claim is that the service track does not need them.

```bash
HATCHET_CLIENT_TOKEN=... HATCHET_CLIENT_TLS_STRATEGY=none \
  pytest tests/qualification -q --no-header -rA
```

Skips cleanly with no token, exactly like the conformance suite.

## What is asserted here, and what is asserted elsewhere

This suite covers **worker lifecycle and native run operations** — the things
with no domain content, which nothing else exercises.

Per-unit signal fidelity (`CANCELLED` reported natively, declared task names in
`shape`, `error_message` without a finalizer, `skipped` preserved) is covered by
`tests/conformance`, which asserts the same properties end to end through the
port with a real worker and a real database. Re-asserting them here would be a
second copy of a passing test.

## The finding this suite exists to settle

The branch's spike notes recorded stale worker registrations as a genuine
hazard: killed pytest runs left workers the engine still listed as **ACTIVE**
and still eligible for tasks, runs sat at `running` with no units, and suite
wall clock drifted 62s → 322s as they accumulated. The notes were explicit that
it was unresolved whether the engine ever expires a worker, and that the
question had to be settled before any claim about recovery.

Settled at `hatchet-lite v0.86.24` / `hatchet-sdk 1.35.1`. **The engine's rule
is correct and its expiry is fast: a registration is ACTIVE exactly as long as
something heartbeats for it, and clears ~5 seconds after that stops.** The
problem was never on the engine's side of the wire.

**The worker is a process tree, and killing its root does not kill it.** The
SDK runs its action listener in a spawned child process. Signal only the parent
and that child survives, is reparented to init, keeps its gRPC stream open and
keeps heartbeating — so the engine goes on reporting the worker ACTIVE, entirely
correctly, because something really is alive claiming to be it. It keeps
assigning work the orphan can no longer execute, and runs sit at `running` with
no units. That is the whole of the "stale workers never expire" finding.

Measured on the development machine: **27 orphaned `spawn_main` processes**, all
`PPID 1`, the oldest over a day old, each holding a registration open. Killing
them dropped the engine's ACTIVE count from three to **zero** immediately, with
no change to the engine and no reaping API called.

Two consequences, and neither is application code:

1. **A worker must be supervised as a process tree** — its own container with
   the worker as PID 1, or a supervisor that signals the process *group*. This
   is a deployment requirement and belongs in the runbook.
2. **A red suite on this branch should be checked against `ps` before the
   engine.** `spawn_main` processes with `PPID 1` are the tell.

Separately, a clean exit does deregister. `exit_gracefully` calls
`_pause_task_assignment` first — *"tell the engine that the worker is paused"*.
The earlier "a clean exit leaks exactly like a kill" result came from calling an
`async def` from sync code: it returned a coroutine nobody awaited, so nothing
ever told the engine anything. The reaper that paused stale workers on setup was
compensating for that bug, and is deleted.

## Not automated here, and why

Engine restart, queue saturation and task reassignment need container control
and multi-minute waits. A flaky five-minute test that restarts Docker in CI is
worse than a documented procedure someone actually runs, so they stay in the
runbook as operator checks. `SIGKILL` recovery *is* automated below, because a
subprocess worker makes it deterministic.
