<script lang="ts">
  import { Search, X } from "@lucide/svelte";

  import Chip from "../lib/components/ui/Chip.svelte";
  import { catalog } from "./store.svelte";
  import { EMPTY_FILTERS, FACETS, isFiltered, type FacetKey, type Filters } from "./derive";

  let { onchange }: { onchange: (f: Filters) => void } = $props();

  const LABEL: Record<FacetKey, string> = {
    family: "Family",
    kind: "Kind",
    grain: "Grain",
    values: "In this AOI",
  };

  const toggle = (list: string[], value: string): string[] =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value];

  const facets = $derived(catalog.facets);
  const filters = $derived(catalog.filters);

  // A facet with one option filters nothing — "grain: block 7" on a catalog
  // where every indicator is a block indicator. It comes back when a second
  // option appears, or while a link has it selected.
  const shown = $derived(
    FACETS.filter(
      (k) => Object.keys(facets[k]).length > 1 || filters.facets[k].length > 0,
    ),
  );

  function pick(key: FacetKey, value: string) {
    onchange({
      ...filters,
      facets: { ...filters.facets, [key]: toggle(filters.facets[key], value) },
    });
  }
</script>

<div class="flex flex-col gap-3">
  <div class="flex flex-wrap items-center gap-2">
    <label class="relative block w-full max-w-sm">
      <span class="sr-only">Search indicators</span>
      <Search
        class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <input
        type="search"
        placeholder="Search name, slug or description…"
        value={filters.q}
        oninput={(e) => onchange({ ...filters, q: e.currentTarget.value })}
        class="w-full rounded border border-line bg-surface-raised py-1.5 pr-3 pl-8 text-sm
               placeholder:text-muted-foreground focus:border-accent focus:outline-none"
      />
    </label>

    {#if isFiltered(filters)}
      <button
        type="button"
        onclick={() => onchange({ ...EMPTY_FILTERS })}
        class="inline-flex items-center gap-1 rounded border border-line px-2 py-1
               text-xs text-muted-foreground hover:text-foreground"
      >
        <X class="size-3" aria-hidden="true" />
        Clear filters
      </button>
    {/if}
  </div>

  {#each shown as key (key)}
    <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label={LABEL[key]}>
      <span class="w-full font-mono md:w-24 text-[10px] tracking-[0.1em] text-muted-foreground uppercase">
        {LABEL[key]}
      </span>
      {#each Object.entries(facets[key]).sort() as [name, count] (name)}
        <Chip pressed={filters.facets[key].includes(name)} {count} onclick={() => pick(key, name)}>
          {name}
        </Chip>
      {/each}
    </div>
  {/each}
</div>
