<script lang="ts">
  import { statusFor, statusLabel } from "./derive";
  import { confirmRemoveScenario, create, headline, recomputeSelected, select, ws } from "./store.svelte";

  /**
   * Which scenario is on the map, what it changes, and whether that is
   * current. The rules themselves are edited on the map; this bar is where a
   * scenario is picked, started and deleted.
   */
  let newName = $state("");

  const head = $derived(headline(ws.selectedId));
  const row = $derived(ws.scenarios.find((s) => s.id === ws.selectedId) ?? null);
  /** The selected scenario's real status, as its table row shows it. */
  const status = $derived(row ? statusFor(row, ws.following[row.id] ?? null) : null);
  /** A recompute failed, was never queued, or is behind the rules: offer one. */
  const canRecompute = $derived(
    status === "stale" || status === "failed" || status === "partial" || status === "not-computed",
  );
  const running = $derived(
    ws.busy || (ws.selectedId !== null && ws.following[ws.selectedId] !== undefined),
  );

  const number = (n: number | null): string =>
    n === null ? "—" : Math.round(n).toLocaleString("en-US");
  const change = (h: { base: number | null; now: number | null }): string => {
    if (h.base === null || h.now === null) return "";
    const d = Math.round(h.now - h.base);
    return `${d >= 0 ? "+" : "−"}${Math.abs(d).toLocaleString("en-US")}`;
  };

  async function submitNew(event: SubmitEvent) {
    event.preventDefault();
    await create(newName);
    if (ws.message?.tone !== "bad") newName = "";
  }

  const control = "rounded border border-line bg-surface px-2 py-1 text-xs text-foreground";
</script>

<section
  aria-label="Scenario"
  class="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-line bg-surface px-3 py-2 text-xs"
>
  <label class="flex items-center gap-2 text-muted-foreground">
    Scenario
    <select
      class={control}
      value={ws.selectedId ?? ""}
      disabled={ws.busy}
      onchange={(e) => select(e.currentTarget.value || null)}
    >
      <option value="">Baseline city</option>
      {#each ws.scenarios as scenario (scenario.id)}
        <option value={scenario.id}>{scenario.name}</option>
      {/each}
    </select>
  </label>

  <span class="tabular-nums">
    <span class="text-muted-foreground">Homes</span>
    {#if ws.selectedId === null}
      {number(head.homes.now)}
    {:else}
      {number(head.homes.base)} → {number(head.homes.now)}
      <span class="text-accent">{change(head.homes)}</span>
    {/if}
  </span>
  <span class="tabular-nums">
    <span class="text-muted-foreground">People</span>
    {#if ws.selectedId === null}
      {number(head.people.now)}
    {:else}
      {number(head.people.base)} → {number(head.people.now)}
      <span class="text-accent">{change(head.people)}</span>
    {/if}
  </span>

  <span class="text-muted-foreground" role="status">
    {#if running}
      Recomputing…
    {:else if ws.selectedId === null}
      Draw an area on the map to start a scenario.
    {:else if ws.rules.length === 0}
      No rules yet: draw an area on the map.
    {:else if status}
      {statusLabel(status)}
    {/if}
  </span>
  {#if canRecompute && !running}
    <button
      type="button"
      class="rounded border border-accent/40 bg-accent/10 px-2.5 py-1 text-accent"
      onclick={() => void recomputeSelected()}
    >
      Recompute
    </button>
  {/if}

  <form class="ml-auto flex items-center gap-2" onsubmit={submitNew}>
    <input
      class="w-40 {control}"
      placeholder="New scenario name"
      aria-label="New scenario name"
      bind:value={newName}
      disabled={!ws.projectId || ws.busy}
    />
    <button
      type="submit"
      class="rounded border border-line px-2.5 py-1 hover:text-foreground disabled:opacity-50"
      disabled={!ws.projectId || !newName.trim() || ws.busy}
    >
      New scenario
    </button>
  </form>
  {#if ws.selectedId !== null}
    <button
      type="button"
      class="rounded px-2 py-1 text-state-error disabled:opacity-50"
      disabled={ws.busy}
      onclick={() => ws.selectedId && void confirmRemoveScenario(ws.selectedId)}
    >
      Delete scenario
    </button>
  {/if}
</section>
