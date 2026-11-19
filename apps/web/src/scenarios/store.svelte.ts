/**
 * The scenario workspace's state, and every request that changes it.
 *
 * Components render `ws` and call the actions below; nothing else fetches.
 * The rule the whole module keeps: **a response lands only on the selection
 * that asked for it.** Each action captures the scenario it acts for and a
 * selection token, and drops its result when either has moved — a slow rule
 * load, comparison or run for scenario A must never be drawn over B.
 */
import type { Feature, Polygon } from "geojson";

import {
  addScenarioEdits,
  createScenario,
  deleteScenario,
  getComparison,
  getIndicator,
  getIndicatorStats,
  getScenario,
  listAois,
  listReachableIndicators,
  listScenarios,
  recomputeScenario,
  type AOIRow,
  type ReachableIndicator,
  type ScenarioListRow,
} from "../api";
import { setKnownAois, subscribe as subscribeToAoi } from "../aoi";
import {
  DENSITY_INDICATOR_SLUG,
  FLOORS_INDICATOR_SLUG,
  comparisonHeadline,
  densityBoundOf,
  groupEditsByArea,
  refusalText,
  validateEdit,
  type ComparisonHeadline,
  type EditGroup,
  type ScenarioOperation,
} from "../scenario";
import { cellValue } from "./derive";
import { followRun, removeRule } from "./follow";

/** The table's key for the baseline city's row. */
export const BASELINE = "baseline";

export interface Cell {
  value: number | null;
  /** Why this one cell could not be read; the rest of the table still renders. */
  error: string | null;
}

export interface Message {
  text: string;
  tone: "ok" | "bad";
}

export interface WorkspaceState {
  /** Every pilot, for the picker. The active one is `aoiId`. */
  aois: AOIRow[];
  aoiId: string | null;
  projectId: string | null;
  indicators: ReachableIndicator[];
  scenarios: ScenarioListRow[];
  /** Keyed by scenario id (or {@link BASELINE}), then indicator slug. */
  cells: Record<string, Record<string, Cell>>;
  selectedId: string | null;
  slug: string | null;
  mode: "value" | "delta";
  rules: EditGroup[];
  /** Runs this page enqueued and is following, by scenario. A run is
   *  followed to its end whichever row is selected, so its row refreshes
   *  when it lands; only what it says and redraws is for the selection. */
  following: Record<string, { runId: string; status: string }>;
  comparison: ComparisonHeadline | null;
  message: Message | null;
  /** The area drawn on the map, which the next rule is written over. */
  drawn: Feature<Polygon> | null;
  densityValidMax: number | null;
  floorsValidMax: number | null;
  /** False until the route a page opened with has been applied. Whatever
   *  writes the selection back into the URL waits for it — before then the
   *  selection is empty only because the pilot is still loading, and writing
   *  that would erase the link the page was opened from. */
  routeReady: boolean;
}

export const ws: WorkspaceState = $state({
  aois: [],
  aoiId: null,
  projectId: null,
  indicators: [],
  scenarios: [],
  cells: {},
  selectedId: null,
  slug: null,
  mode: "value",
  rules: [],
  following: {},
  comparison: null,
  message: null,
  drawn: null,
  densityValidMax: null,
  floorsValidMax: null,
  routeReady: false,
});

/** Bumped on every selection change; an async action holding an older one
 *  is answering for a selection that is gone. */
let selection = 0;
/** Bumped on every pilot change, for the same reason. */
let pilot = 0;
/** One follower per scenario; a second recompute replaces the first. */
const followers = new Map<string, () => void>();
let pendingRoute: { scenarioId: string | null; slug: string | null } | null = null;
const mapRefreshListeners = new Set<() => void>();

const say = (text: string, tone: Message["tone"] = "ok"): void => {
  ws.message = { text, tone };
};

const refused = async (err: unknown, operation: ScenarioOperation): Promise<void> => {
  say(await refusalText(err, operation), "bad");
};

const scenarioName = (id: string): string =>
  ws.scenarios.find((s) => s.id === id)?.name ?? "the scenario";

/** Fire `onMapRefresh` listeners: the map rebuilds its source from them. */
function refreshMap(): void {
  for (const listener of mapRefreshListeners) listener();
}

/** Called when a followed run lands, so the map drops the tiles cached
 *  before it. Returns an unsubscribe. */
export function onMapRefresh(listener: () => void): () => void {
  mapRefreshListeners.add(listener);
  return () => mapRefreshListeners.delete(listener);
}

