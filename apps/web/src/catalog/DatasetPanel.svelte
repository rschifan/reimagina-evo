<script lang="ts">
  import { Database, HardDrive, Layers } from "@lucide/svelte";

  import Badge from "./lib/components/ui/Badge.svelte";
  import type { LineageNode } from "./api";

  let { upstream }: { upstream: LineageNode[] } = $props();

  // Datasets are the non-derived end of the closure: everything an indicator
  // ultimately reads, however many indicators sit between.
  const datasets = $derived(
    upstream.filter((n) => n.kind !== "derived").sort((a, b) => a.asset_key.localeCompare(b.asset_key)),
  );

  const icon = (kind: string) => (kind === "external" ? HardDrive : kind === "source" ? Database : Layers);

  const stateTone = (state: string | null) =>
    state === "fresh" ? "fresh" : state === "stale" ? "stale" : state === "partial" ? "partial" : "absent";
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
        <Badge tone={stateTone(node.state)} title={node.state === "absent"
          ? "No version recorded yet — nothing has published one for this asset."
          : `Freshness: ${node.state}`}>
          {node.state ?? "unknown"}
        </Badge>
      </li>
    {/each}
  </ul>

  {#if datasets.every((d) => d.state === "absent")}
    <p class="mt-3 text-[11px] leading-relaxed text-muted-foreground">
      Every asset reads <span class="font-mono">absent</span>: the graph is seeded
      but no version or materialization has been published yet. The lineage above
      is real; the freshness column will stay <span class="font-mono">absent</span>
      until an ingest publishes a version and a compute records a materialization.
    </p>
  {/if}
{/if}
