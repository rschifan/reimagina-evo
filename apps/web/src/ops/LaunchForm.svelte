<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import Card from "../lib/components/ui/Card.svelte";
  import {
    listChangeInputs,
    listIndicators,
    previewImpact,
    type ChangeInput,
    type DatasetDependency,
    type ImpactPreview,
    type IndicatorSummary,
  } from "./api";
  import {
    aoiOptions,
    describeApiError,
    launchRun,
    loadAois,
    ops,
  } from "./store.svelte";

  let { refreshNow, onClose }: { refreshNow: () => void; onClose: () => void } =
    $props();

  /**
   * Three ways to scope a run, because they answer different questions.
   *
   * "stale" — recompute whatever is out of date. The operational one.
   * "changed" — name what changed (an attribute, or a whole dataset such as the
   *   road network) and let the planner derive the indicators that read it.
   * "pick" — choose indicators by name, needing no internal vocabulary at all.
   */
  type Mode = "stale" | "changed" | "pick";

  let mode = $state<Mode>("stale");
  let aoiId = $state("");
  let scenarioId = $state("");
  let picked = $state.raw<Set<string>>(new Set());
  let pickedAttrs = $state.raw<Set<string>>(new Set());
  let pickedDatasets = $state.raw<Set<string>>(new Set());
  let attrInputs = $state.raw<ChangeInput[]>([]);
  let datasetInputs = $state.raw<DatasetDependency[]>([]);
  let catalog = $state.raw<IndicatorSummary[]>([]);
  let preview = $state.raw<ImpactPreview | null>(null);
  let previewError = $state<string | null>(null);
  let message = $state<string | null>(null);
  let failed = $state(false);

  const busy = $derived(ops.isBusy("launch"));

  const body = $derived(
    mode === "pick"
      ? { aoi_id: aoiId || null, indicators: [...picked] }
      : mode === "changed"
        ? {
            aoi_id: aoiId || null,
            changed_attributes: [...pickedAttrs],
            changed_datasets: [...pickedDatasets],
          }
        : {
            aoi_id: aoiId || null,
            scenario_id: scenarioId.trim() || null,
          },
  );

  $effect(() => {
    void loadAois();
    listIndicators()
      .then((rows) => (catalog = rows))
      .catch(() => (catalog = []));
    listChangeInputs()
      .then((r) => {
        // Version markers resolve to the same indicators as the datasets they
        // stand for, so offering both would be two controls for one change.
        attrInputs = r.attributes.filter((a) => !a.is_version_marker);
        datasetInputs = r.datasets;
      })
      .catch(() => {
        attrInputs = [];
        datasetInputs = [];
      });
  });

  // Ask the planner what this scope would do, before anyone commits to it.
  // The endpoint persists nothing, so running it on every edit is free.
  $effect(() => {
    const ctrl = new AbortController();
    const request = body;
    previewError = null;
    previewImpact(request, { signal: ctrl.signal })
      .then((p) => (preview = p))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        preview = null;
        previewError = err instanceof Error ? err.message : String(err);
      });
    return () => ctrl.abort();
  });

  function toggle(slug: string) {
    const next = new Set(picked);
    if (!next.delete(slug)) next.add(slug);
    picked = next;
  }

  function toggleIn(set: Set<string>, key: string): Set<string> {
    const next = new Set(set);
    if (!next.delete(key)) next.add(key);
    return next;
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    message = null;
    failed = false;
    try {
      const { status, runId } = await launchRun(body, refreshNow);
      if (!runId) {
        message = `Nothing to recompute (${status}). ${preview?.reason ?? ""}`;
        return;
      }
      location.hash = `#/r/${runId}`;
      onClose();
    } catch (err) {
      failed = true;
      // `describeApiError`, not `err.message`. The latter is the status line
      // `ApiError` builds — "500 Internal Server Error on /jobs" — which names
      // the route and says nothing about the cause, while the body the API
      // sent carries either its own sentence or a request id to search the
      // logs by. Launching was the only action here still discarding that;
      // cancel, rerun and delete have always read it.
      message = await describeApiError(err);
    }
  }

  const canSubmit = $derived(
    !busy &&
      preview?.noop !== true &&
      (mode === "stale" ||
        (mode === "pick" && picked.size > 0) ||
        (mode === "changed" && pickedAttrs.size + pickedDatasets.size > 0)),
  );
</script>