/**
 * Load what the page needs once, and follow the active pilot from then on.
 *
 * The bounds come from the catalog (`getIndicator`), never compiled in — see
 * `scenario.ts`. A bound that cannot be read is no precheck, not a guess: the
 * server refuses with the numbers either way.
 */
export async function boot(): Promise<void> {
  subscribeToAoi((aoi) => void onPilot(aoi));
  const [indicators, aois] = await Promise.all([
    listReachableIndicators().catch(async (err) => {
      await refused(err, "list");
      return [] as ReachableIndicator[];
    }),
    listAois().catch(async (err) => {
      await refused(err, "list");
      return [] as AOIRow[];
    }),
  ]);
  ws.indicators = indicators;
  // Open on an indicator: an overlay with nothing picked is the one view
  // indistinguishable from a broken map. A route naming another wins later.
  ws.slug ??= indicators[0]?.slug ?? null;
  ws.aois = aois;
  for (const [slug, field] of [
    [DENSITY_INDICATOR_SLUG, "densityValidMax"],
    [FLOORS_INDICATOR_SLUG, "floorsValidMax"],
  ] as const) {
    void getIndicator(slug)
      .then((indicator) => (ws[field] = densityBoundOf(indicator)))
      .catch((err) => console.warn(`${slug} ceiling unavailable; the server will check it`, err));
  }
  setKnownAois(aois);
}

async function onPilot(aoi: AOIRow | null): Promise<void> {
  const changed = (aoi?.aoi_id ?? null) !== ws.aoiId;
  ws.aoiId = aoi?.aoi_id ?? null;
  ws.projectId = aoi?.project_id ?? null;
  if (!changed) return;
  const myPilot = ++pilot;
  // Runs of the previous pilot's scenarios: their rows are no longer shown.
  stopAllFollowing();
  if (ws.selectedId !== null) {
    // A scenario belongs to one pilot; carried across, its rules select
    // nothing here and its overlay is scoped to the wrong city.
    select(null);
    say("Scenario cleared — a scenario belongs to one pilot.");
  }
  ws.cells = {};
  await loadScenarios();
  if (myPilot !== pilot) return;
  if (pendingRoute) {
    const route = pendingRoute;
    pendingRoute = null;
    applyRoute(route);
  }
  await refreshTable();
}

async function loadScenarios(): Promise<void> {
  const aoiId = ws.aoiId;
  if (!aoiId) {
    ws.scenarios = [];
    return;
  }
  try {
    const rows = await listScenarios({ aoiId });
    if (aoiId !== ws.aoiId) return;
    ws.scenarios = [...rows].sort((a, b) => a.name.localeCompare(b.name));
  } catch (err) {
    await refused(err, "list");
  }
}

/** Run `tasks` at most `limit` at a time. */
async function pooled(tasks: Array<() => Promise<void>>, limit: number): Promise<void> {
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < tasks.length) await tasks[next++]();
  };
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
}

/** One row's cells: the baseline when `scenarioId` is null. */
function rowTasks(scenarioId: string | null): Array<() => Promise<void>> {
  const aoiId = ws.aoiId;
  const key = scenarioId ?? BASELINE;
  const myPilot = pilot;
  return ws.indicators.map((indicator) => async () => {
    let cell: Cell;
    try {
      const stats = await getIndicatorStats(indicator.slug, {
        aoi_id: aoiId ?? undefined,
        scenario_id: scenarioId ?? undefined,
      });
      cell = { value: cellValue(stats, indicator.statistic), error: null };
    } catch (err) {
      cell = { value: null, error: err instanceof Error ? err.message : String(err) };
    }
    if (myPilot !== pilot) return;
    ws.cells[key] = { ...(ws.cells[key] ?? {}), [indicator.slug]: cell };
  });
}

/**
 * Every cell of the table: the baseline row and each scenario's.
 *
 * ponytail: one request per cell (rows × reachable indicators), six at a
 * time — a batch endpoint when a pilot holds dozens of scenarios.
 */
export async function refreshTable(): Promise<void> {
  if (!ws.aoiId) return;
  const rows = [null, ...ws.scenarios.map((s) => s.id)];
  await pooled(rows.flatMap(rowTasks), 6);
}

async function refreshRow(scenarioId: string): Promise<void> {
  await pooled(rowTasks(scenarioId), 6);
}

function stopFollowing(scenarioId: string): void {
  followers.get(scenarioId)?.();
  followers.delete(scenarioId);
  delete ws.following[scenarioId];
}

function stopAllFollowing(): void {
  for (const scenarioId of [...followers.keys()]) stopFollowing(scenarioId);
}

