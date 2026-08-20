<script lang="ts">
  import type { Snippet } from "svelte";

  import { Boxes } from "@lucide/svelte";

  import { cn } from "./utils";

  /**
   * The one navigation bar every Svelte page shares.
   *
   * These pages are separate Vite entries, not routes — there is no router and
   * no layout file to hang navigation on — so "shared nav" has to mean a shared
   * component that each entry renders itself. Links are plain anchors because
   * crossing entries is a real document load.
   *
   * `actions` is where a page puts its own controls (a refresh stamp, an AOI
   * picker, a pause button); the bar itself stays identical across pages so it
   * reads as one product rather than three.
   */
  let {
    current,
    actions,
  }: {
    current: "map" | "catalog" | "ops";
    actions?: Snippet;
  } = $props();

  const links = [
    { key: "map", href: "/index.html", label: "Map" },
    { key: "catalog", href: "/catalog.html", label: "Catalog" },
    { key: "ops", href: "/ops.html", label: "Runs" },
  ] as const;
</script>

<header
  class="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-surface px-4 py-2.5"
>
  <a href="/index.html" class="flex items-center gap-2">
    <Boxes class="size-5 text-interactive" aria-hidden="true" />
    <span class="text-sm font-semibold tracking-tight">reimagina</span>
  </a>

  <nav aria-label="Sections" class="flex items-center gap-1">
    {#each links as link (link.key)}
      <a
        href={link.href}
        aria-current={link.key === current ? "page" : undefined}
        class={cn(
          "rounded px-2.5 py-1 text-xs transition-colors",
          link.key === current
            ? // Not colour alone: the current page keeps a border and an
              // aria-current, so it survives greyscale and a screen reader.
              "border border-accent/40 bg-accent/10 text-accent"
            : "border border-transparent text-muted-foreground hover:text-foreground",
        )}
      >
        {link.label}
      </a>
    {/each}
  </nav>

  {#if actions}
    <div class="ml-auto flex flex-wrap items-center gap-3">
      {@render actions()}
    </div>
  {/if}
</header>
