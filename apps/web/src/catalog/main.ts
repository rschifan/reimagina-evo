/**
 * Catalog page entry.
 *
 * Svelte 5 mounts through `mount()`; `new App({ target })` is the Svelte 4
 * API and throws under runes mode.
 */
import { mount } from "svelte";

import App from "./App.svelte";
import "./catalog.css";

const target = document.getElementById("catalog-root");
if (!target) throw new Error("#catalog-root is missing from catalog.html");

export default mount(App, { target });
