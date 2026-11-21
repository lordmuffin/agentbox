// Example: a user flow against a web app (tether's PWA, or any other), failing on any
// console error. Run: node /opt/agentbox/browser/examples/pwa-flow.cjs http://localhost:7000
const assert = require('node:assert/strict');
const { launch, newPage, watch } = require('agentbox-browser');

(async () => {
  const url = process.argv[2];
  if (!url) { console.error('usage: pwa-flow.cjs <url>'); process.exit(2); }
  const browser = await launch();
  try {
    const page = await newPage(browser, { device: 'Pixel 7' }); // mobile first
    const log = watch(page);
    await page.goto(url, { waitUntil: 'networkidle' });
    assert.ok((await page.title()).length > 0, 'page has a title');
    // Drive the app here: page.getByRole('button', { name: '...' }).click(), page.fill(...), ...
    await page.screenshot({ path: 'pwa-flow.png', fullPage: true });
    assert.deepEqual(log.problems(), [], 'no console errors, failed requests or HTTP errors');
    console.log('ok');
  } finally { await browser.close(); }
})().catch((e) => { console.error(e.message); process.exit(1); });
