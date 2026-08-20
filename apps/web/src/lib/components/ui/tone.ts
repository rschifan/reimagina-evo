/**
 * The semantic tones a Badge can carry.
 *
 * Extracted from Badge.svelte's instance `<script>` so non-component code can
 * name it: a page's pure `derive.ts` maps a backend status onto a tone, and
 * typing that map as `Record<Status, Tone>` is what makes the compiler prove
 * the mapping is total. A type declared inside a `.svelte` instance script is
 * not importable at all, so this file is the whole mechanism.
 */
export type Tone =
  "neutral" | "fresh" | "stale" | "partial" | "accent" | "error";
