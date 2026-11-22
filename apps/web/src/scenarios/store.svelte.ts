/**
 * The scenario workspace's state, and every request that changes it.
 *
 * Components render `ws` and call the actions below; nothing else fetches.
 * The rule the whole module keeps: **a response lands only on the selection
 * that asked for it.** Each action captures the scenario it acts for and a
 * selection token, and drops its result when either has moved — a slow rule
 * load, map refresh or run for scenario A must never be drawn over B.
 */
import type { Feature, Polygon } from "geojson";

import {
  addRule,
  createScenario,
  deleteRule as deleteRuleRequest,
  deleteScenario,
  getIndicator,
  getIndicatorStats,
  getScenario,
  listAois,
  listReachableIndicators,
  listScenarios,
  recomputeScenario,
  updateRule,
  type AOIRow,
  type ReachableIndicator,
  type RecomputeResponse,
  type ScenarioListRow,
} from "../api";
import { setActiveAoiId, setKnownAois, subscribe as subscribeToAoi } from "../aoi";
import { ApiError } from "../http";
import {
  DENSITY_INDICATOR_SLUG,
  FLOORS_INDICATOR_SLUG,
  densityBoundOf,
  refusalText,
  ruleInputs,
  rulesFromRows,
  validateEdit,
  type Rule,
  type ScenarioOperation,
} from "../scenario";
import { cellValue } from "./derive";
import { followRun } from "./follow";

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

/** The numbers a rule sets, as the rule box edits them. Null: the rule does
 *  not set it, and every block keeps its own value. */
export interface RuleValues {
  levels: number | null;
  unitSize: number | null;
  peoplePerHousehold: number | null;
  /** Share of the block built on, `0 < c ≤ 1`. */
  lotCoverage: number | null;
}

/** A new rule's box starts empty. Pre-filled numbers were what flattened an
 *  area: a planner changing household size also set every block's storeys,
 *  unit size and coverage to the same values. */
export const EMPTY_RULE: RuleValues = {
  levels: null,
  unitSize: null,
  peoplePerHousehold: null,
  lotCoverage: null,
};

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
  /** The selected scenario's rules, in "later wins" order: the server's. */
  rules: Rule[];
  /** Runs this page enqueued and is following, by scenario. A run is
   *  followed to its end whichever row is selected, so its row refreshes
   *  when it lands; only what it says and redraws is for the selection. */
  following: Record<string, { runId: string; status: string }>;
  message: Message | null;
  /** The area drawn on the map, which the next rule is written over. */
  drawn: Feature<Polygon> | null;
  densityValidMax: number | null;
  floorsValidMax: number | null;
  /** An apply, save, delete or recompute is in flight (it ends when its run
   *  lands). One at a time: the box's buttons are disabled meanwhile. */
  busy: boolean;
  /** Why the open rule box's last action was refused, if it was. */
  boxError: { field: string | null; text: string } | null;
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
  message: null,
  drawn: null,
  densityValidMax: null,
  floorsValidMax: null,
  busy: false,
  boxError: null,
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
    // nothing here and its overlay is scoped to the wrong city. Even mid
    // gesture: a held selection put A's rules on B's map, and the next rule
    // went into A. The gesture itself keeps its own scenario id.
    select(null, { duringGesture: true });
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
    ws.rules = rulesFromRows(detail.rules);
  } catch (err) {
    if (token === selection) await refused(err, "list");
  }
}

/** Show `scenarioId` (null: the baseline city). */
export function select(
  scenarioId: string | null,
  options: { duringGesture?: boolean } = {},
): void {
  if (scenarioId === ws.selectedId) return;
  // A gesture holds the selection until it ends: switching mid-recompute left
  // its box, shape and refusal on the other scenario, and the next Apply
  // wrote there. Only the gesture itself (creating, or undoing, a scenario)
  // and a pilot switch may move it.
  if (ws.busy && !options.duringGesture) return;
  ++selection;
  const token = selection;
  ws.selectedId = scenarioId;
  ws.rules = [];
  if (scenarioId === null) {
    ws.mode = "value";
    return;
  }
  // The change against the baseline is what a scenario with rules is for;
  // one with none has nothing to show there.
  void loadRules(scenarioId, token).then(() => {
    if (token === selection) ws.mode = ws.rules.length > 0 ? "delta" : "value";
  });
}

