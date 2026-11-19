// Screenshots of every page of the web application, for the delivery
// documentation (docs/delivery/04_technical_documentation.md §6.5).
//
// Run against a running stack — `make dev` on the pilot's data — with the
// Playwright node module resolvable (see tests/e2e/README.md):
//
//   NODE_PATH=<dir with playwright> node scripts/capture_app_screenshots.js \
//       [--web http://localhost:5173] [--api http://127.0.0.1:8001] \
//       [--out docs/delivery/screenshots]
//
// ⚠ Read-only by intent: it opens pages, picks things to look at, and closes
// what it opens. It never presses Apply, Save or Delete. The scenario shown is
// the one with the most rules on the pilot (or `--scenario <id>`).
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};
const WEB = arg('web', 'http://localhost:5173');
const API = arg('api', 'http://127.0.0.1:8001');
const OUT = arg('out', path.join(__dirname, '..', 'docs', 'delivery', 'screenshots'));
// Where the map page's pictures look: the pilot's urban core. Mexicali's by
// default; another pilot passes its own.
const [CORE_LON, CORE_LAT] = arg('center', '-115.468,32.628').split(',').map(Number);
const CORE_ZOOM = Number(arg('zoom', '13.2'));
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

async function settle(page, ms = 2500) {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(ms);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    args: ['--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
  });
  const shot = async (page, name) => {
    await page.screenshot({ path: path.join(OUT, `${name}.png`) });
    console.log(`  ${name}.png`);
  };
  const open = async (url, viewport = DESKTOP) => {
    const page = await (await browser.newContext({ viewport })).newPage();
    await page.goto(url, { waitUntil: 'networkidle', timeout: 90_000 });
    await settle(page);
    return page;
  };

  // 1. The map: the indicator, over the urban core.
  let page = await open(`${WEB}/`);
  await page.evaluate(([lon, lat, zoom]) => window.__map.jumpTo({ center: [lon, lat], zoom }),
    [CORE_LON, CORE_LAT, CORE_ZOOM]);
  await settle(page, 4000);
  await shot(page, 'app-map');
  // 2. A block pinned: its values and rank in the inspector.
  const box = await page.locator('.maplibregl-canvas').first().boundingBox();
  await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.5);
  // The card fills in once the block's values and ranks arrive; the mouse
  // moves off the map so its hover tooltip does not cover them.
  await page.waitForFunction(() => !document.body.innerText.includes('Loading…'), null, { timeout: 20_000 })
    .catch(() => {});
  await page.mouse.move(100, 700);
  await settle(page, 1500);
  await shot(page, 'app-map-block');
  await page.context().close();

  // 3. The map on a phone: panel as a drawer, legend over the map.
  page = await open(`${WEB}/`, PHONE);
  await page.evaluate(([lon, lat, zoom]) => window.__map.jumpTo({ center: [lon, lat], zoom }),
    [CORE_LON, CORE_LAT, CORE_ZOOM - 1]);
  await settle(page, 4000);
  await shot(page, 'app-map-phone');
  await page.context().close();

  // 4–5. The scenarios workspace, on the scenario with the most rules.
  const scenarios = await (await fetch(`${API}/scenarios`)).json();
  const chosen = arg('scenario', null)
    ?? [...scenarios].sort((a, b) => b.edit_count - a.edit_count)[0]?.id;
  page = await open(`${WEB}/scenarios.html${chosen ? `#/${chosen}` : ''}`);
  await settle(page, 3000);
  // Frame the scenario's rules, with the city around them.
  await page.evaluate(() => {
    const m = window.__scenarioMap;
    const coords = m.querySourceFeatures('scenario-rules')
      .flatMap((f) => (f.geometry.type === 'Polygon' ? f.geometry.coordinates : f.geometry.coordinates.flat()).flat());
    if (!coords.length) return;
    const xs = coords.map((c) => c[0]);
    const ys = coords.map((c) => c[1]);
    m.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]],
      { padding: 160, maxZoom: 14, animate: false });
  });
  await settle(page, 4000);
  await shot(page, 'app-scenarios');
  if (chosen) {
    // Open the first rule's box by clicking inside its area, then close it.
    const point = await page.evaluate(() => {
      const m = window.__scenarioMap;
      const f = m && m.querySourceFeatures('scenario-rules')[0];
      if (!f) return null;
      const ring = f.geometry.type === 'Polygon'
        ? f.geometry.coordinates[0] : f.geometry.coordinates[0][0];
      const lon = ring.reduce((s, p) => s + p[0], 0) / ring.length;
      const lat = ring.reduce((s, p) => s + p[1], 0) / ring.length;
      const px = m.project([lon, lat]);
      return [px.x, px.y];
    });
    if (point) {
      const canvas = await page.locator('.maplibregl-canvas').boundingBox();
      await page.mouse.click(canvas.x + point[0], canvas.y + point[1]);
      await page.locator('form[aria-label="Edit rule"]').waitFor({ timeout: 10_000 }).catch(() => {});
      await settle(page, 1200);
      await shot(page, 'app-scenarios-rule');
      await page.keyboard.press('Escape');
    }
  }
  await page.context().close();

  // 6. The indicator catalogue. 7. The runs dashboard.
  page = await open(`${WEB}/catalog.html`);
  await shot(page, 'app-catalog');
  await page.context().close();
  page = await open(`${WEB}/ops.html`);
  await shot(page, 'app-runs');
  await page.context().close();

  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
