/**
 * Ops page entry.
 *
 * Svelte 5 mounts through `mount()`; `new App({ target })` is the Svelte 4
 * API and throws under runes mode.
 */
import { mount } from "svelte";

import App from "./App.svelte";
import "./ops.css";

const target = document.getElementById("ops-root");
if (!target) throw new Error("#ops-root is missing from ops.html");

export default mount(App, { target });
