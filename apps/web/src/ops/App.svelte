<script lang="ts">
  import { RefreshCw } from "@lucide/svelte";

  import TopBar from "../lib/TopBar.svelte";

  import { RUN_STATUSES, type RunStatus } from "./api";
  import {
    isRunTerminal,
    runIdFromHash,
    serverNow,
    statusesFromHash,
    statusesToHash,
  } from "./derive";
  import HealthStrip from "./HealthStrip.svelte";
  import LaunchForm from "./LaunchForm.svelte";
  import RunDetail from "./RunDetail.svelte";
  import RunList from "./RunList.svelte";
  import {
    boot,
    ops,
    removeFinished,
    setFilters,
    setOpenRun,
    setPaused,
    startPolling,
    loadMore,
  } from "./store.svelte";

  let hash = $state(location.hash);
  const openRunId = $derived(runIdFromHash(hash));

  // A clock of its own, not the poll tick: deriving "now" from the last
  // response would freeze every relative time whenever polling backs off or
  // the tab is hidden.
  let clientNow = $state(Date.now());
  const now = $derived(serverNow(clientNow, ops.offsetMs));

  let refresh: (() => void) | null = null;
  /** Announced transitions only. Marking the table itself polite would
   *  re-announce fifty rows every two seconds. */
  let showLaunch = $state(false);
  // Same two-press confirmation as the per-run destructive actions.
  let armedClear = $state(false);
  let clearTimer: ReturnType<typeof setTimeout> | null = null;
  let clearMessage = $state<string | null>(null);

  const finishedCount = $derived(
    ops.runs.filter((r) => isRunTerminal(r.status)).length,
  );

  $effect(() => () => {
    if (clearTimer) clearTimeout(clearTimer);
  });

  async function clearFinished() {
    if (!armedClear) {
      armedClear = true;
      if (clearTimer) clearTimeout(clearTimer);
      clearTimer = setTimeout(() => (armedClear = false), 4_000);
      return;
    }
    armedClear = false;
    const { error, deleted } = await removeFinished(() => refresh?.());
    clearMessage = error ?? `removed ${deleted} run${deleted === 1 ? "" : "s"}`;
  }
  let announcement = $state("");
  let previous = new Map<string, string>();

  $effect(() => {
    const onHash = () => (hash = location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  });

  $effect(() => {
    setOpenRun(openRunId);
  });

  $effect(() => {
    setFilters(statusesFromHash(hash, RUN_STATUSES));
  });

  $effect(() => {
    void boot();
    const poll = startPolling();
    refresh = poll.refreshNow;
    return poll.stop;
  });

  $effect(() => {
    const t = setInterval(() => {
      if (!document.hidden) clientNow = Date.now();
    }, 1_000);
    return () => clearInterval(t);
  });

  $effect(() => {
    const changed: string[] = [];
    for (const run of ops.runs) {
      const before = previous.get(run.run_id);
      if (before && before !== run.status) {
        changed.push(`run ${run.run_id.slice(0, 8)} ${run.status}`);
      }
      previous.set(run.run_id, run.status);
    }
    if (changed.length) announcement = changed.join(", ");
  });

  function toggleStatus(status: RunStatus) {
    const next = ops.filters.includes(status)
      ? ops.filters.filter((s) => s !== status)
      : [...ops.filters, status];
    location.hash = statusesToHash(next);
  }
</script>

<TopBar current="ops">
  {#snippet actions()}
    <span role="status" class="font-mono text-[11px] text-muted-foreground">
      {#if ops.lastLoadedAt}
        updated {ops.lastLoadedAt.toLocaleTimeString()}
      {/if}
    </span>
    {#if ops.refreshing}
      <RefreshCw class="size-3 text-muted-foreground animate-spin" aria-hidden="true" />
    {/if}
    {#if ops.stale}
      <span
        class="rounded border border-state-stale/40 px-1.5 py-0.5 font-mono text-[11px] text-state-stale"
      >
        stale — no successful refresh in over a minute
      </span>
    {/if}
    {#if ops.error}
      <span
        class="rounded border border-state-error/40 px-1.5 py-0.5 font-mono text-[11px] text-state-error"
      >
        refresh failed — showing last known
      </span>
    {/if}
    <button
      class="rounded border border-accent/40 bg-accent/10 px-2 py-1 font-mono text-[11px] text-accent hover:bg-accent/20"
      onclick={() => (showLaunch = !showLaunch)}
    >
      New run
    </button>
    <button
      class="rounded border border-line px-2 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
      onclick={() => refresh?.()}
    >
      Refresh now
    </button>
    {#if !openRunId && finishedCount > 0}
      <button
        class={`rounded border px-2 py-1 font-mono text-[11px] ${
          armedClear
            ? "border-state-error/60 bg-state-error/15 text-state-error"
            : "border-line text-muted-foreground hover:text-foreground"
        }`}
        disabled={ops.isBusy("delete-all")}
        title="Removes finished runs from the history. Computed values are not affected."
        onclick={clearFinished}
      >
        {armedClear ? `Delete ${finishedCount} finished?` : "Clear history"}
      </button>
    {/if}
    {#if clearMessage}
      <span class="font-mono text-[11px] text-muted-foreground" role="status">
        {clearMessage}
      </span>
    {/if}
    <button
      class="rounded border border-line px-2 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
      aria-pressed={ops.paused}
      onclick={() => setPaused(!ops.paused)}
    >
      {ops.paused ? "Resume" : "Pause"}
    </button>
  {/snippet}
</TopBar>

<p role="status" class="sr-only">{announcement}</p>

<HealthStrip executors={ops.executors} runs={ops.runs} />

{#if showLaunch && !openRunId}
  <LaunchForm
    refreshNow={() => refresh?.()}
    onClose={() => (showLaunch = false)}
  />
{/if}

<main aria-live="off">
  {#if openRunId && ops.openRun}
    <RunDetail
      detail={ops.openRun}
      {now}
      onBack={() => (location.hash = "#/")}
      refreshNow={() => refresh?.()}
    />
  {:else}
    <RunList
      runs={ops.runs}
      counts={ops.counts}
      total={ops.total}
      filters={ops.filters}
      loading={ops.loading}
      {now}
      onToggleStatus={toggleStatus}
      onClearFilters={() => (location.hash = "#/")}
      onLoadMore={loadMore}
      onOpen={(id) => (location.hash = `#/r/${id}`)}
    />
  {/if}
</main>
