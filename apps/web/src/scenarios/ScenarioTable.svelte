<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import { unitLabel } from "../format";
  import { cn } from "../lib/utils";

  import { formatCell, NO_BASELINE, statusFor, statusLabel, statusTone } from "./derive";
  import {
    BASELINE,
    confirmRemoveScenario,
    recomputeSelected,
    select,
    ws,
    type Cell,
  } from "./store.svelte";

  /**
   * The scenario list: every scenario on the pilot, side by side, on the
   * indicators a rule can change. It is also where a scenario is picked,
   * recomputed and deleted — the page has no other place for those. One
   * number per cell — a total for counts, a median for rates; a total also
   * shows its change against the baseline city.
   */
  const pilotName = $derived(ws.aois.find((a) => a.aoi_id === ws.aoiId)?.name ?? "");

  function cellText(cell: Cell | undefined, slug: string): string {
    const indicator = ws.indicators.find((i) => i.slug === slug);
    if (!indicator) return "";
    if (cell === undefined) return "…";
    if (cell.error) return "?";
    return formatCell(cell.value, indicator);
  }

  /** "+1,841" for a total that differs from the baseline's, else "". */
  function change(cell: Cell | undefined, slug: string): string {
    const base = ws.cells[BASELINE]?.[slug]?.value;
    if (cell?.value == null || base == null) return "";
    const d = Math.round(cell.value - base);
    return d === 0 ? "" : `${d > 0 ? "+" : "−"}${Math.abs(d).toLocaleString("en-US")}`;
  }

  function header(statistic: "sum" | "median"): string {
    return statistic === "sum"
      ? "Total over the pilot: the scenario's potential, not a measurement"
      : "Median across the pilot's blocks";
  }

  /** A recompute of the selected scenario is queued, running or followed. */
  const running = $derived(
    ws.busy || (ws.selectedId !== null && ws.following[ws.selectedId] !== undefined),
  );

  const rowClass = (selected: boolean) =>
    cn(
      "cursor-pointer border-b border-line hover:bg-accent/5",
      selected && "bg-accent/10 outline outline-1 outline-accent/40",
    );
</script>

<Card class="shrink-0" title={pilotName ? `Scenarios · ${pilotName}` : "Scenarios"}>
  {#if ws.aoiId === null}
    <p class="text-sm text-muted-foreground">Pick a pilot to see its scenarios.</p>
  {:else}
    <div class="max-h-[26vh] overflow-auto">
      <table class="w-full border-collapse text-sm">
        <thead>
          <tr class="sticky top-0 border-b border-line bg-surface text-left">
            <th scope="col" class="py-2 pr-4 font-medium">Scenario</th>
            {#each ws.indicators as indicator (indicator.slug)}
              <th
                scope="col"
                class="py-2 pr-4 text-right font-medium"
                title={header(indicator.statistic)}
              >
                {indicator.name}
                <span class="block font-mono text-[10px] text-muted-foreground">
                  {indicator.statistic === "sum" ? "total" : "median"} · {indicator.unit ? unitLabel(indicator.unit) : "—"}
                </span>
              </th>
            {/each}
            <th scope="col" class="py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          <!-- A row is picked by a click anywhere on it; the name is the
               button, so the keyboard reaches it too. -->
          <tr
            aria-current={ws.selectedId === null ? "true" : undefined}
            class={rowClass(ws.selectedId === null)}
            onclick={() => !ws.busy && select(null)}
          >
            <th scope="row" class="py-2 pr-4 text-left font-normal">
              <button type="button" class="hover:underline disabled:opacity-50" disabled={ws.busy} onclick={(e) => { e.stopPropagation(); select(null); }}>
                Baseline city
              </button>
            </th>
            {#each ws.indicators as indicator (indicator.slug)}
              {@const cell = ws.cells[BASELINE]?.[indicator.slug]}
              <td
                class="py-2 pr-4 text-right font-mono tabular-nums"
                title={cell?.error ??
                  (cell && cell.value === null ? `No baseline: ${NO_BASELINE}` : undefined)}
              >
                {cellText(cell, indicator.slug)}
              </td>
            {/each}
            <td class="py-2"></td>
          </tr>
          {#each ws.scenarios as row (row.id)}
            {@const status = statusFor(row, ws.following[row.id] ?? null)}
            {@const selected = ws.selectedId === row.id}
            <tr
              aria-current={selected ? "true" : undefined}
              class={rowClass(selected)}
              onclick={() => !ws.busy && select(row.id)}
            >
              <th scope="row" class="py-2 pr-4 text-left font-normal">
                <button type="button" class="hover:underline disabled:opacity-50" disabled={ws.busy} onclick={(e) => { e.stopPropagation(); select(row.id); }}>
                  {row.name}
                </button>
              </th>
              {#each ws.indicators as indicator (indicator.slug)}
                {@const cell = ws.cells[row.id]?.[indicator.slug]}
                {@const delta = indicator.statistic === "sum" ? change(cell, indicator.slug) : ""}
                <td
                  class="py-2 pr-4 text-right font-mono tabular-nums"
                  title={cell?.error ?? undefined}
                >
                  {cellText(cell, indicator.slug)}
                  {#if delta}<span class="ml-1 text-xs text-accent">{delta}</span>{/if}
                </td>
              {/each}
              <td class="py-2 whitespace-nowrap">
                {#if selected && running}
                  <Badge tone={statusTone("computing")}>Recomputing…</Badge>
                {:else if selected && ws.rules.length === 0}
                  <span class="text-xs text-muted-foreground">No rules yet: draw an area on the map</span>
                {:else}
                  <Badge tone={statusTone(status)}>{statusLabel(status)}</Badge>
                {/if}
                {#if selected && !running && ws.rules.length > 0 && (status === "stale" || status === "failed" || status === "partial" || status === "not-computed")}
                  <button
                    type="button"
                    class="ml-2 rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-xs text-accent"
                    onclick={(e) => { e.stopPropagation(); void recomputeSelected(); }}
                  >
                    Recompute
                  </button>
                {/if}
                <button
                  type="button"
                  class="ml-2 rounded px-1.5 py-0.5 text-xs text-state-error hover:bg-state-error/10 disabled:opacity-50"
                  aria-label="Delete {row.name}"
                  disabled={ws.busy}
                  onclick={(e) => { e.stopPropagation(); void confirmRemoveScenario(row.id); }}
                >
                  Delete
                </button>
              </td>
            </tr>
          {/each}
          <tr>
            <td colspan={ws.indicators.length + 2} class="py-2 text-xs text-muted-foreground">
              New scenario: on <strong class="font-medium text-foreground">Baseline city</strong>,
              draw an area on the map and set its numbers; you name the scenario in the same box.
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  {/if}
</Card>
