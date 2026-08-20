<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Chip from "../lib/components/ui/Chip.svelte";
  import { RUN_STATUSES, type RunRow, type RunStatus } from "./api";
  import {
    durationMs,
    formatDuration,
    isStalled,
    progressOf,
    runTone,
  } from "./derive";

  let {
    runs,
    counts,
    total,
    filters,
    loading,
    now,
    onToggleStatus,
    onClearFilters,
    onLoadMore,
    onOpen,
  }: {
    runs: RunRow[];
    counts: Record<RunStatus, number>;
    total: number;
    filters: RunStatus[];
    loading: boolean;
    now: number;
    onToggleStatus: (s: RunStatus) => void;
    onClearFilters: () => void;
    onLoadMore: () => void;
    onOpen: (runId: string) => void;
  } = $props();

  // Anchors only — matching the catalog's guard. There are deliberately no
  // buttons in these rows: an in-row control would both navigate on click and
  // turn one tab stop per row into four.
  function rowClick(event: MouseEvent, runId: string) {
    if (event.defaultPrevented) return;
    if ((event.target as HTMLElement).closest("a")) return;
    if (window.getSelection()?.toString()) return;
    onOpen(runId);
  }
</script>

<section class="px-4 py-3">
  <div class="mb-3 flex flex-wrap items-center gap-2">
    {#each RUN_STATUSES as status (status)}
      <Chip
        pressed={filters.includes(status)}
        count={counts[status]}
        onclick={() => onToggleStatus(status)}
      >
        {status}
      </Chip>
    {/each}
    {#if filters.length}
      <button
        class="rounded border border-line px-2 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        onclick={onClearFilters}
      >
        Clear
      </button>
    {/if}
  </div>

  {#if loading}
    <div class="space-y-2" aria-busy="true">
      {#each Array(4) as _, i (i)}
        <div class="h-9 animate-pulse rounded bg-surface-raised"></div>
      {/each}
    </div>
  {:else if runs.length === 0}
    <div class="rounded-lg border border-dashed border-line p-8 text-center text-sm text-muted-foreground">
      {filters.length ? "No runs match these filters." : "No runs yet."}
    </div>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full border-collapse text-sm">
        <thead>
          <tr class="text-left font-mono text-[10px] tracking-wide text-muted-foreground uppercase">
            <th class="py-2 pr-4">Run</th>
            <th class="py-2 pr-4">Status</th>
            <th class="py-2 pr-4">Progress</th>
            <th class="py-2 pr-4">Scenario / AOI</th>
            <th class="py-2 pr-4">Duration</th>
          </tr>
        </thead>
        <tbody>
          {#each runs as run (run.run_id)}
            {@const p = progressOf(run)}
            <tr
              class="cursor-pointer border-t border-line/60 hover:bg-accent/5"
              onclick={(e) => rowClick(e, run.run_id)}
            >
              <td class="py-2 pr-4">
                <a
                  class="font-mono text-accent hover:underline"
                  href={`#/r/${run.run_id}`}
                  onclick={(e) => e.stopPropagation()}
                >
                  {run.run_id.slice(0, 8)}
                </a>
                <div class="text-[11px] text-muted-foreground">
                  {new Date(run.submitted_at).toLocaleString()}
                </div>
              </td>
              <td class="py-2 pr-4">
                <Badge tone={runTone[run.status]}>{run.status}</Badge>
                {#if isStalled(run, now)}
                  <Badge tone="error" title="every unit finished but the run never closed">
                    stalled
                  </Badge>
                {/if}
              </td>
              <td class="py-2 pr-4">
                {#if p.total === 0}
                  <span class="text-[11px] text-muted-foreground">no units</span>
                {:else}
                  <div class="flex items-center gap-2">
                    <progress
                      class="h-1.5 w-24"
                      value={p.done}
                      max={p.total}
                      aria-label={`${p.done} of ${p.total} units complete`}
                    ></progress>
                    <span class="font-mono text-[11px]" data-numeric>
                      {p.done}/{p.total}{p.failed ? ` · ${p.failed} failed` : ""}
                    </span>
                  </div>
                {/if}
              </td>
              <td class="py-2 pr-4 font-mono text-[11px] text-muted-foreground">
                {run.scenario_id?.slice(0, 8) ?? "—"} / {run.aoi_id?.slice(0, 8) ?? "—"}
              </td>
              <td class="py-2 pr-4 font-mono text-[11px]" data-numeric>
                {formatDuration(durationMs(run.submitted_at, run.completed_at, now))}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="mt-3 flex items-center gap-3 font-mono text-[11px] text-muted-foreground">
      <span data-numeric>{runs.length} of {total} shown</span>
      {#if runs.length < total}
        <button
          class="rounded border border-line px-2 py-1 hover:text-foreground"
          onclick={onLoadMore}
        >
          Load more
        </button>
      {/if}
    </div>
  {/if}
</section>
