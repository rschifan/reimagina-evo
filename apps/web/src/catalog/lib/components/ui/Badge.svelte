<script lang="ts">
  import type { Snippet } from "svelte";

  import { cn } from "../../utils";

  type Tone = "neutral" | "fresh" | "stale" | "partial" | "accent";

  let {
    tone = "neutral",
    title,
    class: klass,
    children,
  }: {
    tone?: Tone;
    title?: string;
    class?: string;
    children: Snippet;
  } = $props();

  // Tone carries text as well as colour everywhere it is used: these are
  // operational states, and colour alone fails both a colour-blind reader
  // and a greyscale print-out.
  const tones: Record<Tone, string> = {
    neutral: "border-line text-muted-foreground",
    fresh: "border-state-fresh/40 text-state-fresh bg-state-fresh/10",
    stale: "border-state-stale/40 text-state-stale bg-state-stale/10",
    partial: "border-state-partial/40 text-state-partial bg-state-partial/10",
    accent: "border-accent/40 text-accent bg-accent/10",
  };
</script>

<span
  {title}
  class={cn(
    "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] leading-4 whitespace-nowrap",
    tones[tone],
    klass,
  )}
>
  {@render children()}
</span>
