<script lang="ts">
  import { onMount, untrack } from "svelte";

  import { setActiveAoiId } from "../aoi";
  import TopBar from "../lib/TopBar.svelte";

  import { formatHash, parseHash } from "./derive";
  import ScenarioMap from "./ScenarioMap.svelte";
  import ScenarioTable from "./ScenarioTable.svelte";
  import { applyRoute, boot, ws } from "./store.svelte";

  /**
   * The workspace shell: the route in the URL hash, and the three parts the
   * page is made of. The store owns every request; this file only keeps the
   * hash and the selection in step, in both directions.
   */
  let hash = $state(location.hash);
  $effect(() => {
    const onHash = () => (hash = location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  });

  // Once, not as an effect: `boot` reads store state synchronously, and an
  // effect would re-run it — a second boot — every time that state changed.
  onMount(() => void boot());

  // URL → selection, tracking the hash and nothing else. `applyRoute` reads
  // the store; untracked, so a store change does not re-apply an old route.
  // It defers until the pilot's scenarios are known, so a link opened cold
  // still lands on its scenario.
  $effect(() => {
    const route = parseHash(hash);
    untrack(() => applyRoute(route));
  });

  // Selection → URL, without a history entry per click — and only once the
  // route the page opened with has been applied, or the empty selection of a
  // page still loading would overwrite the link it was opened from.
  $effect(() => {
    if (!ws.routeReady) return;
    const next = formatHash(ws.selectedId, ws.slug);
    if (next !== location.hash && !(next === "#/" && location.hash === "")) {
      history.replaceState(null, "", next);
      hash = next;
    }
  });
</script>

<!-- The page is the viewport: the map takes the height the rest leaves, the
     table scrolls inside itself, and the page never scrolls. -->
<div class="flex h-dvh flex-col overflow-hidden">
<TopBar current="scenarios">
  {#snippet actions()}
    <label class="flex items-center gap-2 text-xs text-muted-foreground">
      Pilot
      <select
        class="rounded border border-line bg-surface px-2 py-1 text-xs text-foreground"
        value={ws.aoiId ?? ""}
        onchange={(e) => setActiveAoiId(e.currentTarget.value || null)}
      >
        {#each ws.aois as aoi (aoi.aoi_id)}
          <option value={aoi.aoi_id}>{aoi.name}</option>
        {/each}
      </select>
    </label>
  {/snippet}
</TopBar>

<main class="mx-auto flex min-h-0 w-full max-w-[1600px] flex-1 flex-col gap-3 p-4">
  <!-- Rules are drawn, edited and deleted on the map; scenarios are picked,
       recomputed and deleted in the table. -->
  <ScenarioMap />
  <ScenarioTable />
</main>
</div>
