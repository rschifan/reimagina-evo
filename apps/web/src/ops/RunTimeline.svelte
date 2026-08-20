<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import type { UnitDetail } from "./api";
  import { formatDuration, timelineOf, unitTone, PX_PER_SECOND } from "./derive";

  let { units, now }: { units: UnitDetail[]; now: number } = $props();

  const t = $derived(timelineOf(units, now));

  /**
   * Rows in dispatch order, so the picture reads top-to-bottom as the run
   * happened — a dependency chain becomes a diagonal, a wave a block of bars.
   */
  const rows = $derived([...t.rows].sort((a, b) => a.runLeftPx - b.runLeftPx));

  let scroller = $state<HTMLElement | null>(null);
  const liveEdge = $derived(
    t.rows.some((r) => r.live) ? Math.max(...t.rows.map((r) => r.runLeftPx + r.runWidthPx)) : null,
  );

  // Follow a growing run, but only when the reader is already at the right-hand
  // end — yanking the view while someone is inspecting an earlier unit would be
  // its own kind of "it moved on its own".
  $effect(() => {
    if (liveEdge === null || !scroller) return;
    const atEnd =
      scroller.scrollWidth - scroller.scrollLeft - scroller.clientWidth < 48;
    if (atEnd) scroller.scrollLeft = scroller.scrollWidth;
  });
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

    <div class="flex">
      <!-- Labels sit outside the scroller so they stay put while the track
           scrolls; the two panes share a row height to stay aligned. -->
      <div class="shrink-0 border-r border-line/60 py-3">
        <div class="h-5 px-3 font-mono text-[10px] text-muted-foreground uppercase">
          unit
        </div>
        {#each rows as row (row.indicator)}
          <div class="h-9 w-56 px-3">
            <div class="truncate font-mono text-[11px]">{row.indicator}</div>
            {#if row.depends_on.length}
              <div class="truncate font-mono text-[10px] text-muted-foreground">
                after {row.depends_on.join(", ")}
              </div>
            {/if}
          </div>
        {/each}
      </div>

      <!--
        A fixed pixels-per-second track, wide enough for the whole run and
        scrolled when that exceeds the panel.

        This is the point of the layout: a bar's position and width are computed
        from its own timestamps alone, never from the run's total. Nothing
        already drawn moves when the run gets longer — a running bar simply
        grows to the right, which is the direction time goes.
      -->
      <div bind:this={scroller} class="grow overflow-x-auto py-3">
        <div class="relative" style={`width:${Math.max(t.widthPx, 1)}px`}>
          <div class="relative h-5">
            {#each t.ticks as tick (tick.atPx)}
              <span
                class="absolute top-0 font-mono text-[10px] text-muted-foreground"
                style={`left:${tick.atPx}px`}
                data-numeric
              >
                {tick.label}
              </span>
            {/each}
          </div>

          {#each rows as row (row.indicator)}
            <div class="relative h-9">
              {#each t.ticks as tick (tick.atPx)}
                <span
                  class="absolute inset-y-0 w-px bg-line/40"
                  style={`left:${tick.atPx}px`}
                ></span>
              {/each}

              <!-- The wait, drawn rather than left blank: it is why a unit that
                   started late started late. -->
              {#if row.queuedWidthPx > 1}
                <span
                  class="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-sm bg-line"
                  style={`left:${row.queuedLeftPx}px;width:${row.queuedWidthPx}px`}
                  title={`waited ${formatDuration(
                    ((row.runLeftPx - row.queuedLeftPx) / PX_PER_SECOND) * 1000,
                  )}`}
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
                style={`left:${row.runLeftPx}px;width:${Math.max(row.runWidthPx, 2)}px`}
                title={`${row.indicator} · ${row.status} · ran ${formatDuration(row.durationMs)}`}
              ></span>
            </div>
          {/each}
        </div>
      </div>

      <div class="shrink-0 border-l border-line/60 py-3">
        <div class="h-5"></div>
        {#each rows as row (row.indicator)}
          <div class="flex h-9 items-start gap-1.5 px-3">
            <span class="font-mono text-[10.5px] text-muted-foreground" data-numeric>
              {formatDuration(row.durationMs)}
            </span>
            <Badge tone={unitTone[row.status]}>{row.status.slice(0, 4)}</Badge>
          </div>
        {/each}
      </div>
    </div>
  </div>
{/if}