async function loadRules(scenarioId: string, token: number): Promise<void> {
  try {
    const detail = await getScenario(scenarioId);
    if (token !== selection) return;
    ws.rules = groupEditsByArea(detail.edits);
  } catch (err) {
    if (token === selection) await refused(err, "list");
  }
}

/** Show `scenarioId` (null: the baseline city). */
export function select(scenarioId: string | null): void {
  if (scenarioId === ws.selectedId) return;
  ++selection;
  const token = selection;
  ws.selectedId = scenarioId;
  ws.comparison = null;
  ws.rules = [];
  if (scenarioId === null) {
    ws.mode = "value";
    return;
  }
  void loadRules(scenarioId, token);
}

/**
 * Select what a link names, once the pilot's scenarios are known.
 *
 * A scenario that is gone, or belongs to another pilot, selects nothing and
 * says so — rather than a 404 on every request the page would then make.
 */
export function applyRoute(route: { scenarioId: string | null; slug: string | null }): void {
  if (ws.aoiId === null) {
    pendingRoute = route;
    return;
  }
  ws.routeReady = true;
  if (route.slug && ws.indicators.some((i) => i.slug === route.slug)) ws.slug = route.slug;
  if (route.scenarioId === null) {
    select(null);
    return;
  }
  if (!ws.scenarios.some((s) => s.id === route.scenarioId)) {
    select(null);
    say("That scenario is not on this pilot — it may have been deleted.", "bad");
    return;
  }
  select(route.scenarioId);
}

export function setIndicator(slug: string): void {
  if (!ws.indicators.some((i) => i.slug === slug)) return;
  ws.slug = slug;
  ws.comparison = null;
}

export function setMode(mode: "value" | "delta"): void {
  ws.mode = ws.selectedId === null ? "value" : mode;
}

export async function create(name: string): Promise<void> {
  const trimmed = name.trim();
  if (!trimmed) {
    say("Give the scenario a name.", "bad");
    return;
  }
  if (!ws.projectId) {
    say("No pilot is active, so there is no project to create a scenario in.", "bad");
    return;
  }
  try {
    const created = await createScenario({
      project_id: ws.projectId,
      name: trimmed,
      aoi_id: ws.aoiId,
    });
    await loadScenarios();
    select(created.id);
    say(`Created ${created.name}. Draw an area on the map and apply the numbers.`);
    void refreshRow(created.id);
  } catch (err) {
    await refused(err, "create");
  }
}

export async function applyRule(
  area: Feature<Polygon> | null,
  values: { levels: number; unitSize: number; peoplePerHousehold: number },
): Promise<void> {
  const scenarioId = ws.selectedId;
  if (!scenarioId) return;
  if (!area) {
    say("Draw an area on the map first — a rule is an area.", "bad");
    return;
  }
  const refusal = validateEdit({
    ...values,
    densityValidMax: ws.densityValidMax,
    floorsValidMax: ws.floorsValidMax,
  });
  if (refusal) {
    say(refusal, "bad");
    return;
  }
  const token = selection;
  try {
    const written = await addScenarioEdits(scenarioId, {
      area,
      levels: values.levels,
      unit_size: values.unitSize,
      people_per_household: values.peoplePerHousehold,
    });
    await loadScenarios();
    if (token !== selection) return;
    say(
      `${written.edits.length} edits written over the drawn area on ` +
        `${written.feature_type}. Recompute to materialise them.`,
    );
    await loadRules(scenarioId, token);
  } catch (err) {
    if (token === selection) await refused(err, "edits");
  }
}

/** Remove one rule — every edit of it — from the scenario it belongs to. */
export async function removeRuleGroup(editIds: string[]): Promise<void> {
  const scenarioId = ws.selectedId;
  if (!scenarioId) return;
  const token = selection;
  const { removed, error } = await removeRule(scenarioId, editIds);
  await loadScenarios();
  // The removal finishes regardless; what it says is for the scenario it
  // removed from, and another one may be on screen now.
  if (token !== selection) return;
  if (error) await refused(error, "removeEdit");
  else
    say(
      "Rule removed. Recompute to withdraw the overrides it wrote — " +
        "removing the rule does not withdraw them on its own.",
    );
  if (error && removed > 0) {
    say(
      `${removed} of ${editIds.length} edits removed; the rule is now ` +
        "incomplete and will not compute. Remove the rest.",
      "bad",
    );
  }
  await loadRules(scenarioId, token);
}

