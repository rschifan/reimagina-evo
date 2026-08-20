<script lang="ts">
  import { Search, X } from "@lucide/svelte";

  import Chip from "./lib/components/ui/Chip.svelte";
  import { catalog } from "./store.svelte";
  import { EMPTY_FILTERS, type Filters } from "./derive";

  let { onchange }: { onchange: (f: Filters) => void } = $props();

  const toggle = (list: string[], value: string): string[] =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value];

  const facets = $derived(catalog.facets);
  const filters = $derived(catalog.filters);
  const active = $derived(
    filters.q !== "" || filters.families.length > 0 || filters.grains.length > 0,
  );
</script>

<div class="flex flex-col gap-3">
  <label class="relative block max-w-sm">
    <span class="sr-only">Search indicators</span>
    <Search
      class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
      aria-hidden="true"
    />
    <input
      type="search"
      placeholder="Search name or slug…"
      value={filters.q}
      oninput={(e) => onchange({ ...filters, q: e.currentTarget.value })}
      class="w-full rounded border border-line bg-surface-raised py-1.5 pr-3 pl-8 text-sm
             placeholder:text-muted-foreground focus:border-accent focus:outline-none"
    />
  </label>

  <div class="flex flex-wrap items-center gap-1.5">
    <span class="font-mono text-[10px] tracking-[0.1em] text-muted-foreground uppercase">
      Family
    </span>
    {#each Object.entries(facets.family).sort() as [name, count] (name)}
      <Chip
        pressed={filters.families.includes(name)}
        {count}
        onclick={() => onchange({ ...filters, families: toggle(filters.families, name) })}
      >
        {name}
      </Chip>
    {/each}
  </div>

  <div class="flex flex-wrap items-center gap-1.5">
    <span class="font-mono text-[10px] tracking-[0.1em] text-muted-foreground uppercase">
      Grain
    </span>
    {#each Object.entries(facets.grain).sort() as [name, count] (name)}
      <Chip
        pressed={filters.grains.includes(name)}
        {count}
        onclick={() => onchange({ ...filters, grains: toggle(filters.grains, name) })}
      >
        {name}
      </Chip>
    {/each}

    {#if active}
      <button
        type="button"
        onclick={() => onchange({ ...EMPTY_FILTERS })}
        class="ml-1 inline-flex items-center gap-1 rounded border border-line px-2 py-1
               text-xs text-muted-foreground hover:text-foreground"
      >
        <X class="size-3" aria-hidden="true" />
        Clear
      </button>
    {/if}
  </div>
</div>
