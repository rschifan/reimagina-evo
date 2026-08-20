<script lang="ts">
  import type { Snippet } from "svelte";

  import { cn } from "../../utils";

  let {
    pressed = false,
    count,
    onclick,
    children,
  }: {
    pressed?: boolean;
    count?: number;
    onclick: () => void;
    children: Snippet;
  } = $props();
</script>

<!-- aria-pressed rather than a class is how the map app expresses toggle
     state too; keeping the convention means one mental model across pages. -->
<button
  type="button"
  aria-pressed={pressed}
  {onclick}
  class={cn(
    "inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs transition-colors",
    pressed
      ? "border-accent-strong bg-accent-strong text-white"
      : "border-line bg-surface-raised text-foreground hover:border-accent/50",
  )}
>
  {@render children()}
  {#if count !== undefined}
    <span class={cn("font-mono text-[10px]", pressed ? "text-white/70" : "text-muted-foreground")}>
      {count}
    </span>
  {/if}
</button>
