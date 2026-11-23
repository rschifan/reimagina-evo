// Drive the scenario editor end to end in a real browser and report each step.
//
// ⚠ It WRITES: it creates a scenario, adds, edits, moves and deletes rules,
// and deletes the scenario at the end. Point it only at a scratch stack
// (`test_scenario_flow.py` refuses unless REIMAGINA_E2E_WRITES=1).
//
// What a step checks is what the map paints — `queryRenderedFeatures` on the
// indicator layer at a screen point — because that is where the stale-tile
// and clipping defects lived while the table and the API were right.
const { chromium } = require('playwright');

const WEB = process.env.E2E_WEB || 'http://127.0.0.1:5173';
const API = process.env.E2E_API || 'http://127.0.0.1:8001';
const OUT = process.env.E2E_OUT;
const FILL = 'indicator_overlay__fill';
const RULES = 'scenario-rules-fill';
const NAME = `E2E ${Date.now()}`;
// On a real pilot the blocks are not a grid: `E2E_FOCUS=lon0,lat0,lon1,lat1`
// fits the map to that window and addresses it as an `E2E_GRID` x `E2E_GRID`
// grid of cells (default 4), each probe snapped to a block painted near the
// cell's centre. Without it the synthetic territory's own grid is used.
const FOCUS = process.env.E2E_FOCUS ? process.env.E2E_FOCUS.split(',').map(Number) : null;
const GRID = Number(process.env.E2E_GRID || 4);
const density = (v) => (v.coverage * v.levels * 10000) / v.unitSize;

const report = { steps: [], errors: [], warnings: [], scenarioId: null };
let canvasBox = { x: 0, y: 0 };
const step = (name, ok, detail = {}) => report.steps.push({ name, ok: Boolean(ok), detail });

