// Opens every page of the web app once and reports what the browser saw.
//
// Writes JSON rather than asserting, so the Python suite owns what counts as
// a failure. Keeping the judgement out of here means a new assertion needs no
// change to the driver.
const { chromium } = require('playwright');

const WEB = process.env.E2E_WEB || 'http://127.0.0.1:5173';
const OUT = process.env.E2E_OUT;
const PAGES = [
  { name: 'map',     path: '/' },
  { name: 'catalog', path: '/catalog.html' },
  { name: 'ops',     path: '/ops.html' },
];

(async () => {
  const browser = await chromium.launch();
  const report = { pages: {} };
  for (const page of PAGES) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const tab = await ctx.newPage();
    const errors = [], requests = [], failed = [];
    tab.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    tab.on('pageerror', e => errors.push('UNCAUGHT: ' + e.message));
    tab.on('request', r => requests.push(r.url()));
    tab.on('requestfailed', r => failed.push(r.url()));
    let navigation = null;
    await tab.goto(WEB + page.path, { waitUntil: 'networkidle', timeout: 60000 })
      .catch(e => { navigation = e.message; });
    await tab.waitForTimeout(2500);
    report.pages[page.name] = {
      navigation,
      title: await tab.title().catch(() => null),
      text: (await tab.locator('body').innerText().catch(() => '')).slice(0, 4000),
      consoleErrors: errors,
      requests,
      failedRequests: failed,
    };
    await ctx.close();
  }

  // ── Interaction probes ──────────────────────────────────────────────
  //
  // Everything above is "does the page load". These drive the keyboard and
  // the breakpoint, which is where the map page's interaction actually
  // lives and where loading successfully proves nothing.
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const tab = await ctx.newPage();
  // Street tiles, counted by outcome. 200 is a tile with linework in it;
  // 204 is a legitimately empty one (outside the loaded territory, which is
  // most tiles). Anything else is a broken layer.
  const roadTiles = { ok: 0, empty: 0, other: [] };
  tab.on('response', (r) => {
    if (!r.url().includes('type_slug=road')) return;
    if (r.status() === 200) roadTiles.ok += 1;
    else if (r.status() === 204) roadTiles.empty += 1;
    else roadTiles.other.push(r.status());
  });
  await tab.goto(WEB + '/', { waitUntil: 'networkidle', timeout: 60000 });
  await tab.waitForTimeout(3000);

  const visible = (sel) => tab.evaluate(
    (s) => {
      const el = document.querySelector(s);
      if (!el) return null;
      const cs = getComputedStyle(el);
      return cs.display !== 'none' && cs.visibility !== 'hidden';
    }, sel);

  const interaction = {};
  interaction.paletteHiddenAtRest = (await visible('.palette')) === false;

  await tab.keyboard.press('Control+k');
  await tab.waitForTimeout(400);
  interaction.paletteOpens = (await visible('.palette')) === true;
  interaction.paletteGroups = await tab.$$eval('.palette__group',
    (els) => els.map((e) => e.textContent));
  interaction.paletteCommands = await tab.$$eval('.palette__item', (els) => els.length);

  await tab.keyboard.type('blo');
  await tab.waitForTimeout(300);
  interaction.queryTop = await tab.$$eval('.palette__item .palette__label',
    (els) => els.slice(0, 3).map((e) => e.textContent));

  await tab.keyboard.press('Escape');
  await tab.waitForTimeout(300);
  interaction.paletteCloses = (await visible('.palette')) === false;

  // Single-key shortcuts must survive the palette having had focus.
  await tab.keyboard.press('\\');
  await tab.waitForTimeout(400);
  interaction.panelCollapses = await tab.$eval('.app',
    (e) => e.classList.contains('is-collapsed'));
  await tab.keyboard.press('\\');
  await tab.waitForTimeout(400);

  await tab.keyboard.press('?');
  await tab.waitForTimeout(300);
  interaction.shortcutSheetOpens = (await visible('.sheet')) === true;
  interaction.shortcutRows = await tab.$$eval('.sheet__row', (els) => els.length);
  await tab.keyboard.press('Escape');

  // Streets: the map's only spatial reference, and the thing that makes a
  // block something other than a rectangle in black.
  interaction.streetToggle = await tab.$$eval('#layer-toggles label',
    (els) => els.map((e) => e.textContent.trim())).catch(() => []);
  interaction.roadTiles = roadTiles;
  interaction.attribution = await tab
    .$eval('.maplibregl-ctrl-attrib-inner', (e) => e.textContent)
    .catch(() => null);

  interaction.legendHasDistribution = (await tab.$('.legend .dist')) !== null;
  interaction.distributionBars = await tab.$$eval('.legend .dist__bar',
    (els) => els.length).catch(() => 0);

  // Horizontal document overflow, per page, at phone width. A page that
  // drifts sideways under a thumb is the classic responsive failure, and it
  // is invisible at desktop size.
  interaction.overflow = {};
  for (const page of PAGES) {
    const small = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const st = await small.newPage();
    await st.goto(WEB + page.path, { waitUntil: 'networkidle', timeout: 60000 })
      .catch(() => {});
    await st.waitForTimeout(2500);
    interaction.overflow[page.name] = await st.evaluate(async () => {
      window.scrollTo(9999, 0);
      await new Promise((r) => setTimeout(r, 100));
      const x = window.scrollX;
      window.scrollTo(0, 0);
      return x;
    });
    await small.close();
  }

  // The drawer at phone width: closed on arrival, and openable.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const pt = await phone.newPage();
  await pt.goto(WEB + '/', { waitUntil: 'networkidle', timeout: 60000 });
  await pt.waitForTimeout(3000);
  interaction.drawerStartsClosed = await pt.$eval('.app',
    (e) => e.classList.contains('is-collapsed'));
  await pt.click('#sidebar-toggle');
  await pt.waitForTimeout(400);
  interaction.drawerOpens = await pt.$eval('.app',
    (e) => !e.classList.contains('is-collapsed'));
  await phone.close();
  await ctx.close();

  report.interaction = interaction;
  await browser.close();
  require('fs').writeFileSync(OUT, JSON.stringify(report, null, 2));
})().catch(e => { console.error(e); process.exit(1); });
