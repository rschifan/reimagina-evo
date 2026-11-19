<script lang="ts">
  import type { RuleValues } from "./store.svelte";

  /**
   * The numbers of one rule, next to its shape on the map.
   *
   * `new`: a shape just drawn — Apply writes it (and, on the baseline city,
   * creates the scenario first). `rule`: a saved rule — Save writes its new
   * numbers and, if it was dragged, its new area; Delete removes it. Either
   * way the store recomputes and the map shows the change; this component
   * holds only what is being typed.
   *
   * Coverage is typed in percent, as planners say it, and handed on as a
   * share. `novalidate`: the checks below name the field and the range; the
   * browser's own would pre-empt them with a tooltip that says neither.
   */
  interface Props {
    mode: "new" | "rule";
    values: RuleValues;
    needsScenarioName: boolean;
    defaultName: string;
    overriddenBy: number | null;
    busy: boolean;
    error: { field: string | null; text: string } | null;
    draggable: boolean;
    onapply: (values: RuleValues, scenarioName?: string) => void;
    onsave: (values: RuleValues) => void;
    ondelete: () => void;
    oncancel: () => void;
  }

  let {
    mode,
    values,
    needsScenarioName,
    defaultName,
    overriddenBy,
    busy,
    error,
    draggable,
    onapply,
    onsave,
    ondelete,
    oncancel,
  }: Props = $props();

  // Seeded once from the props: the box is remounted for every shape, and
  // a prop change mid-typing must not overwrite what is being typed.
  const initial = (() => values)();
  let levels = $state(initial.levels);
  let unitSize = $state(initial.unitSize);
  let people = $state(initial.peoplePerHousehold);
  let coveragePct = $state(Math.round(initial.lotCoverage * 100));
  let name = $state((() => defaultName)());
  let localError = $state<string | null>(null);

  function read(): RuleValues | null {
    if (!Number.isFinite(coveragePct) || coveragePct < 1 || coveragePct > 100) {
      localError = "Lot coverage must be between 1 and 100 %.";
      return null;
    }
    localError = null;
    return {
      levels,
      unitSize,
      peoplePerHousehold: people,
      lotCoverage: coveragePct / 100,
    };
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const next = read();
    if (!next) return;
    if (mode === "new") onapply(next, needsScenarioName ? name : undefined);
    else onsave(next);
  }

  const input = "w-20 rounded border border-line bg-surface px-1.5 py-0.5 text-right text-xs tabular-nums";
  const row = "flex items-center justify-between gap-3 text-xs text-muted-foreground";
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<form
  class="w-56 rounded-lg border border-line bg-surface p-3 text-xs shadow-lg"
  novalidate
  aria-label={mode === "new" ? "New rule" : "Edit rule"}
  onsubmit={submit}
  onkeydown={(e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      oncancel();
    }
  }}
>
  <h3 class="mb-2 text-sm text-foreground">{mode === "new" ? "New rule" : "Rule"}</h3>
  {#if needsScenarioName}
    <label class="mb-2 flex flex-col gap-1 text-muted-foreground">
      New scenario
      <input class="rounded border border-line bg-surface px-1.5 py-0.5 text-xs" aria-label="Scenario name" bind:value={name} />
    </label>
  {/if}
  <div class="flex flex-col gap-1.5">
    <label class={row}>Storeys <input class={input} type="number" min="1" step="1" aria-label="Storeys" bind:value={levels} /></label>
    <label class={row}>m² per home <input class={input} type="number" min="1" step="1" aria-label="m² per home" bind:value={unitSize} /></label>
    <label class={row}>People per home <input class={input} type="number" min="0.1" step="0.1" aria-label="People per home" bind:value={people} /></label>
    <label class={row}>Lot coverage <input class={input} type="number" min="1" max="100" step="5" aria-label="Lot coverage (%)" bind:value={coveragePct} /></label>
  </div>
  {#if mode === "rule" && overriddenBy !== null}
    <p class="mt-2 text-muted-foreground">Rule {overriddenBy} overrides this one where they overlap.</p>
  {/if}
  {#if mode === "rule" && !draggable}
    <p class="mt-2 text-muted-foreground">This rule's area is in several pieces, so it can't be dragged. Delete it and draw it again to move it.</p>
  {/if}
  {#if localError ?? error?.text}
    <p class="mt-2 text-state-error" role="alert">{localError ?? error?.text}</p>
  {/if}
  <div class="mt-3 flex items-center gap-2">
    {#if mode === "new"}
      <button type="submit" class="rounded border border-accent/40 bg-accent/10 px-2.5 py-1 text-accent disabled:opacity-50" disabled={busy}>Apply</button>
    {:else}
      <button type="submit" class="rounded border border-accent/40 bg-accent/10 px-2.5 py-1 text-accent disabled:opacity-50" disabled={busy}>Save</button>
      <button type="button" class="rounded px-1.5 py-1 text-state-error disabled:opacity-50" disabled={busy} onclick={ondelete}>Delete rule</button>
    {/if}
    <button type="button" class="ml-auto px-1.5 py-1 text-muted-foreground hover:text-foreground disabled:opacity-50" disabled={busy} onclick={oncancel}>Cancel</button>
  </div>
  {#if busy}
    <p class="mt-2 text-muted-foreground" role="status">Recomputing…</p>
  {/if}
</form>
