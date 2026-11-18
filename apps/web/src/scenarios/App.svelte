<script lang="ts">
  import TopBar from "../lib/TopBar.svelte";

  import { formatHash, parseHash } from "./derive";
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

  $effect(() => {
    void boot();
  });

  // URL → selection. `applyRoute` defers until the pilot's scenarios are
  // known, so a link opened cold still lands on its scenario.
  $effect(() => {
    applyRoute(parseHash(hash));
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

<TopBar current="scenarios" />

<main class="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
  <p class="text-sm text-muted-foreground">Scenario workspace</p>
</main>
