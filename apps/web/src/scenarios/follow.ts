/**
 * Follow the run a rule change or recompute enqueued. No DOM.
 *
 * It used to live inside the sidebar panel's closure, where the only way to
 * test it was to click through a rendered panel. Each rule below exists
 * because its absence once shipped.
 *
 * Polling rather than the SSE stream `/jobs/{id}/events`: the page needs one
 * bit — has it landed — and a stream would be a second long-lived connection
 * per open tab for something a planner watches for a few seconds. The Runs
 * page is where a run is actually followed, and `handedOver` says so.
 */
import { getRun, type RunView } from "../api";
import { isTerminalRunStatus, runProducedResults } from "../scenario";

/** How often, and for how long, the page follows the run it enqueued. Two
 *  minutes covers a synthetic territory and a small pilot; a Mexicali-scale
 *  recompute outlives it and is handed over rather than reported lost. */
export const RUN_POLL_MS = 500;
export const MAX_RUN_POLLS = 240;
/** Consecutive unreadable polls tolerated. One network blip must not end it. */
export const MAX_RUN_READ_FAILURES = 3;

export interface FollowEvents {
  /** Still going; `status` is the run's own word for it. */
  progress(status: string): void;
  /** Finished with results (`succeeded` or `partial`) — redraw from them. */
  landed(run: RunView): void;
  /** Finished without results. The overrides stand either way. */
  failed(run: RunView): void;
  /** The run could not be read, repeatedly. Not a failed run: unknown. */
  unreadable(): void;
  /** Still running at the poll cap; the Runs page has it from here. */
  handedOver(status: string): void;
}

/**
 * Poll `runId` until it ends, and report through `on`. Returns `cancel`.
 *
 * ⚠ After `cancel()` nothing fires — including a tick already awaiting
 * `getRun`. Clearing the timer alone is not enough: the old panel followed a
 * run whose scenario was no longer on screen and wrote its status, and
 * redrew the map, over the scenario that was.
 */
export function followRun(runId: string, on: FollowEvents): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempts = 0;
  let readFailures = 0;

  const schedule = (): void => {
    timer = setTimeout(() => void tick(), RUN_POLL_MS);
  };

  const tick = async (): Promise<void> => {
    if (cancelled) return;
    attempts += 1;
    let run: RunView;
    try {
      run = await getRun(runId);
    } catch (err) {
      if (cancelled) return;
      // A poll that could not be read is not a recompute that failed.
      console.warn("could not read run", runId, err);
      readFailures += 1;
      if (readFailures < MAX_RUN_READ_FAILURES && attempts < MAX_RUN_POLLS) {
        schedule();
        return;
      }
      on.unreadable();
      return;
    }
    if (cancelled) return;
    readFailures = 0;
    if (isTerminalRunStatus(run.status)) {
      if (runProducedResults(run.status)) on.landed(run);
      else on.failed(run);
      return;
    }
    if (attempts >= MAX_RUN_POLLS) {
      on.handedOver(run.status);
      return;
    }
    on.progress(run.status);
    schedule();
  };

  schedule();
  return () => {
    cancelled = true;
    if (timer !== null) clearTimeout(timer);
  };
}
