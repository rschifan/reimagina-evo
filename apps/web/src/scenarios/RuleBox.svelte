<script lang="ts">
  import { onMount } from "svelte";

  import { percentOf } from "../scenario";
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
    /** Which later rules win over this one where they overlap, and for
     *  which numbers; null when none does. */
    overlapNote: string | null;
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
    overlapNote,
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
  // Empty means "not set by this rule": every block keeps its own value.
  let levels = $state<number | null>(initial.levels);
  let unitSize = $state<number | null>(initial.unitSize);
  let people = $state<number | null>(initial.peoplePerHousehold);
  // Unrounded: a coverage of 0.625 shows as 62.5 and is saved as 0.625, not
  // rounded to 63 by a Save that only changed the storeys.
  let coveragePct = $state<number | null>(
    initial.lotCoverage === null ? null : percentOf(initial.lotCoverage),
  );
  const inputs: Record<string, HTMLInputElement | undefined> = $state({});
  const LABELS: Record<string, string> = {
    levels: "Storeys",
    unit_size: "m² per home",
    people_per_household: "People per home",
    lot_coverage: "Lot coverage",
  };
  let localError = $state<string | null>(null);
  let localField = $state<string | null>(null);
  /** The field a refusal is about, by the API's name, for aria-invalid. */
  const badField = $derived(localError ? localField : (error?.field ?? null));

  onMount(() => {
    // Keyboard users land in the box that just opened.
    (needsScenarioName ? undefined : inputs.levels)?.focus();
  });
  let name = $state((() => defaultName)());

  /** A number input's value, or null when it is empty. */
  const set = (v: number | null | undefined | string): number | null =>
    v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v);

  function read(): RuleValues | null {
    // A number the browser could not parse ("3,5" where it expects "3.5")
    // binds as empty — which would read as "not set" and be dropped silently.
    for (const [field, el] of Object.entries(inputs)) {
      if (el?.validity.badInput) {
        localField = field;
        localError = `${LABELS[field]} is not a number this browser can read; use a dot for decimals.`;
        return null;
      }
    }
    const next: RuleValues = {
      levels: set(levels),
      unitSize: set(unitSize),
      peoplePerHousehold: set(people),
      lotCoverage: set(coveragePct) === null ? null : (set(coveragePct) as number) / 100,
    };
    if (Object.values(next).every((v) => v === null)) {
      localField = null;
      localError = "Set at least one number: a rule changes only what you type.";
      return null;
    }
    const pct = set(coveragePct);
    if (pct !== null && (pct <= 0 || pct > 100)) {
      localField = "lot_coverage";
      localError = "Lot coverage must be more than 0 and at most 100 %.";
      return null;
    }
    localError = null;
    return next;
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
      // Mid-recompute the box stays open: a refusal is shown only here.
      if (!busy) oncancel();
    }
  }}
>
  <h3 class="mb-2 text-sm text-foreground">{mode === "new" ? "New rule" : "Rule"}</h3>
  {#if needsScenarioName}
    <label class="mb-2 flex flex-col gap-1 text-muted-foreground">
      New scenario
      <input class="rounded border border-line bg-surface px-1.5 py-0.5 text-xs" aria-label="Scenario name" aria-invalid={badField === "name"} aria-describedby={badField === "name" ? "rule-box-error" : undefined} bind:value={name} />
    </label>
  {/if}
  <p class="mb-2 text-muted-foreground">Set what this area changes. A number left empty keeps each block's own value.</p>
  <div class="flex flex-col gap-1.5">
    <label class={row}>Storeys <input class={input} type="number" min="1" step="1" aria-label="Storeys" placeholder="as is" aria-invalid={badField === "levels"} aria-describedby={badField === "levels" ? "rule-box-error" : undefined} bind:this={inputs.levels} bind:value={levels} /></label>
    <label class={row}>m² per home <input class={input} type="number" min="1" step="1" aria-label="m² per home" placeholder="as is" aria-invalid={badField === "unit_size"} aria-describedby={badField === "unit_size" ? "rule-box-error" : undefined} bind:this={inputs.unit_size} bind:value={unitSize} /></label>
    <label class={row}>People per home <input class={input} type="number" min="0.1" step="0.1" aria-label="People per home" placeholder="as is" aria-invalid={badField === "people_per_household"} aria-describedby={badField === "people_per_household" ? "rule-box-error" : undefined} bind:this={inputs.people_per_household} bind:value={people} /></label>
    <label class={row}>Lot coverage <input class={input} type="number" min="1" max="100" step="any" aria-label="Lot coverage (%)" placeholder="as is" aria-invalid={badField === "lot_coverage"} aria-describedby={badField === "lot_coverage" ? "rule-box-error" : undefined} bind:this={inputs.lot_coverage} bind:value={coveragePct} /></label>
  </div>
  {#if mode === "rule" && overlapNote !== null}
    <p class="mt-2 text-muted-foreground">{overlapNote}</p>
  {/if}
  {#if mode === "rule" && !draggable}
    <p class="mt-2 text-muted-foreground">This rule's area is in several pieces, so it can't be dragged. Delete it and draw it again to move it.</p>
  {/if}
  {#if localError ?? error?.text}
    <p id="rule-box-error" class="mt-2 text-state-error" role="alert">{localError ?? error?.text}</p>
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