async function main() {
  const browser = await chromium.launch({
    args: ['--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
  });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  page.on('pageerror', (e) => report.errors.push(String(e)));
  page.on('console', (m) => {
    if (m.type() === 'error') report.errors.push(m.text());
    if (m.type() === 'warning' && !/GL Driver/.test(m.text())) report.warnings.push(m.text());
  });
  try {
    await run(page);
  } catch (err) {
    step('the flow ran to the end', false, { error: String(err && err.stack || err) });
  } finally {
    if (report.scenarioId) {
      await fetch(`${API}/scenarios/${report.scenarioId}`, { method: 'DELETE' }).catch(() => {});
    }
    await browser.close();
    require('fs').writeFileSync(OUT, JSON.stringify(report, null, 2));
  }
}

async function run(page) {
  await page.goto(`${WEB}/scenarios.html`, { waitUntil: 'networkidle' });
  await page.waitForFunction(
    (layers) => {
      const m = window.__scenarioMap;
      return m && layers.every((id) => m.getLayer(id)) && m.loaded();
    },
    [FILL, RULES],
    { timeout: 60_000 },
  );
  await page.getByLabel('Indicator').selectOption({ label: 'Housing Density (potential)' });
  await settled(page);

  // Canvas-relative points, placed where the canvas is *now*: `settled`
  // re-measures it after every gesture, because anything that changes height
  // above the map moves it, and a click aimed at a stale box lands beside the
  // rule it was meant for.
  const at = (x, y) => ({ x: canvasBox.x + x, y: canvasBox.y + y });
  // Fill the canvas with the blocks, then address them by block: `blk(i, j)`
  // is the centre of column i, row j of the grid's bounding box.
  const grid = await fitToBlocks(page);
  const bw = (grid.x1 - grid.x0) / grid.cols;
  const bh = (grid.y1 - grid.y0) / grid.rows;
  const centre = (i, j) => [grid.x0 + (i + 0.5) * bw, grid.y0 + (j + 0.5) * bh];
  // A block painted at (or, on a real pilot, near) the cell's centre: a street
  // there would read as "no feature" and fail a step for nothing.
  const snapped = new Map();
  const blk = (i, j) => snapped.get(`${i},${j}`) || centre(i, j);
  for (let i = 0; i < grid.cols; i += 1) {
    for (let j = 0; j < grid.rows; j += 1) {
      snapped.set(`${i},${j}`, await snap(page, centre(i, j), 0.15 * Math.min(bw, bh)));
    }
  }
  // Nearly whole cells: a probe's block is caught by its interior point, and
  // on a real pilot that point can sit well off the cell's centre.
  const span = (i0, j0, i1, j1) => [
    at(grid.x0 + (i0 + 0.05) * bw, grid.y0 + (j0 + 0.05) * bh),
    at(grid.x0 + (i1 + 0.95) * bw, grid.y0 + (j1 + 0.95) * bh),
  ];
  const click = async ([x, y]) => page.mouse.click(at(x, y).x, at(x, y).y);

  // 1 + 5: draw on the baseline city; Apply names and creates the scenario.
  await drawRectangle(page, ...span(0, 1, 1, 2));
  const box = page.locator('form[aria-label="New rule"]');
  await box.waitFor({ timeout: 10_000 });
  const named = await box.getByLabel('Scenario name', { exact: true }).inputValue().catch(() => null);
  await box.getByLabel('Scenario name', { exact: true }).fill(NAME);
  // The box opens empty: a rule sets only what is typed.
  await fillRule(box, { levels: 6, unitSize: 60, coverage: 60 });
  await box.getByRole('button', { name: 'Apply' }).click();
  // The map page's spinner, while the recompute runs.
  const spun = await page.locator('.map-loading[data-loading="true"]')
    .waitFor({ timeout: 5_000 }).then(() => true, () => false);
  await gestureDone(page, box);
  const created = (await (await fetch(`${API}/scenarios`)).json()).find((s) => s.name === NAME);
  report.scenarioId = created ? created.id : null;
  const centre1 = await rendered(page, ...blk(0, 1));
  const bar = await page.locator('tr[aria-current="true"]').innerText();
  step('apply on the baseline creates the scenario and shows the change', created && centre1 && centre1.state === 'changed' && spun, {
    spinnerShown: spun, prefilledName: named, created: Boolean(created), centre: centre1, bar,
    changePressed: await page.getByRole('button', { name: 'Change' }).getAttribute('aria-pressed'),
  });
  if (!created) return;
  const first = await rules(created.id);

  // 2: change the rule's numbers; the map must paint the new value, not a cached one.
  await click(blk(0, 1));
  const ruleBox = page.locator('form[aria-label="Edit rule"]');
  await ruleBox.waitFor({ timeout: 10_000 });
  const v2 = { levels: 8, unitSize: 60, coverage: 0.6 };
  await page.locator('form').getByLabel('Storeys', { exact: true }).fill(String(v2.levels));
  await ruleBox.getByRole('button', { name: 'Save' }).click();
  await gestureDone(page, ruleBox);
  await page.getByRole('button', { name: 'Values' }).click();
  await settled(page);
  const valued = await rendered(page, ...blk(0, 1));
  step('saving new numbers repaints the block with the new value', valued && approx(valued.value_numeric, density(v2)), {
    expected: density(v2), rendered: valued,
  });
  await page.getByRole('button', { name: 'Change' }).click();
  await settled(page);

  // 2b: drag the rule; it moves in place (same id, same place in "later wins").
  await click(blk(0, 1));
  await ruleBox.waitFor({ timeout: 10_000 });
  await page.waitForTimeout(300);
  const shot = async (name) => process.env.E2E_SHOTS && page.screenshot({ path: `${process.env.E2E_SHOTS}/${name}.png` });
  await shot('2b-before-drag');
  // Grab the rule by its middle — near a corner, terra-draw takes the corner
  // handle and reshapes the rule instead of moving it — and move it by two
  // blocks, so its middle lands between blocks (2, 1) and (3, 2).
  const middle = [grid.x0 + 1.0 * bw, grid.y0 + 2.0 * bh];
  await drag(page, at(...middle), at(middle[0] + 2 * bw, middle[1]));
  await shot('2b-after-drag');
  await ruleBox.getByRole('button', { name: 'Save' }).click();
  await gestureDone(page, ruleBox);
  await shot('2b-after-save');
  const oldPlace = await rendered(page, ...blk(0, 1));
  const newPlace = await rendered(page, ...blk(2, 1));
  const moved = await rules(created.id);
  step('dragging a rule moves it in place', oldPlace && oldPlace.state === 'unchanged' && newPlace && newPlace.state === 'changed'
    && sameIds(first, moved) && moved.length === 1
    && moved[0].geometry_wkt !== first[0].geometry_wkt, {
    oldPlace, newPlace, sameIds: sameIds(first, moved),
  });

  // 3: a later, overlapping rule wins where they overlap.
  await drawRectangle(page, ...span(3, 0, 3, 1));
  await box.waitFor({ timeout: 10_000 });
  const v3 = { levels: 3, unitSize: 66, coverage: 0.6 };
  await fillRule(box, { levels: v3.levels, unitSize: v3.unitSize, coverage: 60 });
  await box.getByRole('button', { name: 'Apply' }).click();
  await gestureDone(page, box);
  await page.getByRole('button', { name: 'Values' }).click();
  await settled(page);
  const overlap = await rendered(page, ...blk(3, 1));
  const onlyFirst = await rendered(page, ...blk(2, 2));
  step('the later rule wins in the overlap', overlap && approx(overlap.value_numeric, density(v3))
    && onlyFirst && approx(onlyFirst.value_numeric, density(v2)), { overlap, onlyFirst, v2: density(v2), v3: density(v3) });
  await page.getByRole('button', { name: 'Change' }).click();
  await settled(page);
  await click(blk(2, 2));
  await ruleBox.waitFor({ timeout: 10_000 });
  const says = await ruleBox.innerText();
  step('an earlier rule says which rule overrides it', /Rule 2 also gives storeys/.test(says), { box: says });
  await page.keyboard.press('Escape');
  await ruleBox.waitFor({ state: 'detached', timeout: 5_000 });

  // 4: delete both; the scenario equals the baseline again.
  for (const point of [blk(3, 0), blk(2, 2)]) {
    await click(point);
    await ruleBox.waitFor({ timeout: 10_000 });
    await ruleBox.getByRole('button', { name: 'Delete rule' }).click();
    await gestureDone(page, ruleBox);
  }
  const left = await rules(created.id);
  const after = await rendered(page, ...blk(2, 1));
  step('deleting every rule returns the scenario to the baseline', left.length === 0 && (!after || after.state === 'unchanged'), {
    rules: left.length, rendered: after,
  });

  // 6: a rule that sets only household size changes population, and density
  // keeps each block's own value — writing every number flattened the area.
  await page.getByRole('button', { name: 'Values' }).click();
  await settled(page);
  const before = [await rendered(page, ...blk(0, 1)), await rendered(page, ...blk(1, 2))];
  await drawRectangle(page, ...span(0, 1, 1, 2));
  await box.waitFor({ timeout: 10_000 });
  await fillRule(box, { people: 5 });
  await box.getByRole('button', { name: 'Apply' }).click();
  await gestureDone(page, box);
  await page.getByRole('button', { name: 'Values' }).click();
  await settled(page);
  const dens = [await rendered(page, ...blk(0, 1)), await rendered(page, ...blk(1, 2))];
  await page.getByLabel('Indicator').selectOption({ label: 'Resident Population (potential)' });
  await page.getByRole('button', { name: 'Change' }).click();
  await settled(page);
  const people = await rendered(page, ...blk(0, 1));
  const v = (f) => f && f.value_numeric;
  step('a rule that sets only household size keeps each block\'s density', dens.every(Boolean)
    && approx(v(dens[0]), v(before[0])) && approx(v(dens[1]), v(before[1]))
    && Math.abs(v(dens[0]) - v(dens[1])) > 1e-6 && people && people.state === 'changed', {
    before, after: dens, people,
  });
}

/** Type a rule's numbers into its box as values ("="); anything not given
 *  is left empty. An empty number starts at "+ %", so "=" is chosen. */
async function fillRule(box, { levels, unitSize, people, coverage }) {
  for (const [label, value] of [
    ['Storeys', levels], ['m² per home', unitSize], ['People per home', people], ['Lot coverage', coverage],
  ]) {
    await box.getByLabel(label, { exact: true }).fill(value === undefined ? '' : String(value));
    if (value !== undefined) {
      await box.getByRole('group', { name: `${label} unit` }).getByRole('button', { name: '=', exact: true }).click();
    }
  }
}

/** Every [lon, lat] of a geometry, whatever its nesting: a block on a real
 *  pilot is often a MultiPolygon, one level deeper than the synthetic grid's
 *  polygons, and flattening once handed fitBounds rings instead of points. */
const POSITIONS = `(function positions(c) {
  return typeof c[0] === 'number' ? [c] : c.flatMap(positions);
})`;

/** Fit the map to the indicator's blocks (or to E2E_FOCUS); return the
 *  screen box to address and its grid. */
async function fitToBlocks(page) {
  await page.evaluate(([layer, focus, positionsSrc]) => {
    const positions = eval(positionsSrc);
    const m = window.__scenarioMap;
    if (focus) {
      m.fitBounds([[focus[0], focus[1]], [focus[2], focus[3]]], { padding: 20, animate: false });
      return;
    }
    const coords = m.queryRenderedFeatures({ layers: [layer] }).flatMap((f) => positions(f.geometry.coordinates));
    const xs = coords.map((c) => c[0]);
    const ys = coords.map((c) => c[1]);
    m.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], { padding: 60, animate: false });
  }, [FILL, FOCUS, POSITIONS]);
  await settled(page);
  return page.evaluate(([layer, focus, grid, positionsSrc]) => {
    const positions = eval(positionsSrc);
    const m = window.__scenarioMap;
    if (focus) {
      const a = m.project([focus[0], focus[3]]);
      const b = m.project([focus[2], focus[1]]);
      return { x0: a.x, x1: b.x, y0: a.y, y1: b.y, cols: grid, rows: grid };
    }
    const features = m.queryRenderedFeatures({ layers: [layer] });
    const points = features.flatMap((f) => positions(f.geometry.coordinates)).map((c) => m.project(c));
    const ids = new Set(features.map((f) => f.properties.feature_id));
    const side = Math.round(Math.sqrt(ids.size));
    return {
      x0: Math.min(...points.map((p) => p.x)), x1: Math.max(...points.map((p) => p.x)),
      y0: Math.min(...points.map((p) => p.y)), y1: Math.max(...points.map((p) => p.y)),
      cols: side, rows: side,
    };
  }, [FILL, FOCUS, GRID, POSITIONS]);
}

