<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import type { RunDetailResponse } from "./api";
  import RunTimeline from "./RunTimeline.svelte";
  import { cancel, ops, rerun } from "./store.svelte";
  import {
    canCancel,
    canRerun,
    durationMs,
    formatDuration,
    isStalled,
    progressOf,
    runTone,
    unitTone,
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
  // can only be rerun as a new run — and this codebase has no modal library.
  // A two-press button is the cheapest honest confirmation.
  let armed = $state(false);
  let armedTimer: ReturnType<typeof setTimeout> | null = null;
  let actionError = $state<string | null>(null);

  function arm() {
    armed = true;
    if (armedTimer) clearTimeout(armedTimer);
    armedTimer = setTimeout(() => (armed = false), 4_000);
  }

  $effect(() => () => {
    if (armedTimer) clearTimeout(armedTimer);
  });

  async function doCancel() {
    if (!armed) return arm();
    armed = false;
    actionError = await cancel(run.run_id, refreshNow);
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
  const p = $derived(progressOf(run));

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
          armed
            ? "border-state-error/60 bg-state-error/15 text-state-error"
            : "border-line text-muted-foreground hover:text-foreground"
        }`}
      >
        {armed ? "Confirm cancel?" : "Cancel run"}
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

    {#if actionError}
      <span class="font-mono text-[11px] text-state-error" role="status">
        {actionError}
      </span>
    {/if}
  </div>

  <div class="mt-3 grid gap-3 sm:grid-cols-2">
    <Card title="Run">
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

    <Card title="Progress">
      {#if p.total === 0}
        <p class="text-[11px] text-muted-foreground">This run has no units.</p>
      {:else}
        <progress
          class="h-2 w-full"
          value={p.done}
          max={p.total}
          aria-label={`${p.done} of ${p.total} units complete`}
        ></progress>
        <p class="mt-2 font-mono text-[11px]" data-numeric>
          {p.done} of {p.total} units{p.failed ? ` · ${p.failed} failed` : ""}
        </p>
      {/if}
    </Card>
  </div>

  <RunTimeline units={detail.units} {now} />

  <div class="mt-4 overflow-x-auto">
    <table class="w-full border-collapse text-sm">
      <thead>
        <tr class="text-left font-mono text-[10px] tracking-wide text-muted-foreground uppercase">
          <th class="py-2 pr-4">Indicator</th>
          <th class="py-2 pr-4">Status</th>
          <th class="py-2 pr-4">Started</th>
          <th class="py-2 pr-4">Duration</th>
          <th class="py-2 pr-4">Detail</th>
        </tr>
      </thead>
      <tbody>
        {#each detail.units as unit (unit.indicator)}
          <tr class="border-t border-line/60 align-top">
            <td class="py-2 pr-4 font-mono text-[12px]">{unit.indicator}</td>
            <td class="py-2 pr-4">
              <Badge tone={unitTone[unit.status]}>{unit.status}</Badge>
            </td>
            <td class="py-2 pr-4 font-mono text-[11px]" data-numeric>
              {unit.started_at ? new Date(unit.started_at).toLocaleTimeString() : "—"}
            </td>
            <td class="py-2 pr-4 font-mono text-[11px]" data-numeric>
              {formatDuration(durationMs(unit.started_at, unit.completed_at, now))}
            </td>
            <td class="max-w-md py-2 pr-4">
              {#if unit.detail}
                <!-- A ≤500-char exception repr, not a traceback, but still too
                     long for a cell. <details> is keyboard-reachable, which a
                     title= tooltip is not. -->
                <details>
                  <summary
                    class="cursor-pointer font-mono text-[10.5px] text-state-error"
                  >
                    {unit.detail.slice(0, 80)}{unit.detail.length > 80 ? "…" : ""}
                  </summary>
                  <p
                    class="mt-1 font-mono text-[10.5px] break-words whitespace-pre-wrap text-state-error"
                  >
                    {unit.detail}
                  </p>
                </details>
              {:else}
                <span class="text-[11px] text-muted-foreground">—</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>
