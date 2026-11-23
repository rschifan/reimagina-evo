<script lang="ts">
  import { onMount } from "svelte";

  import type { RuleInputs, RuleMode, RuleUnit, RuleUnits } from "../api";
  import { percentOf, RULE_NUMBERS } from "../scenario";

  /**
   * The numbers of one rule, next to its shape on the map.
   *
   * `new`: a shape just drawn — Apply writes it (and, on the baseline city,
   * creates the scenario first). `rule`: a saved rule — Save writes its new
   * numbers and, if it was dragged, its new area; Delete removes it. Either
   * way the store recomputes and the map shows the change; this component
   * holds only what is being typed.
   *
   * Each number chooses how it applies: "=" (the blocks' new value), "+" an
   * amount in its own unit (coverage in points), or "+ %" of each block's
   * baseline. So one rule can set coverage and raise storeys over the same
   * area. A rule whose numbers are all "=" is sent as a plain set rule; any
   * other is a change rule whose `units` carry each number's choice. An empty
   * number starts at "+ %", the way planners first asked ("+15 %"); the line
   * under the numbers reads the rule back in words, so "+30" cannot be
   * applied as 30 storeys by someone who meant 30 %.
   *
   * Coverage is typed in percent (or points), as planners say it, and handed
   * on as a share. `novalidate`: the checks below name the field and the
   * range; the browser's own would pre-empt them with a tooltip that says
   * neither.
   */
  interface Props {
    mode: "new" | "rule";
    values: RuleInputs;
    needsScenarioName: boolean;
    defaultName: string;
    /** Which later rules win over this one where they overlap, and for
     *  which numbers; null when none does. */
    overlapNote: string | null;
    busy: boolean;
    error: { field: string | null; text: string } | null;
    draggable: boolean;
    /** How the saved rule applies its numbers; a new rule starts at "set". */
    ruleMode?: RuleMode;
    ruleUnits?: RuleUnits;
    /** A rule of "=" numbers only is applied with two arguments; any other
     *  adds "change" and each number's unit. */
    onapply: (values: RuleInputs, scenarioName?: string, ruleMode?: RuleMode, units?: RuleUnits) => void;
    onsave: (values: RuleInputs, ruleMode?: RuleMode, units?: RuleUnits) => void;
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
    ruleMode: savedMode = "set",
    ruleUnits = {},
    onapply,
    onsave,
    ondelete,
    oncancel,
  }: Props = $props();

  // Seeded once from the props: the box is remounted for every shape, and
  // a prop change mid-typing must not overwrite what is being typed.
  const initial = (() => values)();
  const startMode = (() => savedMode)();
  const startUnits = (() => ruleUnits)();
  /** A saved number's choice: all "=" in a set rule; in a change rule its
   *  unit, an amount when it has none (as the server reads it). */
  const savedUnit = (key: keyof RuleInputs): RuleUnit =>
    startMode === "set" ? "set" : (startUnits[key] ?? "abs");
  // Coverage is shown in percent (or points); a % change is shown as typed.
  // Unrounded: 0.625 shows as 62.5 and is saved as 0.625.
  const shown = (key: keyof RuleInputs): number | null => {
    const v = initial[key];
    if (v === undefined) return null;
    return key === "lot_coverage" && savedUnit(key) !== "pct" ? percentOf(v) : v;
  };
  let numbers = $state<Record<keyof RuleInputs, number | null>>({
    levels: shown("levels"),
    unit_size: shown("unit_size"),
    people_per_household: shown("people_per_household"),
    lot_coverage: shown("lot_coverage"),
  });
  let units = $state<RuleUnits>(
    Object.fromEntries(Object.keys(initial).map((k) => [k, savedUnit(k as keyof RuleInputs)])),
  );
  const unitOf = (key: keyof RuleInputs): RuleUnit => units[key] ?? "pct";
  /** Each number's amount unit. */
  const AMOUNT: Record<keyof RuleInputs, string> = {
    levels: "storeys", unit_size: "m²", people_per_household: "people", lot_coverage: "points",
  };
  const CHOICES = (key: keyof RuleInputs): ReadonlyArray<readonly [RuleUnit, string]> => [
    ["set", "="], ["abs", `+ ${AMOUNT[key]}`], ["pct", "+ %"],
  ];

  const inputs: Record<string, HTMLInputElement | undefined> = $state({});
  const LABELS = Object.fromEntries(RULE_NUMBERS.map(([key, label]) => [key, label]));
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

  /** What the typed numbers will do, in words: "Storeys +30 % · Lot coverage = 40 %". */
  const readBack = $derived(
    RULE_NUMBERS.flatMap(([key, label]) => {
      const v = set(numbers[key]);
      if (v === null) return [];
      const unit = unitOf(key);
      if (unit === "set") return [`${label} = ${v}${key === "lot_coverage" ? " %" : ""}`];
      const sign = v < 0 ? "−" : "+";
      return [`${label} ${sign}${Math.abs(v)} ${unit === "pct" ? "%" : AMOUNT[key]}`];
    }),
  );

  /** The typed numbers as the API takes them, or null after a refusal. */
  function read(): RuleInputs | null {
    // A number the browser could not parse ("3,5" where it expects "3.5")
    // binds as empty — which would read as "not set" and be dropped silently.
    for (const [field, el] of Object.entries(inputs)) {
      if (el?.validity.badInput) {
        localField = field;
        localError = `${LABELS[field]} is not a number this browser can read; use a dot for decimals.`;
        return null;
      }
    }
    const next: RuleInputs = {};
    for (const [key] of RULE_NUMBERS) {
      const v = set(numbers[key]);
      if (v === null) continue;
      const unit = unitOf(key);
      if (key === "lot_coverage" && unit === "set" && (v <= 0 || v > 100)) {
        localField = key;
        localError = "Lot coverage must be more than 0 and at most 100 %.";
        return null;
      }
      // Coverage set or changed by points is a share; a % change is as typed.
      next[key] = key === "lot_coverage" && unit !== "pct" ? v / 100 : v;
    }
    if (Object.keys(next).length === 0) {
      localField = null;
      localError = "Set at least one number: a rule changes only what you type.";
      return null;
    }
    localError = null;
    return next;
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const next = read();
    if (!next) return;
    const sent: RuleUnits = Object.fromEntries(
      Object.keys(next).map((k) => [k, unitOf(k as keyof RuleInputs)]),
    );
    // All "=": a plain set rule, sent as before. Anything else is a change
    // rule whose units carry each number's choice, "set" included.
    if (Object.values(sent).every((u) => u === "set")) {
      if (mode === "new") onapply(next, needsScenarioName ? name : undefined);
      else onsave(next);
      return;
    }
    if (mode === "new") onapply(next, needsScenarioName ? name : undefined, "change", sent);
    else onsave(next, "change", sent);
  }

  const row = "flex items-center justify-between gap-2 text-xs text-muted-foreground";
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<form
  class="w-72 rounded-lg border border-line bg-surface p-3 text-xs shadow-lg"
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
  <p class="mb-2 text-muted-foreground">Per number: = sets it, + changes each block from its baseline. Empty keeps each block's own value.</p>
  <div class="flex flex-col gap-1.5">
    {#each RULE_NUMBERS as [key, label] (key)}
      <div class={row}>
        <span>{label}</span>
        <span class="flex items-center gap-1">
          <input class="w-14 rounded border border-line bg-surface px-1.5 py-0.5 text-right text-xs tabular-nums" type="number" step="any" aria-label={label} placeholder="as is" aria-invalid={badField === key} aria-describedby={badField === key ? "rule-box-error" : undefined} bind:this={inputs[key]} bind:value={numbers[key]} />
          <span role="group" aria-label="{label} unit" class="flex overflow-hidden rounded border border-line">
            {#each CHOICES(key) as [u, text] (u)}
              <button
                type="button"
                class="px-1 py-0.5 text-[10px] {unitOf(key) === u ? 'bg-accent-strong text-white' : 'text-muted-foreground'}"
                aria-pressed={unitOf(key) === u}
                disabled={busy}
                onclick={() => (units = { ...units, [key]: u })}
              >{text}</button>
            {/each}
          </span>
        </span>
      </div>
    {/each}
  </div>
  {#if readBack.length}
    <p class="mt-2 text-foreground" aria-label="This rule" aria-live="polite">
      Each block: {readBack.join(" · ")}
    </p>
  {/if}
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
