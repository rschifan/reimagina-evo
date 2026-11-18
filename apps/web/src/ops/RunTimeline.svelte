<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import type { UnitDetail } from "./api";
  import { timelineModel, UNIT_VISUAL, type RowState } from "./timeline";

  let {
    units,
    slots,
    now,
    live,
  }: { units: UnitDetail[]; slots: number; now: number; live: boolean } = $props();

  // Every rule lives in timeline.ts; this file only draws the model.
  const m = $derived(timelineModel(units, slots, now, live));

  const legend = Object.entries(UNIT_VISUAL) as [RowState, (typeof UNIT_VISUAL)[RowState]][];

  /** The strip as one step path in a 100 × 32 box (x in %, y in px). */
  const STRIP_H = 32;
  const y = (running: number) => STRIP_H - (Math.min(running, slots) / Math.max(slots, 1)) * (STRIP_H - 4);
  const stripPath = $derived.by(() => {
    if (!m.strip.length) return "";
    let d = `M ${m.strip[0].pct} ${STRIP_H} V ${y(m.strip[0].running)}`;
    for (let i = 1; i < m.strip.length; i++) {
      d += ` H ${m.strip[i].pct} V ${y(m.strip[i].running)}`;
    }
    return `${d} V ${STRIP_H} Z`;
  });

  const tickShift = (i: number, n: number) =>
    i === 0 ? "" : i === n - 1 ? "-translate-x-full" : "-translate-x-1/2";
</script>

{#snippet guides()}
  {#if m.forecastPct !== null}
    <div
      class="pointer-events-none absolute inset-y-0 border-l border-dashed border-muted-foreground/60"
      style:left={`${m.forecastPct}%`}
    ></div>
  {/if}
  {#if m.nowPct !== null}
    <div
      class="pointer-events-none absolute inset-y-0 w-px bg-foreground"
      style:left={`${m.nowPct}%`}
    ></div>
  {/if}
{/snippet}

<Card title="Execution" class="mt-4">
  {#if !units.length}
    <p class="text-[11px] text-muted-foreground">This run has no units.</p>
  {:else}
    <div class="flex flex-wrap items-center gap-1.5">
      {#each m.chips as chip (chip.text)}
        <Badge tone={chip.tone}>{chip.text}</Badge>
      {/each}
    </div>

    <ul class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
      {#each legend as [state, v] (state)}
        <li class="flex items-center gap-1.5">
          <span class={`size-2 shrink-0 rounded-full ${v.dot}`}></span>{v.label}
        </li>
      {/each}
      <li class="flex items-center gap-1.5">
        <span class="h-2 w-3.5 rounded-sm border border-dashed border-state-partial"></span>
        expected (last run)
      </li>
    </ul>

    <div class="mt-4 grid grid-cols-[16rem_minmax(0,1fr)_7rem] items-center gap-x-3">
      <span class="text-[11px] text-muted-foreground">parallel units</span>
      <div class="relative h-8">
        <svg
          class="absolute inset-0 h-full w-full overflow-visible"
          viewBox={`0 0 100 ${STRIP_H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Units running at once, out of ${slots} slots`}
        >
          <line
            x1="0"
            x2="100"
            y1={y(slots)}
            y2={y(slots)}
            class="stroke-muted-foreground"
            stroke-dasharray="3 3"
            vector-effect="non-scaling-stroke"
          />
          <path d={stripPath} class="fill-state-partial/40" />
        </svg>
        {@render guides()}
      </div>
      <span class="text-right font-mono text-[11px] text-muted-foreground">
        {slots} slots
      </span>

      {#each m.rows as row (row.unit.indicator)}
        {@const v = UNIT_VISUAL[row.state]}
        <span class="flex h-7 min-w-0 items-center gap-2">
          <span class={`size-2 shrink-0 rounded-full ${v.dot}`} title={v.label}></span>
          <span class="truncate font-mono text-[11px]" title={row.unit.indicator}>
            {row.unit.indicator}
          </span>
        </span>

        <div class="relative h-7">
          {#if row.expectedPct !== null}
            <div
              class="absolute top-1/2 h-3 -translate-y-1/2 rounded-sm border border-dashed border-state-partial"
              style:left={`${row.leftPct}%`}
              style:width={`${row.expectedPct}%`}
            ></div>
          {/if}
          {#if v.bar && row.unit.started_at}
            <div
              class={`absolute top-1/2 h-3 min-w-[2px] -translate-y-1/2 rounded-sm ${v.bar}`}
              style:left={`${row.leftPct}%`}
              style:width={`${row.widthPct}%`}
            ></div>
          {/if}
          {#if row.note}
            <span
              class={`absolute top-1/2 max-w-[60%] -translate-y-1/2 truncate pl-2 text-[11px] ${
                row.state === "ready" ? "text-state-stale" : "text-muted-foreground"
              }`}
              style:left={`${Math.min(m.nowPct ?? 0, 40)}%`}
              title={row.note}
            >
              {row.note}
            </span>
          {/if}
          {@render guides()}
        </div>

        <span
          class={`text-right font-mono text-[11px] tabular-nums whitespace-nowrap ${
            row.over ? "text-state-stale" : "text-muted-foreground"
          }`}
        >
          {row.numbers}
        </span>

        {#if row.state === "failed" && row.unit.detail}
          <!-- A ≤500-char exception repr; <details> is keyboard-reachable,
               which a title= tooltip is not. -->
          <details class="col-span-3 mb-1 pl-4">
            <summary class="cursor-pointer font-mono text-[10.5px] text-state-error">
              {row.unit.detail.slice(0, 80)}{row.unit.detail.length > 80 ? "…" : ""}
            </summary>
            <p class="mt-1 font-mono text-[10.5px] break-words whitespace-pre-wrap text-state-error">
              {row.unit.detail}
            </p>
          </details>
        {/if}
      {/each}

      <span></span>
      <div class="relative h-4 font-mono text-[10px] text-muted-foreground">
        {#each m.ticks as tick, i (i)}
          <span
            class={`absolute top-0 whitespace-nowrap ${tickShift(i, m.ticks.length)}`}
            style:left={`${tick.pct}%`}
          >
            {tick.label}
          </span>
        {/each}
      </div>
      <span></span>
    </div>
  {/if}
</Card>