<div class="px-4 pt-3">
  <Card title="New run">
    <form class="grid gap-4" onsubmit={submit}>
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-muted-foreground">Recompute</span>
        <button
          type="button"
          aria-pressed={mode === "stale"}
          onclick={() => (mode = "stale")}
          class={`rounded border px-2.5 py-1 text-xs ${
            mode === "stale"
              ? "border-accent-strong bg-accent-strong text-white"
              : "border-line bg-surface-raised text-foreground hover:border-accent/50"
          }`}
        >
          what is out of date
        </button>
        <button
          type="button"
          aria-pressed={mode === "changed"}
          onclick={() => (mode = "changed")}
          class={`rounded border px-2.5 py-1 text-xs ${
            mode === "changed"
              ? "border-accent-strong bg-accent-strong text-white"
              : "border-line bg-surface-raised text-foreground hover:border-accent/50"
          }`}
        >
          what changed
        </button>
        <button
          type="button"
          aria-pressed={mode === "pick"}
          onclick={() => (mode = "pick")}
          class={`rounded border px-2.5 py-1 text-xs ${
            mode === "pick"
              ? "border-accent-strong bg-accent-strong text-white"
              : "border-line bg-surface-raised text-foreground hover:border-accent/50"
          }`}
        >
          indicators I choose
        </button>
      </div>

      {#if mode === "changed"}
        <div class="rounded border border-line p-2">
          <div class="mb-1 font-mono text-[11px] text-foreground">
            attributes
            <span class="ml-1 text-[10px] text-muted-foreground">
              a field on a feature
            </span>
          </div>
          <div class="mb-3 flex flex-wrap gap-1.5">
            {#each attrInputs as input (input.attribute)}
              <button
                type="button"
                aria-pressed={pickedAttrs.has(input.attribute)}
                title={`${input.dataset}.${input.attribute} — read by ${input.indicators.length} indicator(s)`}
                onclick={() => (pickedAttrs = toggleIn(pickedAttrs, input.attribute))}
                class={`rounded border px-2 py-1 font-mono text-[11px] ${
                  pickedAttrs.has(input.attribute)
                    ? "border-accent-strong bg-accent-strong text-white"
                    : "border-line bg-surface-raised text-foreground hover:border-accent/50"
                }`}
              >
                {input.attribute}
                <span class="ml-1 opacity-60" data-numeric>{input.indicators.length}</span>
              </button>
            {/each}
          </div>

          <div class="mb-1 font-mono text-[11px] text-foreground">
            whole datasets
            <span class="ml-1 text-[10px] text-muted-foreground">
              reloaded or edited — e.g. a street added to the network
            </span>
          </div>
          <div class="flex flex-wrap gap-1.5">
            {#each datasetInputs as d (d.dataset)}
              <button
                type="button"
                aria-pressed={pickedDatasets.has(d.dataset)}
                title={`${d.indicators.length} indicator(s) declare an edge to ${d.dataset} (${d.relation})`}
                onclick={() => (pickedDatasets = toggleIn(pickedDatasets, d.dataset))}
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
      {:else if mode === "pick"}
        <div class="rounded border border-line p-2">
          <div class="mb-2 flex items-center gap-2">
            <button
              type="button"
              class="rounded border border-line px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
              onclick={() => (picked = new Set(catalog.map((i) => i.slug)))}
            >
              Select all
            </button>
            <button
              type="button"
              class="rounded border border-line px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
              onclick={() => (picked = new Set())}
            >
              Clear
            </button>
            <span class="font-mono text-[11px] text-muted-foreground" data-numeric>
              {picked.size} of {catalog.length} selected
            </span>
          </div>
          <div class="grid gap-1 sm:grid-cols-2">
            {#each catalog as ind (ind.slug)}
              <label class="flex items-start gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={picked.has(ind.slug)}
                  onchange={() => toggle(ind.slug)}
                />
                <span>
                  {ind.name}
                  <span class="block font-mono text-[10px] text-muted-foreground">
                    {ind.slug}
                  </span>
                </span>
              </label>
            {/each}
          </div>
        </div>
      {:else}
        <label class="flex max-w-md flex-col gap-1 text-xs text-muted-foreground">
          Scenario id (optional)
          <input
            class="rounded border border-line bg-surface-raised px-2 py-1 font-mono text-xs text-foreground"
            placeholder="baseline if empty"
            bind:value={scenarioId}
          />
        </label>
      {/if}

      <label class="flex max-w-xs flex-col gap-1 text-xs text-muted-foreground">
        AOI (optional)
        <select
          class="rounded border border-line bg-surface-raised px-2 py-1 text-xs text-foreground"
          bind:value={aoiId}
        >
          <option value="">(whole dataset)</option>
          {#each aoiOptions() as aoi (aoi.aoi_id)}
            <option value={aoi.aoi_id}>{aoi.name}</option>
          {/each}
        </select>
      </label>

      <!-- The planner's own answer, before anything is submitted. This is what
           turns "why did I get noop?" into something you can see coming. -->
      <div class="rounded border border-line bg-surface-raised px-3 py-2" role="status">
        {#if previewError}
          <span class="font-mono text-[11px] text-state-error">{previewError}</span>
        {:else if !preview}
          <span class="font-mono text-[11px] text-muted-foreground">checking…</span>
        {:else if preview.noop}
          <Badge tone="stale">nothing to recompute</Badge>
          <span class="ml-2 font-mono text-[11px] text-muted-foreground">
            {preview.reason}
          </span>
        {:else}
          <Badge tone="fresh">{preview.count} indicator{preview.count === 1 ? "" : "s"}</Badge>
          <span class="ml-2 font-mono text-[11px] text-muted-foreground">
            {preview.reason}
          </span>
          <p class="mt-1 font-mono text-[10.5px] break-words text-muted-foreground">
            {preview.indicators.join(" · ")}
          </p>
        {/if}
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={!canSubmit}
          aria-busy={busy}
          class="rounded border border-accent/40 bg-accent/10 px-3 py-1 text-xs text-accent
                 hover:bg-accent/20 disabled:opacity-40"
        >
          {busy ? "Submitting…" : "Run"}
        </button>
        <button
          type="button"
          class="rounded border border-line px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
          onclick={onClose}
        >
          Cancel
        </button>

        {#if message}
          <span
            class={`font-mono text-[11px] ${failed ? "text-state-error" : "text-muted-foreground"}`}
            role="status"
          >
            {message}
          </span>
        {/if}
      </div>
    </form>
  </Card>
</div>
