<script lang="ts">
  import { Database, HardDrive, Layers } from "@lucide/svelte";

  import Badge from "../lib/components/ui/Badge.svelte";
  import type { LineageNode } from "./api";

  let { upstream }: { upstream: LineageNode[] } = $props();

  // Datasets are the non-derived end of the closure: everything an indicator
  // ultimately reads, however many indicators sit between.
  const datasets = $derived(
    upstream.filter((n) => n.kind !== "derived").sort((a, b) => a.asset_key.localeCompare(b.asset_key)),
  );

  const icon = (kind: string) => (kind === "external" ? HardDrive : kind === "source" ? Database : Layers);

  const stateTone = (state: string | null) =>
    state === "fresh" ? "fresh" : state === "stale" ? "stale" : "partial";

  // A dataset with no recorded version gets no badge rather than an "absent"
  // one. Absent is the default state of a graph nobody has published into, so
  // labelling it everywhere shouted a non-event on every row and drowned the
  // states that do mean something.
  const shown = (state: string | null) =>
    state === "fresh" || state === "stale" || state === "partial";
</script>

{#if datasets.length === 0}
  <p class="text-xs text-muted-foreground">
    No datasets recorded upstream. Seed the asset graph to populate them.
  </p>
{:else}
  <ul class="flex flex-col gap-1.5">
    {#each datasets as node (node.asset_key)}
      {@const Icon = icon(node.kind)}
      <li class="flex items-center gap-2.5 rounded border border-line bg-surface-raised px-3 py-2">
        <Icon class="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span class="min-w-0 flex-1">
          <span class="block truncate font-mono text-[12px]">{node.asset_key}</span>
          <span class="block font-mono text-[10px] text-muted-foreground">
            {node.kind} · {node.depth} hop{node.depth === 1 ? "" : "s"} upstream
          </span>
        </span>
        {#if shown(node.state)}
          <Badge tone={stateTone(node.state)} title={`Freshness: ${node.state}`}>
            {node.state}
          </Badge>
        {/if}
      </li>
    {/each}
  </ul>

{/if}
