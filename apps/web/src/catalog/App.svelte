<script lang="ts">
  import { RefreshCw } from "@lucide/svelte";

  import TopBar from "../lib/TopBar.svelte";

  import DetailView from "./DetailView.svelte";
  import ImpactCheck from "./ImpactCheck.svelte";
  import IndexView from "./IndexView.svelte";
  import { boot, catalog, startPolling } from "./store.svelte";
  import { filtersFromHash, slugFromHash } from "./derive";

  // A ~20-line matcher rather than a router dependency: two routes, and the
  // rest of this codebase consistently declines a package for what a regex
  // does. `#/` is the index, `#/i/<slug>` the detail.
  let hash = $state(location.hash || "#/");
  const slug = $derived(slugFromHash(hash));

  $effect(() => {
    const onHash = () => {
      hash = location.hash || "#/";
      if (!slugFromHash(hash)) catalog.setFilters(filtersFromHash(hash));
    };
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  });

  $effect(() => {
    catalog.setFilters(filtersFromHash(location.hash || "#/"));
    void boot();
    // Teardown stops the interval AND aborts any request in flight, so a
    // hot reload cannot leave a second loop running behind the first.
    return startPolling();
  });
</script>

<TopBar current="catalog">
  {#snippet actions()}
    {#if catalog.aois.length > 1}
      <label class="flex items-center gap-2 text-xs text-muted-foreground">
        AOI
        <select
          class="rounded border border-line bg-surface-raised px-2 py-1 text-xs text-foreground"
          value={catalog.activeAoiId ?? ""}
          onchange={(e) => void catalog.setAoi(e.currentTarget.value || null)}
        >
          {#each catalog.aois as aoi (aoi.aoi_id)}
            <option value={aoi.aoi_id}>{aoi.name}</option>
          {/each}
        </select>
      </label>
    {/if}

    {#if catalog.error}
      <span class="font-mono text-[10px] text-state-stale" role="status">
        refresh failed — showing last known
      </span>
    {:else if catalog.lastLoadedAt}
      <span class="font-mono text-[10px] text-muted-foreground" role="status">
        updated {catalog.lastLoadedAt.toLocaleTimeString()}
      </span>
    {/if}
    <RefreshCw
      class={`size-3.5 text-muted-foreground ${catalog.refreshing ? "animate-spin" : ""}`}
      aria-hidden="true"
    />
  {/snippet}
</TopBar>

<div class="mx-auto flex min-h-screen max-w-6xl flex-col gap-6 px-6 py-8">
  <main class="flex-1">
    {#if slug}
      <DetailView {slug} />
    {:else}
      <IndexView />
      <div class="mt-6">
        <ImpactCheck />
      </div>
    {/if}
  </main>
</div>
