<script lang="ts">
  import { ChevronRight } from "@lucide/svelte";

  import Badge from "./lib/components/ui/Badge.svelte";
  import FacetBar from "./FacetBar.svelte";
  import { catalog } from "./store.svelte";
  import { familyOf, filtersToHash, type Filters } from "./derive";

  const rows = $derived(catalog.visible);

  function onFilters(f: Filters) {
    catalog.setFilters(f);
    // Replace rather than push: typing in a search box should not bury the
    // back button under one entry per keystroke.
    history.replaceState(null, "", filtersToHash(f));
  }
</script>

<div class="flex flex-col gap-5">
  <FacetBar onchange={onFilters} />

  <p class="font-mono text-[11px] text-muted-foreground" data-numeric>
    {rows.length} of {catalog.total} indicators
    {#if catalog.activeAoiId}· scoped to the active AOI{/if}
  </p>

  {#if catalog.loading}
    <div class="flex flex-col gap-2" aria-busy="true" aria-label="Loading indicators">
      {#each Array.from({ length: 6 }) as _, i (i)}
        <div class="h-12 animate-pulse rounded border border-line bg-surface"></div>
      {/each}
    </div>
  {:else if rows.length === 0}
    <div class="rounded-lg border border-dashed border-line p-8 text-center">
      <p class="text-sm">No indicator matches these filters.</p>
      <p class="mt-1 text-xs text-muted-foreground">
        {catalog.total} exist in the catalog — widen the search or clear a facet.
      </p>
    </div>
  {:else}
    <div class="overflow-x-auto rounded-lg border border-line">
      <table class="w-full border-collapse text-sm">
        <thead>
          <tr class="border-b border-line text-left">
            <th scope="col" class="px-4 py-2.5 font-mono text-[10px] tracking-[0.09em] font-medium text-muted-foreground uppercase">Indicator</th>
            <th scope="col" class="px-4 py-2.5 font-mono text-[10px] tracking-[0.09em] font-medium text-muted-foreground uppercase">Family</th>
            <th scope="col" class="px-4 py-2.5 font-mono text-[10px] tracking-[0.09em] font-medium text-muted-foreground uppercase">Grain</th>
            <th scope="col" class="px-4 py-2.5 font-mono text-[10px] tracking-[0.09em] font-medium text-muted-foreground uppercase">Unit</th>
            <th scope="col" class="px-4 py-2.5 text-right font-mono text-[10px] tracking-[0.09em] font-medium text-muted-foreground uppercase">Declared range</th>
            <th scope="col" class="w-8"><span class="sr-only">Open</span></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.slug)}
            <tr class="border-b border-line/60 last:border-0 hover:bg-accent/5">
              <td class="px-4 py-2.5">
                <a
                  href={`#/i/${encodeURIComponent(row.slug)}`}
                  class="block rounded focus-visible:outline-2"
                >
                  <span class="text-foreground">{row.name}</span>
                  <span class="mt-0.5 block font-mono text-[10.5px] text-muted-foreground">
                    {row.slug}
                  </span>
                </a>
              </td>
              <td class="px-4 py-2.5">
                <Badge>{familyOf(row.slug)}</Badge>
              </td>
              <td class="px-4 py-2.5">
                <span class="flex gap-1">
                  {#each row.applies_to as grain (grain)}
                    <Badge tone="accent">{grain}</Badge>
                  {/each}
                </span>
              </td>
              <td class="px-4 py-2.5 font-mono text-[11px] text-muted-foreground">
                {row.unit ?? "—"}
              </td>
              <td class="px-4 py-2.5 text-right font-mono text-[11px] text-muted-foreground" data-numeric>
                {row.valid_min ?? "—"} – {row.valid_max ?? "—"}
              </td>
              <td class="px-2">
                <ChevronRight class="size-4 text-muted-foreground" aria-hidden="true" />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