/**
 * Select what a link names, once the pilot's scenarios are known.
 *
 * A scenario on another pilot switches the page to that pilot and is selected
 * once its scenarios are loaded: a shared link carries the scenario, and the
 * scenario knows its pilot, so the reader's last pilot must not decide. One
 * that is gone selects nothing and says so.
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
  if (ws.scenarios.some((s) => s.id === route.scenarioId)) {
    select(route.scenarioId);
    return;
  }
  const myPilot = pilot;
  const scenarioId = route.scenarioId;
  void getScenario(scenarioId)
    .then((row) => {
      if (myPilot !== pilot) return;
      if (row.aoi_id && row.aoi_id !== ws.aoiId && ws.aois.some((a) => a.aoi_id === row.aoi_id)) {
        pendingRoute = route;
        setActiveAoiId(row.aoi_id);
        return;
      }
      select(null);
      say("That scenario has no pilot this page can show.", "bad");
    })
    .catch(() => {
      if (myPilot !== pilot) return;
      select(null);
      say("That scenario is not there any more — it may have been deleted.", "bad");
    });
}

export function setIndicator(slug: string): void {
  if (!ws.indicators.some((i) => i.slug === slug)) return;
  ws.slug = slug;
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
    if (!ws.aoiId) return;
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
        `${removed.results_deleted} results deleted.`,
    );
  } catch (err) {
    await refused(err, "delete");
  }
}


// ── editing on the map: one call per gesture ─────────────────────────────
//
// Apply, Save and Delete each write the rule — which the server materialises
// in the same transaction and then recomputes — and wait for the run, so the
// map and the table show what the rules say. A refusal (4xx) stored nothing,
// so there is nothing to undo: the box says why and keeps what was typed.
// Anything else (the recompute not queued, a lost response) may have stored
// the rule, so the page re-reads the rules and says what happened instead.

const HOUSING_UNITS = "edificability_block_housing_units";
const POPULATION = "edificability_block_population";

type Landing =
  | { outcome: "landed"; status: string }
  | { outcome: "failed" | "unknown" };

/** Whether the server refused the request and so stored nothing. */
const isRefusal = (err: unknown): boolean =>
  err instanceof ApiError && err.response.status >= 400 && err.response.status < 500;

/** Follow the run a write or recompute enqueued, until it lands. */
async function follow(scenarioId: string, result: RecomputeResponse): Promise<Landing> {
  void loadScenarios();
  if (result.run_id === null) return { outcome: "landed", status: "succeeded" };
  const runId = result.run_id;
  stopFollowing(scenarioId);
  ws.following[scenarioId] = { runId, status: result.run_status };
  return new Promise<Landing>((resolve) => {
    const finish = (landing: Landing): void => {
      followers.delete(scenarioId);
      delete ws.following[scenarioId];
      // The row's status comes from the list, which was read while the run
      // was still going; read it again now that it is not.
      void loadScenarios();
      resolve(landing);
    };
    const cancel = followRun(runId, {
      progress(status) {
        ws.following[scenarioId] = { runId, status };
      },
      landed: (run) => finish({ outcome: "landed", status: run.status }),
      failed: () => finish({ outcome: "failed" }),
      unreadable: () => finish({ outcome: "unknown" }),
      handedOver: () => finish({ outcome: "unknown" }),
    });
    // Cancelled from outside (a pilot switch): the gesture is over either way,
    // and a promise left pending would hold `busy` forever.
    followers.set(scenarioId, () => {
      cancel();
      finish({ outcome: "unknown" });
    });
  });
}

/** After a run: the row, the rules and the map, and the change view. */
async function afterLanding(scenarioId: string, landing: Landing, done: string): Promise<void> {
  void refreshRow(scenarioId);
  if (ws.selectedId !== scenarioId) return;
  await loadRules(scenarioId, selection);
  refreshMap();
  if (landing.outcome === "landed" && landing.status === "partial") {
    ws.mode = "delta";
    say(`${done} Some indicators failed to recompute; the Runs page has the detail.`, "bad");
  } else if (landing.outcome === "landed") {
    ws.mode = "delta";
    say(`${done} The map shows the change against the baseline city.`);
  } else if (landing.outcome === "failed") {
    say(`${done} The recompute failed; the Runs page has the detail.`, "bad");
  } else {
    say(`${done} The recompute is still running — the Runs page follows it.`);
  }
}

/** A write that was not refused but did not answer cleanly: what stands is
 *  whatever the server has, so read it back and say what happened. */
async function writeUncertain(scenarioId: string, err: unknown, operation: ScenarioOperation): Promise<void> {
  await loadScenarios();
  if (ws.selectedId === scenarioId) {
    await loadRules(scenarioId, selection);
    refreshMap();
  }
  void refreshRow(scenarioId);
  say(await refusalText(err, operation), "bad");
}

const boxRefusal = async (err: unknown, operation: ScenarioOperation): Promise<void> => {
  const text = await refusalText(err, operation);
  let field: string | null = null;
  if (err instanceof ApiError) {
    try {
      const body = (await err.response.clone().json()) as { detail?: { field?: unknown } };
      if (typeof body?.detail?.field === "string") field = body.detail.field;
    } catch {
      // Not JSON: no field to point at.
    }
  }
  ws.boxError = { field, text };
};

function precheck(values: RuleValues): boolean {
  const refusal = validateEdit({
    ...values,
    densityValidMax: ws.densityValidMax,
    floorsValidMax: ws.floorsValidMax,
  });
  if (!refusal) return true;
  // The message names the field in parentheses when it is about one field;
  // the storey and density ceilings are about storeys.
  const field = /\((\w+)\)/.exec(refusal)?.[1] ?? (/storeys/.test(refusal) ? "levels" : null);
  ws.boxError = { field, text: refusal };
  return false;
}