export async function recompute(): Promise<void> {
  const scenarioId = ws.selectedId;
  if (!scenarioId) return;
  /** Messages are for the scenario on screen; a run of another says nothing. */
  const shown = (): boolean => ws.selectedId === scenarioId;
  say("Materialising the rules and queueing the recompute…");
  let result;
  try {
    result = await recomputeScenario(scenarioId);
  } catch (err) {
    if (shown()) await refused(err, "recompute");
    return;
  }
  const matched = Object.values(result.matched_per_edit);
  const caught = matched.length > 0 ? Math.max(...matched) : 0;
  const materialised =
    `${caught} block${caught === 1 ? "" : "s"} caught, ` +
    `${result.overrides_written} overrides written, ` +
    `${result.overrides_removed} removed.`;
  void loadScenarios();
  // No edits on the server and nothing withdrawn: the planner pressed
  // Recompute before writing a rule. Say how to write one; "0 blocks caught"
  // reads as a broken compute.
  if (matched.length === 0 && result.overrides_removed === 0 && result.run_id === null) {
    if (shown()) {
      say(
        "This scenario has no rules yet, so nothing changed. Draw an area on the " +
          "map with the tools at its top left, apply the numbers to it, then recompute.",
      );
    }
    return;
  }
  if (result.run_id === null) {
    if (shown()) say(`${materialised} Nothing needed recomputing.`);
    return;
  }
  const runId = result.run_id;
  const short = runId.slice(0, 8);
  // A second recompute of the same scenario supersedes the first follower:
  // left running, the old one would report its run over the new one's.
  stopFollowing(scenarioId);
  ws.following[scenarioId] = { runId, status: result.run_status };
  if (shown()) {
    say(
      `${materialised} Run ${short} is ${result.run_status}` +
        `${result.deduplicated ? " (already in flight)" : ""}…`,
    );
  }
  const finish = (): void => {
    followers.delete(scenarioId);
    delete ws.following[scenarioId];
    void loadScenarios();
  };
  followers.set(
    scenarioId,
    followRun(runId, {
      progress(status) {
        ws.following[scenarioId] = { runId, status };
        if (shown()) say(`${materialised} Run ${short} is ${status}…`);
      },
      landed(run) {
        finish();
        // The row is refreshed whichever scenario is on screen: the table
        // holds them side by side, and a stale row there is the failure.
        void refreshRow(scenarioId);
        if (!shown()) return;
        ws.comparison = null;
        const failed = run.units.filter((u) => u.status === "failed").length;
        say(
          `Run ${short} ${run.status}` +
            `${failed > 0 ? ` (${failed} indicator${failed === 1 ? "" : "s"} failed)` : ""}. ` +
            "The table and the map now show its results.",
        );
        refreshMap();
      },
      failed(run) {
        finish();
        if (!shown()) return;
        say(
          `Run ${short} ${run.status}. The overrides stand; the indicator results ` +
            "did not change. The Runs page has the detail.",
          "bad",
        );
      },
      unreadable() {
        finish();
        if (shown()) say(`Run ${short} could not be read — follow it on the Runs page.`, "bad");
      },
      handedOver(status) {
        finish();
        if (!shown()) return;
        say(
          `Run ${short} is still ${status}. Follow it on the Runs page; reload ` +
            "this page once it has finished.",
        );
      },
    }),
  );
}

export async function compare(): Promise<void> {
  const scenarioId = ws.selectedId;
  const slug = ws.slug;
  if (!scenarioId) return;
  if (!slug) {
    say("Pick an indicator to compare.", "bad");
    return;
  }
  const token = selection;
  try {
    const response = await getComparison(scenarioId, {
      indicator_slug: slug,
      aoi_id: ws.aoiId,
    });
    if (token !== selection || slug !== ws.slug) return;
    const unit = ws.indicators.find((i) => i.slug === slug)?.unit ?? null;
    ws.comparison = comparisonHeadline(response, unit);
  } catch (err) {
    if (token === selection) await refused(err, "compare");
  }
}

/** Delete the selected scenario. The component asks for confirmation. */
export async function removeScenario(): Promise<void> {
  const scenarioId = ws.selectedId;
  if (!scenarioId) return;
  const name = scenarioName(scenarioId);
  try {
    const removed = await deleteScenario(scenarioId);
    select(null);
    delete ws.cells[scenarioId];
    await loadScenarios();
    say(
      `Deleted ${name}: ${removed.overrides} overrides, ` +
        `${removed.results_superseded} results superseded.`,
    );
  } catch (err) {
    await refused(err, "delete");
  }
}