/** A point within `radius` px of `at` on the block whose own centre is
 *  nearest `at`: a rule catches a block by its interior point, so a probe on
 *  a large block whose middle lies outside the drawn area would read
 *  "unchanged" for a reason that has nothing to do with the page. */
function snap(page, at, radius) {
  return page.evaluate(([x, y, r, layer, positionsSrc]) => {
    const positions = eval(positionsSrc);
    const m = window.__scenarioMap;
    const steps = 4;
    let best = null;
    for (let i = -steps; i <= steps; i += 1) {
      for (let j = -steps; j <= steps; j += 1) {
        const px = x + (i * r) / steps;
        const py = y + (j * r) / steps;
        const [f] = m.queryRenderedFeatures([px, py], { layers: [layer] });
        if (!f) continue;
        const pts = positions(f.geometry.coordinates).map((c) => m.project(c));
        const cx = (Math.min(...pts.map((p) => p.x)) + Math.max(...pts.map((p) => p.x))) / 2;
        const cy = (Math.min(...pts.map((p) => p.y)) + Math.max(...pts.map((p) => p.y))) / 2;
        const d = Math.hypot(cx - x, cy - y) + 0.01 * Math.hypot(px - x, py - y);
        if (!best || d < best.d) best = { d, p: [px, py] };
      }
    }
    return best ? best.p : [x, y];
  }, [at[0], at[1], radius, FILL, POSITIONS]);
}

