<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import type { RunDetailResponse } from "./api";
  import RunTimeline from "./RunTimeline.svelte";
  import { cancel, ops, remove, rerun } from "./store.svelte";
  import {
    canCancel,
    canRerun,
    durationMs,
    formatDuration,
    isRunTerminal,
    isStalled,
    runTone,
  } from "./derive";

  let {
    detail,
    now,
    onBack,
    refreshNow,
  }: {
    detail: RunDetailResponse;
    now: number;
    onBack: () => void;
    refreshNow: () => void;
  } = $props();

  // Cancel is effectively irreversible — nothing resumes a cancelled unit, it
  // can only be rerun as a new run — and deleting a run is plainly so. This
  // codebase has no modal library, so each arms itself for four seconds and
  // wants a second press. Tracked by name so arming one does not arm the other.
  let armed = $state<"cancel" | "delete" | null>(null);
  let armedTimer: ReturnType<typeof setTimeout> | null = null;
  let actionError = $state<string | null>(null);

  function arm(which: "cancel" | "delete") {
    armed = which;
    if (armedTimer) clearTimeout(armedTimer);
    armedTimer = setTimeout(() => (armed = null), 4_000);
  }

  $effect(() => () => {
    if (armedTimer) clearTimeout(armedTimer);
  });

  async function doCancel() {
    if (armed !== "cancel") return arm("cancel");
    armed = null;
    actionError = await cancel(run.run_id, refreshNow);
  }

  async function doDelete() {
    if (armed !== "delete") return arm("delete");
    armed = null;
    const error = await remove(run.run_id, refreshNow);
    if (error) {
      actionError = error;
      return;
    }
    // The run no longer exists; staying on its detail would 404 on next tick.
    onBack();
  }

  async function doRerun() {
    actionError = null;
    const { error, newRunId } = await rerun(run.run_id, refreshNow);
    if (error) {
      actionError = error;
      return;
    }
    if (newRunId) location.hash = `#/r/${newRunId}`;
  }

  const run = $derived(detail.run);

  let heading = $state<HTMLElement | null>(null);
  // Hash routing swaps this subtree, which destroys whatever had focus and
  // drops it silently to <body>. Move it somewhere meaningful instead.
  $effect(() => {
    heading?.focus();
  });
</script>

<section class="px-4 py-3">
  <a
    class="font-mono text-[11px] text-accent hover:underline"
    href="#/"
    onclick={(e) => {
      e.preventDefault();
      onBack();
    }}
  >
    &larr; All runs
  </a>

  <h1
    bind:this={heading}
    tabindex="-1"
    class="mt-2 font-mono text-lg outline-none"
  >
    {run.run_id}
  </h1>

  <div class="mt-3 flex flex-wrap items-center gap-2">
    {#if canCancel(run)}
      <button
        type="button"
        onclick={doCancel}
        disabled={ops.isBusy(`${run.run_id}:cancel`)}
        aria-busy={ops.isBusy(`${run.run_id}:cancel`)}
        class={`rounded border px-3 py-1 text-xs disabled:opacity-40 ${
          armed === "cancel"
            ? "border-state-error/60 bg-state-error/15 text-state-error"
            : "border-line text-muted-foreground hover:text-foreground"
        }`}
      >
        {armed === "cancel" ? "Confirm cancel?" : "Cancel run"}
      </button>
    {/if}

    {#if canRerun(run)}
      <button
        type="button"
        onclick={doRerun}
        disabled={ops.isBusy(`${run.run_id}:rerun`)}
        aria-busy={ops.isBusy(`${run.run_id}:rerun`)}
        title="Recomputes whole indicators — the original per-feature scope is not retained"
        class="rounded border border-accent/40 bg-accent/10 px-3 py-1 text-xs text-accent
               hover:bg-accent/20 disabled:opacity-40"
      >
        Rerun {run.units_total - run.units_succeeded} unfinished
      </button>
    {/if}

    {#if !canCancel(run)}
      <button
        type="button"
        onclick={doDelete}
        disabled={ops.isBusy(`${run.run_id}:delete`)}
        aria-busy={ops.isBusy(`${run.run_id}:delete`)}
        title="Removes this run from the history. The values it computed are not affected."
        class={`ml-auto rounded border px-3 py-1 text-xs disabled:opacity-40 ${
          armed === "delete"
            ? "border-state-error/60 bg-state-error/15 text-state-error"
            : "border-line text-muted-foreground hover:text-foreground"
        }`}
      >
        {armed === "delete" ? "Confirm delete?" : "Delete run"}
      </button>
    {/if}

    {#if actionError}
      <span class="font-mono text-[11px] text-state-error" role="status">
        {actionError}
      </span>
    {/if}
  </div>

  <Card title="Run" class="mt-3 max-w-xl">
    <dl class="grid grid-cols-2 gap-y-1 font-mono text-[11px]">
      <dt class="text-muted-foreground">status</dt>
      <dd>
        <Badge tone={runTone[run.status]}>{run.status}</Badge>
        {#if isStalled(run, now)}
          <Badge tone="error" title="every unit finished but the run never closed">
            stalled
          </Badge>
        {/if}
      </dd>
      <dt class="text-muted-foreground">submitted</dt>
      <dd data-numeric>{new Date(run.submitted_at).toLocaleString()}</dd>
      <dt class="text-muted-foreground">completed</dt>
      <dd data-numeric>
        {run.completed_at ? new Date(run.completed_at).toLocaleString() : "—"}
      </dd>
      <dt class="text-muted-foreground">duration</dt>
      <dd data-numeric>
        {formatDuration(durationMs(run.submitted_at, run.completed_at, now))}
      </dd>
      <dt class="text-muted-foreground">code version</dt>
      <dd>{run.code_version || "—"}</dd>
      <dt class="text-muted-foreground">scenario</dt>
      <dd>{run.scenario_id ?? "—"}</dd>
      <dt class="text-muted-foreground">aoi</dt>
      <dd>{run.aoi_id ?? "—"}</dd>
    </dl>
  </Card>

  <RunTimeline
    units={detail.units}
    slots={detail.unit_slots}
    {now}
    live={!isRunTerminal(run.status)}
  />
</section>
