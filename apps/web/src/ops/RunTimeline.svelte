<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import type { UnitDetail } from "./api";
  import { formatDuration, timelineOf, unitTone } from "./derive";

  let { units, now }: { units: UnitDetail[]; now: number } = $props();

  const t = $derived(timelineOf(units, now));

  /**
   * Rows in dispatch order, so the picture reads top-to-bottom as the run
   * happened. Sorting by the bar's own left edge keeps the diagonal that makes
   * a dependency chain visible, and makes a wave of parallel units read as a
   * block of aligned bars.
   */
  const rows = $derived([...t.rows].sort((a, b) => a.runLeft - b.runLeft));
</script>

{#if rows.length}
  <div class="mt-4 rounded-lg border border-line">
    <header
      class="flex flex-wrap items-baseline gap-x-3 border-b border-line px-3 py-2"
    >
      <h3 class="font-mono text-[11px] tracking-wide text-muted-foreground uppercase">
        Execution
      </h3>
      <span class="font-mono text-[11px] text-muted-foreground" data-numeric>
        {rows.length} units · up to {t.peak} at once · {formatDuration(t.elapsedMs)}
      </span>
      <span class="ml-auto flex items-center gap-3 text-[10px] text-muted-foreground">
        <span class="flex items-center gap-1">
          <span class="inline-block h-2 w-3 rounded-sm bg-line"></span> waiting
        </span>
        <span class="flex items-center gap-1">
          <span class="inline-block h-2 w-3 rounded-sm bg-accent"></span> running
        </span>
      </span>
    </header>

    <div class="space-y-1 px-3 py-3">
      {#each rows as row (row.indicator)}
        <div class="grid grid-cols-[15rem_1fr_5rem] items-center gap-2">
          <div class="min-w-0">
            <div class="truncate font-mono text-[11px]">{row.indicator}</div>
            {#if row.depends_on.length}
              <!-- Why this one started when it did: it could not begin until
                   these finished. Restricted to units in this run. -->
              <div class="truncate font-mono text-[10px] text-muted-foreground">
                after {row.depends_on.join(", ")}
              </div>
            {/if}
          </div>

          <div class="relative h-4 rounded bg-surface-raised">
            <!-- Queued: dispatched but waiting on a slot or a prerequisite. -->
            <span
              class="absolute top-1/2 h-1 -translate-y-1/2 rounded-sm bg-line"
              style={`left:${row.queuedLeft}%;width:${row.queuedWidth}%`}
            ></span>
            <span
              class={`absolute top-1/2 h-2.5 -translate-y-1/2 rounded-sm ${
                row.status === "failed"
                  ? "bg-state-error"
                  : row.status === "succeeded"
                    ? "bg-accent"
                    : row.live
                      ? "bg-interactive"
                      : "bg-state-stale"
              }`}
              style={`left:${row.runLeft}%;width:${Math.max(row.runWidth, 0.6)}%`}
              title={`${row.indicator} · ${row.status} · ${formatDuration(row.durationMs)}`}
            ></span>
          </div>

          <div class="flex items-center justify-end gap-1.5">
            <span class="font-mono text-[10.5px] text-muted-foreground" data-numeric>
              {formatDuration(row.durationMs)}
            </span>
            <Badge tone={unitTone[row.status]}>{row.status.slice(0, 4)}</Badge>
          </div>
        </div>
      {/each}
    </div>
  </div>
{/if}