async function drawRectangle(page, from, to) {
  await page.getByRole('button', { name: 'Draw rectangle' }).click();
  await drag(page, from, to);
}

async function drag(page, from, to) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) {
    await page.mouse.move(from.x + ((to.x - from.x) * i) / 12, from.y + ((to.y - from.y) * i) / 12);
  }
  await page.mouse.up();
  await page.waitForTimeout(300);
}

/** The box closes when its gesture stood; an error inside it is a failure. */
async function gestureDone(page, box) {
  const outcome = await Promise.race([
    box.waitFor({ state: 'detached', timeout: 120_000 }).then(() => 'closed'),
    box.locator('[role="alert"]').waitFor({ timeout: 120_000 }).then(() => 'refused'),
  ]);
  if (outcome === 'refused') throw new Error(`refused: ${await box.locator('[role="alert"]').innerText()}`);
  await settled(page);
}

/** Wait for the map to have drawn everything it asked for. */
async function settled(page) {
  await page.waitForTimeout(400);
  await page.waitForFunction(() => window.__scenarioMap.loaded(), null, { timeout: 60_000 });
  await page.waitForTimeout(300);
  canvasBox = await page.locator('.maplibregl-canvas').boundingBox();
}

/** The indicator feature painted at a canvas point, or null. */
function rendered(page, x, y) {
  return page.evaluate(
    ([px, py, layer]) => {
      const f = window.__scenarioMap.queryRenderedFeatures([px, py], { layers: [layer] })[0];
      return f ? { ...f.properties } : null;
    },
    [x, y, FILL],
  );
}

async function rules(scenarioId) {
  return (await (await fetch(`${API}/scenarios/${scenarioId}`)).json()).rules;
}

const sameIds = (a, b) =>
  JSON.stringify(a.map((r) => [r.id, r.ordinal]).sort()) === JSON.stringify(b.map((r) => [r.id, r.ordinal]).sort());
const approx = (a, b) => typeof a === 'number' && Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(b));

main();
