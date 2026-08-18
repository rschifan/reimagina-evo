<script lang="ts">
  import { Boxes } from "@lucide/svelte";

  import { cn } from "./lib/utils";

  // Phase 3 is the build system: this shell exists to prove both bundles
  // emit and the toolchain works end to end. The index view lands next.
  let mounted = $state(false);
  $effect(() => {
    mounted = true;
  });
</script>

<main class="mx-auto max-w-5xl px-6 py-12">
  <header class="flex items-center gap-3">
    <Boxes class="size-6 text-interactive" aria-hidden="true" />
    <h1 class="text-2xl font-semibold tracking-tight">Indicator catalog</h1>
  </header>
  <p class={cn("mt-3 max-w-prose text-sm", "text-muted-foreground")}>
    Browse the indicator catalog: definitions, lineage, freshness and the
    datasets each indicator is computed from.
  </p>
  <p class="mt-8 text-xs text-muted-foreground" data-testid="boot">
    {mounted ? "Ready." : "Starting…"}
  </p>
</main>
