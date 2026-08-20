<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import {
    listChangeInputs,
    previewImpact,
    submitRun,
    type ChangeInput,
    type DatasetDependency,
    type ImpactPreview,
  } from "./api";

  /**
   * A dry run of the recomputation engine.
   *
   * Answers "if this changed, what would recompute?" without submitting
   * anything: the endpoint derives the dirty set and persists nothing.
   *
   * Inputs are shown as `dataset · attribute`, which is what they actually
   * are. The flat attribute list the planner accepts drops the dataset, so a
   * bare `geometry` gives no way to tell whose geometry it is.
   */
  let inputs = $state.raw<ChangeInput[]>([]);
  let datasets = $state.raw<DatasetDependency[]>([]);
  let unconsumed = $state.raw<string[]>([]);
  let picked = $state.raw<Set<string>>(new Set());
  let pickedDatasets = $state.raw<Set<string>>(new Set());
  let preview = $state.raw<ImpactPreview | null>(null);
  let error = $state<string | null>(null);
  let submitting = $state(false);
  let submitMessage = $state<string | null>(null);
  let submitFailed = $state(false);

  $effect(() => {
    listChangeInputs()
      .then((r) => {
        inputs = r.attributes;
        datasets = r.datasets;
        unconsumed = r.unconsumed;
      })
      .catch(() => {
        inputs = [];
        datasets = [];
        unconsumed = [];
      });
  });

  $effect(() => {
    const chosen = [...picked];
    const chosenDatasets = [...pickedDatasets];
    if (!chosen.length && !chosenDatasets.length) {
      preview = null;
      error = null;
      return;
    }
    const ctrl = new AbortController();
    error = null;
    previewImpact(
      { changed_attributes: chosen, changed_datasets: chosenDatasets },
      { signal: ctrl.signal },
    )
      .then((p) => (preview = p))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        preview = null;
        error = err instanceof Error ? err.message : String(err);
      });
    return () => ctrl.abort();
  });

  async function run() {
    submitting = true;
    submitMessage = null;
    submitFailed = false;
    try {
      // The same body the preview was computed from, so the run cannot differ
      // from what was shown.
      const res = await submitRun({
        changed_attributes: [...picked],
        changed_datasets: [...pickedDatasets],
      });
      if (!res.run_id) {
        submitMessage = `Nothing to recompute (${res.status}).`;
        return;
      }
      // The ops page is a separate entry, so this is a real navigation.
      location.href = `/ops.html#/r/${res.run_id}`;
    } catch (err) {
      submitFailed = true;
      submitMessage = err instanceof Error ? err.message : String(err);
    } finally {
      submitting = false;
    }
  }

  function toggle(attr: string) {
    const next = new Set(picked);
    if (!next.delete(attr)) next.add(attr);
    picked = next;
  }

  function toggleDataset(key: string) {
    const next = new Set(pickedDatasets);
    if (!next.delete(key)) next.add(key);
    pickedDatasets = next;
  }

  /**
   * Real attributes only.
   *
   * `poi_version` and `network_version` are stand-in markers whose whole job was
   * to simulate "that dataset changed" back when a change-set was the only thing
   * the planner accepted. Selecting one resolves to exactly the same indicators
   * as selecting the dataset it stands for — verified identical — so showing
   * both offers two controls for one change and invites the reader to look for a
   * difference that is not there. The dataset button is the honest control; the
   * marker is named on it instead.
   */
  const byDataset = $derived(
    inputs
      .filter((i) => !i.is_version_marker)
      .reduce<Record<string, ChangeInput[]>>((acc, i) => {
        (acc[i.dataset] ??= []).push(i);
        return acc;
      }, {}),
  );
</script>