/**
 * Write a new rule over `area` and show its effect. On the baseline city
 * (no scenario selected) the scenario is created first, named `scenarioName`.
 * Returns whether the gesture is over (the box may close).
 */
export async function applyNewRule(
  area: Feature<Polygon>,
  values: RuleValues,
  scenarioName?: string,
): Promise<boolean> {
  if (ws.busy) return false;
  ws.boxError = null;
  if (!precheck(values)) return false;
  let scenarioId = ws.selectedId;
  if (scenarioId === null && !(scenarioName ?? "").trim()) {
    ws.boxError = { field: "name", text: "Give the new scenario a name." };
    return false;
  }
  ws.busy = true;
  try {
    if (scenarioId === null) {
      if (!ws.projectId || !ws.aoiId) {
        ws.boxError = { field: null, text: "No pilot is active, so there is no project to add a scenario to." };
        return false;
      }
      // The scenario and its first rule are one request: a refused rule
      // leaves no scenario behind, so there is nothing to undo.
      let created;
      try {
        created = await createScenario({
          project_id: ws.projectId,
          name: (scenarioName ?? "").trim(),
          aoi_id: ws.aoiId,
          rule: { area, inputs: ruleInputs(values) },
        });
      } catch (err) {
        if (!isRefusal(err)) {
          await loadScenarios();
          say(await refusalText(err, "create"), "bad");
          return true;
        }
        await boxRefusal(err, "create");
        return false;
      }
      await loadScenarios();
      select(created.id, { duringGesture: true });
      if (created.first_rule === null) return true;
      await afterLanding(created.id, await follow(created.id, created.first_rule), "Rule added.");
      return true;
    }
    let result;
    try {
      result = await addRule(scenarioId, { area, inputs: ruleInputs(values) });
    } catch (err) {
      if (!isRefusal(err)) {
        await writeUncertain(scenarioId, err, "rule");
        return true;
      }
      await boxRefusal(err, "rule");
      return false;
    }
    await afterLanding(scenarioId, await follow(scenarioId, result), "Rule added.");
    return true;
  } finally {
    ws.busy = false;
  }
}

/**
 * Change a rule's numbers, and its area when `area` is given, in place, and
 * show the effect. A refusal changes nothing on the server. Returns whether
 * the gesture is over.
 */
export async function saveRule(
  rule: Rule,
  area: Feature<Polygon> | null,
  values: RuleValues,
): Promise<boolean> {
  const scenarioId = ws.selectedId;
  if (ws.busy || scenarioId === null) return false;
  ws.boxError = null;
  if (!precheck(values)) return false;
  ws.busy = true;
  try {
    let result;
    try {
      result = await updateRule(scenarioId, rule.id, {
        ...(area ? { area } : {}),
        inputs: ruleInputs(values),
      });
    } catch (err) {
      if (!isRefusal(err)) {
        await writeUncertain(scenarioId, err, "rule");
        return true;
      }
      await boxRefusal(err, "rule");
      return false;
    }
    await afterLanding(scenarioId, await follow(scenarioId, result), "Rule saved.");
    return true;
  } finally {
    ws.busy = false;
  }
}

/** Delete a rule — one request — and show the effect. Returns whether the
 *  gesture is over. */
export async function deleteRule(rule: Rule): Promise<boolean> {
  const scenarioId = ws.selectedId;
  if (ws.busy || scenarioId === null) return false;
  ws.boxError = null;
  ws.busy = true;
  try {
    let result;
    try {
      result = await deleteRuleRequest(scenarioId, rule.id);
    } catch (err) {
      if (!isRefusal(err)) {
        await writeUncertain(scenarioId, err, "deleteRule");
        return true;
      }
      await boxRefusal(err, "deleteRule");
      return false;
    }
    await afterLanding(scenarioId, await follow(scenarioId, result), "Rule deleted.");
    return true;
  } finally {
    ws.busy = false;
  }
}

/** Recompute the selected scenario: what a row whose recompute failed, or
 *  was never queued, offers. */
export async function recomputeSelected(): Promise<void> {
  const scenarioId = ws.selectedId;
  if (ws.busy || scenarioId === null) return;
  ws.busy = true;
  try {
    let result;
    try {
      result = await recomputeScenario(scenarioId);
    } catch (err) {
      await loadScenarios();
      await refused(err, "recompute");
      return;
    }
    await afterLanding(scenarioId, await follow(scenarioId, result), "Recomputed.");
  } finally {
    ws.busy = false;
  }
}

/** Total homes and people, baseline and now, for the top bar. */
export function headline(scenarioId: string | null): {
  homes: { base: number | null; now: number | null };
  people: { base: number | null; now: number | null };
} {
  const cell = (row: string, slug: string): number | null => ws.cells[row]?.[slug]?.value ?? null;
  const now = scenarioId ?? BASELINE;
  return {
    homes: { base: cell(BASELINE, HOUSING_UNITS), now: cell(now, HOUSING_UNITS) },
    people: { base: cell(BASELINE, POPULATION), now: cell(now, POPULATION) },
  };
}
