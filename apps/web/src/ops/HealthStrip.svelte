<script lang="ts">
  import Badge from "../lib/components/ui/Badge.svelte";
  import type { ExecutorRow, RunRow } from "./api";

  let { executors, runs }: { executors: ExecutorRow[]; runs: RunRow[] } = $props();

  // 3× the 30 s default heartbeat, so one missed beat is not an alarm.
  const FRESH_S = 90;

  const inFlight = $derived(runs.filter((r) => r.status === "running").length);
  const unitsRunning = $derived(runs.reduce((n, r) => n + r.units_running, 0));
  const unitsQueued = $derived(
    runs.reduce((n, r) => n + (r.units_total - r.units_terminal - r.units_running), 0),
  );
</script>

<!--
  Why this strip exists: a queue that is not moving and a queue with no worker
  look identical in the run table. Without this, the first thing an operator
  does on a stalled dashboard is debug the wrong layer.
-->
<div
  class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line/60 px-4 py-2 font-mono text-[11px] text-muted-foreground"
>
  {#if executors.length === 0}
    <!-- Zero rows is the fresh-install case, and an empty strip here would do
         precisely what the strip exists to prevent. -->
    <Badge tone="error">no executor has ever reported</Badge>
    <span>nothing will run until a worker is started</span>
  {:else}
    <Badge tone="accent">{executors.length} executor{executors.length === 1 ? "" : "s"}</Badge>
    {#each executors as ex (ex.executor_id)}
      <Badge
        tone={ex.heartbeat_age_s < FRESH_S ? "fresh" : "error"}
        title={`${ex.executor_id} · ${ex.role}${ex.app_version ? ` · ${ex.app_version}` : ""}`}
      >
        {ex.role}
        {Math.round(ex.heartbeat_age_s)}s
      </Badge>
    {/each}
  {/if}

  <span class="text-line">·</span>
  <span data-numeric>{inFlight} run{inFlight === 1 ? "" : "s"} in flight</span>
  <span data-numeric>{unitsRunning} unit{unitsRunning === 1 ? "" : "s"} running</span>
  <span data-numeric>{unitsQueued} queued</span>
</div>
