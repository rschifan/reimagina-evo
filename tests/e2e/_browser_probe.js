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
  await browser.close();
  require('fs').writeFileSync(OUT, JSON.stringify(report, null, 2));
})().catch(e => { console.error(e); process.exit(1); });
