<script lang="ts">
  import { ArrowLeft, Download } from "@lucide/svelte";

  import Badge from "./lib/components/ui/Badge.svelte";
  import Card from "./lib/components/ui/Card.svelte";
  import { downloadUrl, type IndicatorStats } from "./api";
  import { boundsFraction, familyOf } from "./derive";
  import { fmtNumber } from "../format";
  import { catalog, statsFor } from "./store.svelte";

  let { slug }: { slug: string } = $props();

  const indicator = $derived(catalog.bySlug(slug));
  // The finest declared grain is what the map draws and what the stats
  // endpoint is asked for, so the two agree by construction.
  const grain = $derived(indicator?.applies_to[0]);

  let stats = $state<IndicatorStats | null>(null);
  let statsError = $state<string | null>(null);

  $effect(() => {
    if (!indicator || !grain) return;
    let live = true;
    statsFor(indicator.slug, grain)
      .then((s) => live && (stats = s))
      .catch((e) => live && (statsError = e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  });

  const p50Pos = $derived(
    boundsFraction(stats?.p50 ?? null, indicator?.valid_min ?? null, indicator?.valid_max ?? null),
  );
  const p05Pos = $derived(
    boundsFraction(stats?.p05 ?? null, indicator?.valid_min ?? null, indicator?.valid_max ?? null),
  );
  const p95Pos = $derived(
    boundsFraction(stats?.p95 ?? null, indicator?.valid_min ?? null, indicator?.valid_max ?? null),
  );
</script>

<div class="flex flex-col gap-5">
  <a href="#/" class="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
    <ArrowLeft class="size-3.5" aria-hidden="true" />
    All indicators
  </a>

  {#if !indicator}
    <div class="rounded-lg border border-dashed border-line p-8">
      <p class="text-sm">No indicator called <code class="font-mono">{slug}</code>.</p>
      <p class="mt-1 text-xs text-muted-foreground">
        It may exist in the catalog but hold no values in this AOI, in which case
        the list does not offer it.
      </p>
    </div>
  {:else}
    <header class="flex flex-wrap items-start gap-x-4 gap-y-2">
      <div>
        <h1 class="text-xl font-semibold tracking-tight">{indicator.name}</h1>
        <p class="mt-1 font-mono text-[11px] text-muted-foreground">{indicator.slug}</p>
      </div>
      <div class="flex flex-wrap gap-1.5 pt-1">
        <Badge>{familyOf(indicator.slug)}</Badge>
        {#each indicator.applies_to as g (g)}
          <Badge tone="accent">{g}</Badge>
        {/each}
      </div>
    </header>

    <div class="grid gap-4 lg:grid-cols-2">
      <Card title="Definition">
        <dl class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[13px]">
          <dt class="text-muted-foreground">Unit</dt>
          <dd class="font-mono text-[11.5px]">{indicator.unit ?? "unitless"}</dd>
          <dt class="text-muted-foreground">Declared range</dt>
          <dd class="font-mono text-[11.5px]" data-numeric>
            {indicator.valid_min ?? "—"} – {indicator.valid_max ?? "—"}
          </dd>
          <dt class="text-muted-foreground">Grains</dt>
          <dd class="font-mono text-[11.5px]">{indicator.applies_to.join(", ")}</dd>
          <dt class="text-muted-foreground">Drawn at</dt>
          <dd class="font-mono text-[11.5px]">{grain}</dd>
        </dl>
      </Card>

      <Card title="Observed values · this AOI">
        {#if statsError}
          <p class="text-xs text-state-stale">Stats unavailable: {statsError}</p>
        {:else if !stats}
          <div class="h-20 animate-pulse rounded bg-surface-raised" aria-busy="true"></div>
        {:else if stats.count === 0}
          <p class="text-sm">No values at <code class="font-mono">{grain}</code> grain here.</p>
          <p class="mt-1 text-xs text-muted-foreground">
            The indicator is registered but nothing has been computed for this AOI.
          </p>
        {:else}
          <div class="flex flex-col gap-3">
            <div class="grid grid-cols-3 gap-3 text-center">
              {#each [["p05", stats.p05], ["median", stats.p50], ["p95", stats.p95]] as const as [label, value] (label)}
                <div>
                  <div class="font-mono text-[9.5px] tracking-[0.1em] text-muted-foreground uppercase">
                    {label}
                  </div>
                  <div class="font-mono text-base" data-numeric>
                    {value === null ? "—" : fmtNumber(value)}
                  </div>
                </div>
              {/each}
            </div>

            {#if p05Pos !== null && p95Pos !== null}
              <!-- The observed band drawn inside the declared safety range.
                   A p05–p95 hugging the left edge is the visual tell that the
                   catalog bound is a cap, not a realistic maximum. -->
              <div class="mt-1">
                <div class="relative h-2 rounded bg-surface-raised">
                  <div
                    class="absolute inset-y-0 rounded bg-accent/70"
                    style={`left:${p05Pos * 100}%;right:${(1 - p95Pos) * 100}%`}
                  ></div>
                  {#if p50Pos !== null}
                    <div
                      class="absolute inset-y-[-2px] w-px bg-interactive"
                      style={`left:${p50Pos * 100}%`}
                    ></div>
                  {/if}
                </div>
                <div class="mt-1 flex justify-between font-mono text-[10px] text-muted-foreground" data-numeric>
                  <span>{indicator.valid_min ?? "—"}</span>
                  <span>observed p05–p95 within declared range</span>
                  <span>{indicator.valid_max ?? "—"}</span>
                </div>
              </div>
            {/if}

            <p class="font-mono text-[11px] text-muted-foreground" data-numeric>
              {stats.count.toLocaleString()} values ·
              min {stats.min === null ? "—" : fmtNumber(stats.min)} ·
              max {stats.max === null ? "—" : fmtNumber(stats.max)}
            </p>
          </div>
        {/if}
      </Card>

      <Card title="Lineage">
        <p class="text-xs text-muted-foreground">
          Dependency graph, upstream datasets and freshness attach here once the
          asset graph merges. Until then this indicator's inputs are declared in
          the code registry and not yet queryable.
        </p>
      </Card>

      <Card title="Download">
        <div class="flex flex-wrap gap-2">
          {#each ["csv", "geojson", "parquet"] as const as fmt (fmt)}
            <a
              href={downloadUrl(indicator.slug, fmt)}
              class="inline-flex items-center gap-1.5 rounded border border-line bg-surface-raised
                     px-2.5 py-1.5 text-xs hover:border-accent/50"
            >
              <Download class="size-3.5" aria-hidden="true" />
              {fmt}
            </a>
          {/each}
        </div>
      </Card>
    </div>
  {/if}
</div>
