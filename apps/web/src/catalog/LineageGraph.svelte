<script lang="ts">
  import { ArrowRight } from "@lucide/svelte";

  import type { DependencyEdge } from "./api";

  let {
    slug,
    name,
    upstream,
    downstream,
  }: {
    slug: string;
    name: string;
    upstream: DependencyEdge[];
    downstream: DependencyEdge[];
  } = $props();

  // Weights are a share of the composite. Showing the sum makes a mistake
  // visible: the constraint is that they add to 1, and a graph that renders
  // 0.2/0.2/0.2/0.3 looks fine until you add it up.
  const weightSum = $derived(
    upstream.reduce((acc, e) => acc + (e.weight ?? 0), 0),
  );
  const weighted = $derived(upstream.some((e) => e.weight !== null));
</script>

{#if upstream.length === 0 && downstream.length === 0}
  <p class="text-xs text-muted-foreground">
    Computed directly from features — it depends on no other indicator, and
    none depends on it.
  </p>
{:else}
  <div class="flex flex-col gap-4">
    {#if upstream.length}
      <div>
        <h3 class="mb-2 font-mono text-[10px] tracking-[0.1em] text-muted-foreground uppercase">
          Inputs · {upstream.length}
        </h3>
        <ul class="flex flex-col gap-1.5">
          {#each upstream as edge (edge.depends_on_slug)}
            <li>
              <a
                href={`#/i/${encodeURIComponent(edge.depends_on_slug)}`}
                class="flex items-center gap-3 rounded border border-line bg-surface-raised px-3 py-2
                       hover:border-accent/50"
              >
                {#if edge.weight !== null}
                  <!-- The weight bar is the share, drawn to scale: four 0.2s
                       and a 0.4 should look like what they are. -->
                  <span class="flex w-14 shrink-0 items-center gap-1.5">
                    <span class="h-1.5 flex-1 overflow-hidden rounded bg-line">
                      <span
                        class="block h-full bg-accent"
                        style={`width:${Math.round((edge.weight ?? 0) * 100)}%`}
                      ></span>
                    </span>
                  </span>
                  <span class="w-8 shrink-0 font-mono text-[11px] text-accent" data-numeric>
                    {edge.weight.toFixed(2)}
                  </span>
                {/if}
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-[13px]">{edge.depends_on_name}</span>
                  <span class="block truncate font-mono text-[10.5px] text-muted-foreground">
                    {edge.depends_on_slug}{edge.depends_on_unit ? ` · ${edge.depends_on_unit}` : ""}
                  </span>
                </span>
                <ArrowRight class="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
              </a>
            </li>
          {/each}
        </ul>

        {#if weighted}
          <p
            class="mt-2 font-mono text-[10.5px] {Math.abs(weightSum - 1) < 1e-9
              ? 'text-muted-foreground'
              : 'text-state-stale'}"
            data-numeric
          >
            weights sum to {weightSum.toFixed(2)}{Math.abs(weightSum - 1) < 1e-9
              ? ""
              : " — expected 1.00"}
          </p>
        {/if}
      </div>
    {/if}

    <div class="flex items-center gap-2 rounded border border-interactive/40 bg-interactive/5 px-3 py-2">
      <span class="min-w-0">
        <span class="block truncate text-[13px] text-interactive">{name}</span>
        <span class="block truncate font-mono text-[10.5px] text-muted-foreground">{slug}</span>
      </span>
    </div>

    {#if downstream.length}
      <div>
        <h3 class="mb-2 font-mono text-[10px] tracking-[0.1em] text-muted-foreground uppercase">
          Feeds · {downstream.length}
        </h3>
        <ul class="flex flex-col gap-1.5">
          {#each downstream as edge (edge.indicator_slug)}
            <li>
              <a
                href={`#/i/${encodeURIComponent(edge.indicator_slug)}`}
                class="flex items-center gap-3 rounded border border-line bg-surface-raised px-3 py-2
                       hover:border-accent/50"
              >
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-[13px]">{edge.indicator_name}</span>
                  <span class="block truncate font-mono text-[10.5px] text-muted-foreground">
                    {edge.indicator_slug}
                  </span>
                </span>
                {#if edge.weight !== null}
                  <span class="shrink-0 font-mono text-[11px] text-muted-foreground" data-numeric>
                    contributes {edge.weight.toFixed(2)}
                  </span>
                {/if}
              </a>
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  </div>
{/if}
