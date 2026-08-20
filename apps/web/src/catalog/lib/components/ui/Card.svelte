<script lang="ts">
  import type { Snippet } from "svelte";

  import { cn } from "../../utils";

  let {
    title,
    class: klass,
    children,
    action,
  }: {
    title?: string;
    class?: string;
    children: Snippet;
    action?: Snippet;
  } = $props();
</script>

<section class={cn("rounded-lg border border-line bg-surface", klass)}>
  {#if title}
    <header class="flex items-center gap-3 border-b border-line px-4 py-2.5">
      <h2 class="font-mono text-[10px] tracking-[0.11em] text-muted-foreground uppercase">
        {title}
      </h2>
      {#if action}
        <div class="ml-auto">{@render action()}</div>
      {/if}
    </header>
  {/if}
  <div class="p-4">{@render children()}</div>
</section>
