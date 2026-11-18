<script lang="ts">
  import { onMount, untrack } from "svelte";

  import { setActiveAoiId } from "../aoi";
  import TopBar from "../lib/TopBar.svelte";

  import { formatHash, parseHash } from "./derive";
  import ScenarioMap from "./ScenarioMap.svelte";
  import ScenarioTable from "./ScenarioTable.svelte";
  import { applyRoute, boot, create, ws } from "./store.svelte";

  /**
   * The workspace shell: the route in the URL hash, and the three parts the
   * page is made of. The store owns every request; this file only keeps the
   * hash and the selection in step, in both directions.
   */
  let hash = $state(location.hash);
  let newName = $state("");

  async function submitNew(event: SubmitEvent) {
    event.preventDefault();
    await create(newName);
    if (ws.message?.tone !== "bad") newName = "";
  }

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

  // Selection → URL, without a history entry per click.
  $effect(() => {
    const next = formatHash(ws.selectedId, ws.slug);
    if (next !== location.hash && !(next === "#/" && location.hash === "")) {
      history.replaceState(null, "", next);
      hash = next;
    }
  });
</script>

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
    <form class="flex items-center gap-2" onsubmit={submitNew}>
      <input
        class="w-44 rounded border border-line bg-surface px-2 py-1 text-xs"
        placeholder="New scenario name"
        aria-label="New scenario name"
        bind:value={newName}
        disabled={!ws.projectId}
      />
      <button
        type="submit"
        class="rounded border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs text-accent disabled:opacity-50"
        disabled={!ws.projectId || !newName.trim()}
        title={ws.projectId ? "Create a scenario on this pilot" : "Pick a pilot first"}
      >
        New scenario
      </button>
    </form>
  {/snippet}
</TopBar>

<main class="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
  <!-- Always in the DOM: a live region inserted with its text is not
       announced by every screen reader; one whose text changes is. -->
  <p
    role="status"
    class={ws.message?.tone === "bad" ? "text-sm text-state-error" : "text-sm text-muted-foreground"}
  >
    {ws.message?.text ?? ""}
  </p>
  <ScenarioTable />
  <ScenarioMap />
</main>
