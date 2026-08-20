import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// svelte-check resolves the compiler options from here, not from
// vite.config.ts. `vitePreprocess` is what lets `<script lang="ts">` work.
export default {
  preprocess: vitePreprocess(),
};
