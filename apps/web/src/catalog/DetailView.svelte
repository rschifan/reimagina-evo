<script lang="ts">
  import { ArrowLeft, Download, Map as MapIcon } from "@lucide/svelte";

  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import DatasetPanel from "./DatasetPanel.svelte";
  import LineageGraph from "./LineageGraph.svelte";
  import {
    DOWNLOAD_ROW_CAP,
    downloadUrl,
    getDependencies,
    getLineage,
    type IndicatorDependencies,
    type IndicatorLineage,
    type IndicatorStats,
  } from "./api";
  import { boundsFraction, capNotes, familyOf } from "./derive";
  import { isAbortError } from "../http";
  import { fmtNumber, unitLabel } from "../format";
  import { catalog, statsFor } from "./store.svelte";

  let { slug }: { slug: string } = $props();

  const indicator = $derived(catalog.bySlug(slug));
  // The finest declared grain is what the map draws and what the stats
  // endpoint is asked for, so the two agree by construction.
  const grain = $derived(indicator?.applies_to[0]);
  // The fetches below key on this string, not on `indicator`: every poll
  // replaces the row object, and keying on it re-ran all three fetches and
  // blanked the cards to skeletons each time.
  const known = $derived(indicator ? indicator.slug : null);

  let stats = $state<IndicatorStats | null>(null);
  let statsError = $state<string | null>(null);
  let deps = $state<IndicatorDependencies | null>(null);
  let depsError = $state<string | null>(null);
  let lineage = $state<IndicatorLineage | null>(null);
  let lineageError = $state<string | null>(null);

  $effect(() => {
    if (!known || !grain) return;
    let live = true;
    // Cleared first: a lineage link swaps the slug in place, and the previous
    // indicator's numbers stayed on screen until the new ones arrived.
    stats = null;
    statsError = null;
    statsFor(known, grain)
      .then((s) => live && (stats = s))
      .catch((e) => live && (statsError = e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  });

  $effect(() => {
    if (!known) return;
    const ctrl = new AbortController();
    deps = null;
    depsError = null;
    getDependencies(known, { signal: ctrl.signal })
      .then((d) => (deps = d))
      .catch((e) => {
        if (!isAbortError(e)) depsError = e instanceof Error ? e.message : String(e);
      });
    return () => ctrl.abort();
  });

  $effect(() => {
    if (!known) return;
    const ctrl = new AbortController();
    lineage = null;
    lineageError = null;
    getLineage(known, { signal: ctrl.signal })
      .then((l) => (lineage = l))
      .catch((e) => {
        if (!isAbortError(e)) lineageError = e instanceof Error ? e.message : String(e);
      });
    return () => ctrl.abort();
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
  const notes = $derived(
    stats && indicator ? capNotes(stats, indicator.valid_min, indicator.valid_max) : [],
  );
  const aoiName = $derived(catalog.activeAoi?.name ?? null);
  // Nothing to open or export where the indicator holds no values.
  const exportable = $derived(indicator?.has_values !== false && (stats?.count ?? 1) > 0);
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
        The catalog has no such slug. It may have been renamed or removed.
      </p>
    </div>
  {:else}
    <header class="flex flex-col gap-3">
      <div class="flex flex-wrap items-start gap-x-4 gap-y-2">
        <div>
          <h1 class="text-xl font-semibold tracking-tight">{indicator.name}</h1>
          <p class="mt-1 font-mono text-[11px] text-muted-foreground">{indicator.slug}</p>
        </div>
        <div class="flex flex-wrap gap-1.5 pt-1">
          <Badge>{familyOf(indicator)}</Badge>
          <Badge>{indicator.computation_kind}</Badge>
          {#each indicator.applies_to as g (g)}
            <Badge tone="accent">{g}</Badge>
          {/each}
        </div>
      </div>

      {#if indicator.description}
        <p class="max-w-3xl text-[13px] leading-relaxed text-muted-foreground">
          {indicator.description}
        </p>
      {/if}

      {#if indicator.has_values === false}
        <p class="w-fit rounded border border-state-stale/40 px-2.5 py-1.5 text-xs text-state-stale">
          No values in {aoiName ?? "this AOI"} — nothing has computed it here yet.
        </p>
      {/if}

      {#if exportable}
        <div class="flex flex-wrap items-center gap-2">
          <a
            href={`/index.html?indicator=${encodeURIComponent(indicator.slug)}`}
            class="inline-flex items-center gap-1.5 rounded border border-accent/40 bg-accent/10
                   px-2.5 py-1.5 text-xs text-accent hover:bg-accent/20"
          >
            <MapIcon class="size-3.5" aria-hidden="true" />
            Open on map
          </a>
          {#each ["csv", "geojson", "parquet"] as const as fmt (fmt)}
            <a
              href={downloadUrl(indicator.slug, fmt, { aoiId: catalog.activeAoiId, grain: grain ?? null })}
              class="inline-flex items-center gap-1.5 rounded border border-line bg-surface-raised
                     px-2.5 py-1.5 text-xs hover:border-accent/50"
            >
              <Download class="size-3.5" aria-hidden="true" />
              {fmt}
            </a>
          {/each}
          <span class="font-mono text-[10.5px] text-muted-foreground" data-numeric>
            {#if stats && stats.count > DOWNLOAD_ROW_CAP}
              downloads stop at {DOWNLOAD_ROW_CAP.toLocaleString()} of {stats.count.toLocaleString()} rows
            {:else if stats}
              {stats.count.toLocaleString()} rows{aoiName ? ` · ${aoiName} only` : ""}
            {/if}
          </span>
        </div>
      {/if}
    </header>

    <div class="grid items-start gap-4 lg:grid-cols-2">
      <Card title="Definition">
        <dl class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[13px]">
          <dt class="text-muted-foreground">Unit</dt>
          <dd class="font-mono text-[11.5px]">{unitLabel(indicator.unit)}</dd>
          <dt class="text-muted-foreground">Declared range</dt>
          <dd class="font-mono text-[11.5px]" data-numeric>
            {indicator.valid_min ?? "—"} – {indicator.valid_max ?? "—"}
          </dd>
          <dt class="text-muted-foreground">Kind</dt>
          <dd class="font-mono text-[11.5px]">{indicator.computation_kind}</dd>
          <dt class="text-muted-foreground">Aggregation</dt>
          <dd class="font-mono text-[11.5px]">{indicator.aggregation_method.replaceAll("_", " ")}</dd>
          <dt class="text-muted-foreground">Grain</dt>
          <dd class="font-mono text-[11.5px]">
            {grain}{indicator.applies_to.length > 1
              ? ` (also ${indicator.applies_to.slice(1).join(", ")})`
              : ""}
          </dd>
        </dl>
      </Card>

      <Card title={`Observed values · ${aoiName ?? "all AOIs"}`}>
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
                  <span>p05–p95 on the declared range</span>
                  <span>{indicator.valid_max ?? "—"}</span>
                </div>
              </div>
            {/if}

            {#each notes as note (note)}
              <p class="rounded border border-state-stale/40 px-2.5 py-1.5 text-xs text-state-stale" role="note">
                {note}
                {#if indicator.description}The description above says what the bound stands for.{/if}
              </p>
            {/each}

            <p class="font-mono text-[11px] text-muted-foreground" data-numeric>
              {stats.count.toLocaleString()} values ·
              min {stats.min === null ? "—" : fmtNumber(stats.min)} ·
              max {stats.max === null ? "—" : fmtNumber(stats.max)}
            </p>
          </div>
        {/if}
      </Card>

      <Card title="Lineage">
        {#if depsError}
          <p class="text-xs text-state-stale">Dependencies unavailable: {depsError}</p>
        {:else if !deps}
          <div class="h-16 animate-pulse rounded bg-surface-raised" aria-busy="true"></div>
        {:else}
          <LineageGraph
            slug={indicator.slug}
            name={indicator.name}
            upstream={deps.upstream}
            downstream={deps.downstream}
          />
        {/if}
      </Card>

      <Card title="Datasets upstream">
        {#if lineageError}
          <p class="text-xs text-state-stale">Lineage unavailable: {lineageError}</p>
        {:else if !lineage}
          <div class="h-16 animate-pulse rounded bg-surface-raised" aria-busy="true"></div>
        {:else}
          <DatasetPanel upstream={lineage.upstream} />
        {/if}
      </Card>

    </div>
  {/if}
</div>
