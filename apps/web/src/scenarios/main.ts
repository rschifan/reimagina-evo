/**
 * Scenario workspace entry: a page of its own for authoring and comparing
 * scenarios, showing only the indicators a scenario rule can change.
 *
 * Svelte 5 mounts through `mount()`; `new App({ target })` is the Svelte 4
 * API and throws under runes mode.
 */
import { mount } from "svelte";

import "maplibre-gl/dist/maplibre-gl.css";

import App from "./App.svelte";
import "./scenarios.css";

const target = document.getElementById("scenarios-root");
if (!target) throw new Error("#scenarios-root is missing from scenarios.html");

export default mount(App, { target });