<section class="rounded-lg border border-line">
  <header class="flex flex-wrap items-baseline gap-x-3 border-b border-line px-4 py-2">
    <h2 class="font-mono text-[11px] tracking-wide text-muted-foreground uppercase">
      Recomputation check
    </h2>
    <p class="text-[11px] text-muted-foreground">
      Pick what changed; the planner says what it would recompute. A dry run —
      nothing is submitted and nothing is written.
    </p>
  </header>

  <!-- Two fixed columns, each scrolling internally. The panel's height never
       changes as you select, so the page underneath does not reflow. -->
  <div class="grid gap-0 sm:grid-cols-2">
    <div class="max-h-72 overflow-y-auto border-line px-4 py-3 sm:border-r">
      <div class="mb-2 flex items-baseline justify-end gap-2">
        {#if picked.size || pickedDatasets.size}
          <button
            type="button"
            class="rounded border border-line px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
            onclick={() => {
              picked = new Set();
              pickedDatasets = new Set();
            }}
          >
            Clear
          </button>
        {/if}
      </div>
      {#each Object.entries(byDataset) as [dataset, items] (dataset)}
        <div class="mb-3">
          <div class="mb-1 flex items-baseline gap-2">
            <span class="font-mono text-[11px] text-foreground">{dataset}</span>
            <span class="text-[10px] text-muted-foreground">dataset</span>
          </div>
          <div class="flex flex-wrap gap-1.5">
            {#each items as input (input.attribute)}
              <button
                type="button"
                aria-pressed={picked.has(input.attribute)}
                title={`${input.indicators.length} indicator(s) read ${dataset}.${input.attribute}`}
                onclick={() => toggle(input.attribute)}
                class={`rounded border px-2 py-1 font-mono text-[11px] ${
                  picked.has(input.attribute)
                    ? "border-accent-strong bg-accent-strong text-white"
                    : "border-line bg-surface-raised text-foreground hover:border-accent/50"
                }`}
              >
                {input.attribute}
                <span class="ml-1 opacity-60" data-numeric>{input.indicators.length}</span>
                {#if input.is_version_marker}<span class="ml-1" title="stand-in marker">*</span>{/if}
              </button>
            {/each}
          </div>
        </div>
      {/each}

      {#if datasets.length}
        <div class="mt-3 border-t border-line/60 pt-2">
          <div class="mb-1 flex items-baseline gap-2">
            <span class="font-mono text-[11px] text-foreground">whole datasets</span>
            <span class="text-[10px] text-muted-foreground">
              reloaded or edited — e.g. a street added to the network
            </span>
          </div>
          <div class="flex flex-wrap gap-1.5">
            {#each datasets as d (d.dataset)}
              <button
                type="button"
                aria-pressed={pickedDatasets.has(d.dataset)}
                title={`${d.indicators.length} indicator(s) declare an edge to ${d.dataset} (${d.relation})`}
                onclick={() => toggleDataset(d.dataset)}
                class={`rounded border px-2 py-1 font-mono text-[11px] ${
                  pickedDatasets.has(d.dataset)
                    ? "border-accent-strong bg-accent-strong text-white"
                    : "border-line bg-surface-raised text-foreground hover:border-accent/50"
                }`}
              >
                {d.dataset}
                <span class="ml-1 opacity-60">{d.relation}</span>
                <span class="ml-1 opacity-60" data-numeric>{d.indicators.length}</span>
              </button>
            {/each}
          </div>
        </div>
      {/if}

      {#if unconsumed.length}
        <div class="mt-2 border-t border-line/60 pt-2">
          <p class="mb-1 text-[10px] text-muted-foreground">
            Registered but consumed by no indicator — editing one recomputes
            nothing, which is a coverage gap rather than a planner fault.
          </p>
          <p class="font-mono text-[10.5px] text-muted-foreground">
            {unconsumed.join(" · ")}
          </p>
        </div>
      {/if}
    </div>

    <div class="max-h-72 overflow-y-auto px-4 py-3">
      {#if error}
        <span class="font-mono text-[11px] text-state-error">{error}</span>
      {:else if !preview}
        <p class="text-[11px] text-muted-foreground">
          Select one or more inputs on the left.
        </p>
      {:else if preview.noop}
        <Badge tone="stale">nothing recomputes</Badge>
        <p class="mt-2 font-mono text-[11px] text-muted-foreground">{preview.reason}</p>
      {:else}
        <div class="flex items-baseline gap-2">
          <Badge tone="fresh">
            {preview.count} indicator{preview.count === 1 ? "" : "s"}
          </Badge>
          <span class="font-mono text-[11px] text-muted-foreground">
            {preview.reason}
          </span>
        </div>
        <ol class="mt-2 space-y-0.5">
          {#each preview.indicators as slug, i (slug)}
            <li class="font-mono text-[11px]">
              <span class="text-muted-foreground" data-numeric>{i + 1}.</span>
              {slug}
            </li>
          {/each}
        </ol>
        <p class="mt-2 text-[10.5px] text-muted-foreground">
          Dependency order — an indicator appears after the ones it reads.
        </p>

        <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-line/60 pt-3">
          <button
            type="button"
            onclick={run}
            disabled={submitting}
            aria-busy={submitting}
            class="rounded border border-accent/40 bg-accent/10 px-3 py-1 text-xs text-accent
                   hover:bg-accent/20 disabled:opacity-40"
          >
            {submitting ? "Submitting…" : `Run these ${preview.count}`}
          </button>
          <span class="text-[10.5px] text-muted-foreground">
            opens the run in Ops · needs a worker running
          </span>
        </div>

        {#if submitMessage}
          <p
            class={`mt-2 font-mono text-[11px] ${submitFailed ? "text-state-error" : "text-muted-foreground"}`}
            role="status"
          >
            {submitMessage}
          </p>
        {/if}
      {/if}
    </div>
  </div>

</section>
