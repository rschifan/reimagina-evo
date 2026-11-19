<script lang="ts">
  import { ChevronRight } from "@lucide/svelte";

  import Badge from "../lib/components/ui/Badge.svelte";
  import FacetBar from "./FacetBar.svelte";
  import { catalog } from "./store.svelte";
  import { familyOf, filtersToHash, type Filters } from "./derive";
  import { unitLabel } from "../format";

  const rows = $derived(catalog.visible);
  const withValues = $derived(catalog.indicators.filter((i) => i.has_values).length);
  // Every indicator is a block indicator today; a column that never varies is
  // reading cost for nothing. It returns when a second grain does.
  const showGrain = $derived(Object.keys(catalog.facets.grain).length > 1);
  const TH =
    "px-4 py-2.5 font-mono text-[10px] tracking-[0.09em] font-medium text-muted-foreground uppercase";

  /** Navigate when the row is clicked anywhere, without turning the row into
   *  a second tab stop — the name anchor is already the keyboard target, and
   *  a focusable row would make every indicator two stops instead of one.
   *
   *  Two clicks are deliberately ignored: one that originated on the anchor
   *  itself (the browser is already navigating, and handling it again would
   *  double the history entry), and one that ends a text selection, because
   *  selecting a slug to copy it should not also navigate away. */
  function onRowClick(event: MouseEvent, slug: string) {
    if (event.defaultPrevented) return;
    if ((event.target as HTMLElement).closest("a")) return;
    if (window.getSelection()?.toString()) return;
    location.hash = `#/i/${encodeURIComponent(slug)}`;
  }

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
    {#if catalog.activeAoi}
      · {withValues} have values in {catalog.activeAoi.name}
    {/if}
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
        {catalog.total} exist in the catalog — widen the search or clear a filter.
      </p>
    </div>
  {:else}
<!--
      `relative` is load-bearing, not decoration.

      ⚠ An `overflow` container only clips absolutely-positioned descendants
      when it is itself a containing block. This wrapper was `static`, and
      Tailwind's `sr-only` is `position: absolute` — so the "Open" label in
      the last header cell escaped the scroll box, laid out at the table's
      full width, and made the *document* scroll sideways.
    -->
    <div class="relative overflow-x-auto rounded-lg border border-line">
      <table class="w-full border-collapse text-sm">
        <thead>
          <tr class="border-b border-line text-left">
            <th scope="col" class={TH}>Indicator</th>
            <th scope="col" class={`${TH} max-md:hidden`}>Family</th>
            <th scope="col" class={`${TH} max-md:hidden`}>Kind</th>
            {#if showGrain}
              <th scope="col" class={`${TH} max-md:hidden`}>Grain</th>
            {/if}
            <th scope="col" class={`${TH} max-md:hidden`}>Unit</th>
            <th scope="col" class={`${TH} text-right max-lg:hidden`}>Declared range</th>
            <th scope="col" class="w-8"><span class="sr-only">Open</span></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.slug)}
            <tr
              class="cursor-pointer border-b border-line/60 align-top last:border-0 hover:bg-accent/5
                     has-[a:focus-visible]:bg-accent/5"
              onclick={(e) => onRowClick(e, row.slug)}
            >
              <td class="px-4 py-2.5">
                <a
                  href={`#/i/${encodeURIComponent(row.slug)}`}
                  class="block rounded focus-visible:outline-2"
                >
                  <span class="text-foreground">{row.name}</span>
                  {#if row.has_values === false}
                    <span class="ml-1.5 align-middle">
                      <Badge tone="stale">no values here</Badge>
                    </span>
                  {/if}
                </a>
                {#if row.description}
                  <p class="mt-0.5 line-clamp-1 max-w-prose text-[12px] text-muted-foreground" title={row.description}>
                    {row.description}
                  </p>
                {/if}
                <span class="mt-0.5 block font-mono text-[10.5px] text-muted-foreground">
                  {row.slug}
                </span>
                <!-- The columns hidden below md, folded into one line. -->
                <span class="mt-0.5 block font-mono text-[10.5px] text-muted-foreground md:hidden">
                  {familyOf(row)} · {row.computation_kind} · {unitLabel(row.unit)}
                </span>
              </td>
              <td class="px-4 py-2.5 max-md:hidden">
                <Badge>{familyOf(row)}</Badge>
              </td>
              <td class="px-4 py-2.5 font-mono text-[11px] text-muted-foreground max-md:hidden">
                {row.computation_kind}
              </td>
              {#if showGrain}
                <td class="px-4 py-2.5 max-md:hidden">
                  <span class="flex gap-1">
                    {#each row.applies_to as grain (grain)}
                      <Badge tone="accent">{grain}</Badge>
                    {/each}
                  </span>
                </td>
              {/if}
              <td class="px-4 py-2.5 font-mono text-[11px] whitespace-nowrap text-muted-foreground max-md:hidden">
                {unitLabel(row.unit)}
              </td>
              <td class="px-4 py-2.5 text-right font-mono text-[11px] whitespace-nowrap text-muted-foreground max-lg:hidden" data-numeric>
                {row.valid_min ?? "—"} – {row.valid_max ?? "—"}
              </td>
              <td class="px-2 py-2.5 text-right">
                <ChevronRight
                  class="inline size-4 text-muted-foreground"
                  aria-hidden="true"
                />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
