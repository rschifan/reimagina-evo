<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import { cn } from "../lib/utils";

  import { formatCell, statusFor, statusLabel, statusTone } from "./derive";
  import { BASELINE, select, ws, type Cell } from "./store.svelte";

  /**
   * Every scenario on the pilot, side by side, on the indicators a rule can
   * change. One number per cell — a total for counts, a median for rates —
   * because comparing scenarios with each other is where differences show:
   * the real pilots have no baseline for these indicators at all.
   */
  const pilotName = $derived(ws.aois.find((a) => a.aoi_id === ws.aoiId)?.name ?? "");

  function cellText(cell: Cell | undefined, slug: string): string {
    const indicator = ws.indicators.find((i) => i.slug === slug);
    if (!indicator) return "";
    if (cell === undefined) return "…";
    if (cell.error) return "?";
    return formatCell(cell.value, indicator);
  }

  function header(statistic: "sum" | "median"): string {
    return statistic === "sum"
      ? "Total over the pilot: the scenario's potential, not a measurement"
      : "Median across the pilot's blocks";
  }
</script>

<Card title={pilotName ? `Scenarios · ${pilotName}` : "Scenarios"}>
  {#if ws.aoiId === null}
    <p class="text-sm text-muted-foreground">Pick a pilot to see its scenarios.</p>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full border-collapse text-sm">
        <thead>
          <tr class="border-b border-line text-left">
            <th scope="col" class="py-2 pr-4 font-medium">Scenario</th>
            {#each ws.indicators as indicator (indicator.slug)}
              <th
                scope="col"
                class="py-2 pr-4 text-right font-medium"
                title={header(indicator.statistic)}
              >
                {indicator.name}
                <span class="block font-mono text-[10px] text-muted-foreground">
                  {indicator.statistic === "sum" ? "total" : "median"} · {indicator.unit ?? "—"}
                </span>
              </th>
            {/each}
            <th scope="col" class="py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          <tr
            aria-selected={ws.selectedId === null}
            class={cn(
              "border-b border-line",
              ws.selectedId === null && "bg-accent/10 outline outline-1 outline-accent/40",
            )}
          >
            <th scope="row" class="py-2 pr-4 text-left font-normal">
              <button type="button" class="hover:underline" onclick={() => select(null)}>
                Baseline city
              </button>
            </th>
            {#each ws.indicators as indicator (indicator.slug)}
              {@const cell = ws.cells[BASELINE]?.[indicator.slug]}
              <td class="py-2 pr-4 text-right font-mono tabular-nums" title={cell?.error ?? undefined}>
                {cellText(cell, indicator.slug)}
              </td>
            {/each}
            <td class="py-2"></td>
          </tr>
          {#each ws.scenarios as row (row.id)}
            {@const status = statusFor(row, ws.following[row.id] ?? null)}
            <tr
              aria-selected={ws.selectedId === row.id}
              class={cn(
                "border-b border-line",
                ws.selectedId === row.id && "bg-accent/10 outline outline-1 outline-accent/40",
              )}
            >
              <th scope="row" class="py-2 pr-4 text-left font-normal">
                <button type="button" class="hover:underline" onclick={() => select(row.id)}>
                  {row.name}
                </button>
              </th>
              {#each ws.indicators as indicator (indicator.slug)}
                {@const cell = ws.cells[row.id]?.[indicator.slug]}
                <td
                  class="py-2 pr-4 text-right font-mono tabular-nums"
                  title={cell?.error ?? undefined}
                >
                  {cellText(cell, indicator.slug)}
                </td>
              {/each}
              <td class="py-2">
                <Badge tone={statusTone(status)}>{statusLabel(status)}</Badge>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if ws.scenarios.length === 0}
      <p class="mt-3 text-sm text-muted-foreground">
        No scenarios on this pilot yet. Name one above to start.
      </p>
    {/if}
  {/if}
</Card>
