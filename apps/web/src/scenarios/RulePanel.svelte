<script lang="ts">
  import Card from "../lib/components/ui/Card.svelte";
  import { CLIENT_PRECHECK_NOTE, describeGroup, maxLevelsFor } from "../scenario";

  import {
    applyRule,
    compare,
    recompute,
    removeRuleGroup,
    removeScenario,
    ws,
  } from "./store.svelte";

  /**
   * The rules of the selected scenario: write one over the drawn area, remove
   * one, recompute, compare, delete. Every request and every refusal is the
   * store's; this panel holds only what the planner is typing.
   */
  let levels = $state(6);
  let unitSize = $state(60);
  let peoplePerHousehold = $state(3.6);
  let busy = $state(false);

  const scenario = $derived(ws.scenarios.find((s) => s.id === ws.selectedId) ?? null);

  /** The storeys input's own `max`: the smaller of the floors ceiling and
   *  what the density ceiling allows at this unit size, once known. The
   *  API's 422 carries the same number as `allowed_levels`. */
  const maxLevels = $derived.by(() => {
    const caps: number[] = [];
    if (ws.floorsValidMax !== null) caps.push(Math.floor(ws.floorsValidMax));
    if (ws.densityValidMax !== null && Number.isFinite(unitSize) && unitSize > 0) {
      caps.push(maxLevelsFor(unitSize, ws.densityValidMax));
    }
    return caps.length ? Math.min(...caps) : undefined;
  });

  async function run(action: () => Promise<void>) {
    busy = true;
    try {
      await action();
    } finally {
      busy = false;
    }
  }

  function confirmDelete() {
    if (!scenario) return;
    if (!window.confirm(`Delete ${scenario.name}, its overrides and its results?`)) return;
    void run(removeScenario);
  }

  const input =
    "w-full rounded border border-line bg-surface px-2 py-1 text-sm tabular-nums";
  const button =
    "rounded border border-line px-2.5 py-1 text-xs hover:text-foreground disabled:opacity-50";
</script>

<Card title={scenario ? `Rules · ${scenario.name}` : "Rules"}>
  {#if !scenario}
    <p class="text-sm text-muted-foreground">
      Pick a scenario in the table, or create one, to write rules on it. A rule is an
      area drawn on the map and three numbers; every block whose centre falls inside it
      takes them.
    </p>
  {:else}
    <form
      class="flex flex-col gap-3"
      onsubmit={(e) => {
        e.preventDefault();
        void run(() => applyRule(ws.drawn, { levels, unitSize, peoplePerHousehold }));
      }}
    >
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        Storeys
        <input class={input} type="number" min="1" step="1" max={maxLevels} bind:value={levels} />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        m² per dwelling
        <input class={input} type="number" min="1" step="5" bind:value={unitSize} />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        People per household
        <input class={input} type="number" min="0.1" step="0.1" bind:value={peoplePerHousehold} />
      </label>
      <p class="text-[11px] leading-snug text-muted-foreground">{CLIENT_PRECHECK_NOTE}</p>
      <button
        type="submit"
        class="rounded border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs text-accent disabled:opacity-50"
        disabled={busy || ws.drawn === null}
        title={ws.drawn === null
          ? "Draw an area on the map first"
          : "Write this rule over the drawn area"}
      >
        Apply to drawn area
      </button>
    </form>

    <h3 class="mt-4 mb-1 font-mono text-[10px] tracking-[0.11em] text-muted-foreground uppercase">
      Rules
    </h3>
    {#if ws.rules.length === 0}
      <p class="text-sm text-muted-foreground">No rules yet.</p>
    {:else}
      <ul class="flex flex-col gap-1">
        {#each ws.rules as group, index (group.editIds[0])}
          {@const label = describeGroup(group, index + 1)}
          <li class="flex items-start justify-between gap-2 text-xs">
            <span class={group.complete ? "" : "text-state-error"}>{label}</span>
            <button
              type="button"
              class="px-1 text-muted-foreground hover:text-state-error disabled:opacity-50"
              aria-label={`Remove area ${index + 1} and all ${group.editIds.length} of its edits`}
              title={`Remove area ${index + 1} and all ${group.editIds.length} of its edits`}
              disabled={busy}
              onclick={() => void run(() => removeRuleGroup(group.editIds))}
            >
              ×
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="mt-4 flex flex-wrap gap-2">
      <button type="button" class={button} disabled={busy} onclick={() => void run(recompute)}>
        Recompute
      </button>
      <button
        type="button"
        class={button}
        disabled={busy || !ws.slug}
        onclick={() => void run(compare)}
      >
        Compare to baseline
      </button>
      <button
        type="button"
        class="{button} ml-auto text-state-error"
        disabled={busy}
        onclick={confirmDelete}
      >
        Delete
      </button>
    </div>

    {#if ws.comparison}
      <div class="mt-3 rounded border border-line p-2 text-xs">
        <div>{ws.comparison.counts}</div>
        <div class="text-muted-foreground">{ws.comparison.denominator}</div>
        <div>{ws.comparison.shift}</div>
      </div>
    {/if}
  {/if}
</Card>
