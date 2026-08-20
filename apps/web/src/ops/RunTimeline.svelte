<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import type { UnitDetail } from "./api";
  import { formatDuration, timelineOf, unitTone } from "./derive";

  let { units, now }: { units: UnitDetail[]; now: number } = $props();

  const t = $derived(timelineOf(units, now));

  /**
   * Rows in dispatch order, so the picture reads top-to-bottom as the run
   * happened — a dependency chain becomes a diagonal, a wave becomes a block of
   * aligned bars.
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

    <!--
      This is a timeline, not a row of progress bars, and it has to say so.
      A bar positioned by start time reads as "filled from the right" when it
      starts late — so the x-axis is labelled, gridlines run behind every row,
      and the wait before a unit starts is drawn rather than left as blank track.
    -->
    <div class="px-3 py-3">
      <div class="grid grid-cols-[15rem_1fr_5rem] items-center gap-2">
        <span class="font-mono text-[10px] text-muted-foreground uppercase">
          time from start
        </span>
        <div class="relative h-4">
          {#each t.ticks as tick (tick.atPct)}
            <span
              class="absolute top-0 font-mono text-[10px] text-muted-foreground"
              style={`left:${tick.atPct}%;transform:translateX(${
                tick.atPct === 0 ? "0" : tick.atPct === 100 ? "-100%" : "-50%"
              })`}
              data-numeric
            >
              {tick.label}
            </span>
          {/each}
        </div>
        <span></span>
      </div>

      <div class="space-y-1">
        {#each rows as row (row.indicator)}
          <div class="grid grid-cols-[15rem_1fr_5rem] items-center gap-2">
            <div class="min-w-0">
              <div class="truncate font-mono text-[11px]">{row.indicator}</div>
              {#if row.depends_on.length}
                <div class="truncate font-mono text-[10px] text-muted-foreground">
                  after {row.depends_on.join(", ")}
                </div>
              {/if}
            </div>

            <div class="relative h-5 rounded bg-surface-raised">
              <!-- Gridlines, so a bar's position is read against the axis
                   above rather than as a proportion of its own row. -->
              {#each t.ticks as tick (tick.atPct)}
                <span
                  class="absolute inset-y-0 w-px bg-line/60"
                  style={`left:${tick.atPct}%`}
                ></span>
              {/each}

              <!-- The wait. Drawn, not blank: it is the reason a unit that
                   started late starts late, and leaving it empty is what made
                   the bar look like a progress fill pinned to the wrong end. -->
              {#if row.queuedWidth > 0.4}
                <span
                  class="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-sm bg-line"
                  style={`left:${row.queuedLeft}%;width:${row.queuedWidth}%`}
                  title={`waiting ${formatDuration((t.spanMs * row.queuedWidth) / 100)}`}
                ></span>
              {/if}

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
                title={`${row.indicator} · ${row.status} · ran ${formatDuration(row.durationMs)}`}
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
  </div>
{/if}
