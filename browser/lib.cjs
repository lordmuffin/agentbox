'use strict';
// Browser testing helpers for the agentbox image: system Chromium driven by playwright-core.
//
//   const { launch, newPage, watch, smoke } = require('agentbox-browser');
//
// The image ships Chromium from Debian (no Playwright browser download: the pod has no
// such egress) and playwright-core, so launch() points Playwright at /usr/bin/chromium and
// adds the flags a container needs: no setuid sandbox (no user namespaces in the pod) and
// no /dev/shm use (Kubernetes gives a 64 MB one).

const DEFAULT_EXECUTABLE = '/usr/bin/chromium';

function truthy(v) { return /^(1|true|yes|on)$/i.test(String(v || '')); }

// Pure: launch options from an environment. Exported so tests need no browser.
function launchOptions(env = process.env, extra = {}) {
  return {
    executablePath: env.AGENTBOX_CHROMIUM || DEFAULT_EXECUTABLE,
    headless: !truthy(env.AGENTBOX_BROWSER_HEADED),
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', ...(extra.args || [])],
    ...Object.fromEntries(Object.entries(extra).filter(([k]) => k !== 'args')),
  };
}

// Pure: context options. Internal sites sit behind a private CA that Chromium's own
// store doesn't have; AGENTBOX_BROWSER_IGNORE_HTTPS_ERRORS=1 accepts it.
function contextOptions(env = process.env, extra = {}) {
  const ignoreHTTPSErrors = truthy(env.AGENTBOX_BROWSER_IGNORE_HTTPS_ERRORS);
  return { ignoreHTTPSErrors, ...extra };
}

function playwright() {
  try { return require('playwright-core'); } catch (e) {
    throw new Error('playwright-core is not installed (expected in the agentbox image): ' + e.message);
  }
}

async function launch(extra = {}) {
  return playwright().chromium.launch(launchOptions(process.env, extra));
}

// device: a Playwright device name ('Pixel 7', 'iPhone 14') or undefined for desktop.
async function newPage(browser, { device, ...extra } = {}) {
  const pw = playwright();
  let opts = contextOptions(process.env, extra);
  if (device) {
    const d = pw.devices[device];
    if (!d) throw new Error(`unknown device ${JSON.stringify(device)}; see playwright.devices`);
    opts = { ...d, ...opts };
  }
  const context = await browser.newContext(opts);
  const page = await context.newPage();
  return page;
}

// Record everything a user would call "broken" on a page: console errors, uncaught page
// errors, failed requests and HTTP >= 400 responses. Works on anything with .on().
function watch(page) {
  const log = { console: [], pageErrors: [], failedRequests: [], badResponses: [] };
  page.on('console', (m) => { if (m.type() === 'error') log.console.push(m.text()); });
  page.on('pageerror', (e) => log.pageErrors.push(String((e && e.message) || e)));
  page.on('requestfailed', (r) => log.failedRequests.push(
    `${r.method()} ${r.url()} ${(r.failure() && r.failure().errorText) || ''}`.trim()));
  page.on('response', (r) => { if (r.status() >= 400) log.badResponses.push(`${r.status()} ${r.url()}`); });
  log.problems = () => [
    ...log.console.map((m) => `console.error: ${m}`),
    ...log.pageErrors.map((m) => `pageerror: ${m}`),
    ...log.failedRequests.map((m) => `request failed: ${m}`),
    ...log.badResponses.map((m) => `http: ${m}`),
  ];
  return log;
}

// Load a URL and report on it. Never throws for a broken app; the report says what broke.
//   opts: { device, timeoutMs, waitUntil, screenshot, expectText, expectSelector, ignore: [regex] }
async function smoke(url, opts = {}) {
  const browser = await launch();
  const report = { url, ok: false, status: null, title: null, problems: [], pwa: null };
  try {
    const page = await newPage(browser, { device: opts.device });
    page.setDefaultTimeout(opts.timeoutMs || 30000);
    const log = watch(page);
    const resp = await page.goto(url, { waitUntil: opts.waitUntil || 'networkidle' });
    report.status = resp ? resp.status() : null;
    report.title = await page.title();
    report.pwa = await pwaInfo(page);
    if (opts.expectSelector) {
      if (!(await page.$(opts.expectSelector))) log.pageErrors.push(`expected selector not found: ${opts.expectSelector}`);
    }
    if (opts.expectText) {
      const body = await page.evaluate(() => document.body ? document.body.innerText : '');
      if (!body.includes(opts.expectText)) log.pageErrors.push(`expected text not found: ${opts.expectText}`);
    }
    if (opts.screenshot) await page.screenshot({ path: opts.screenshot, fullPage: true });
    const ignore = (opts.ignore || []).map((r) => (r instanceof RegExp ? r : new RegExp(r)));
    report.problems = log.problems().filter((p) => !ignore.some((re) => re.test(p)));
    if (report.status !== null && report.status >= 400) report.problems.unshift(`navigation status ${report.status}`);
    report.ok = report.problems.length === 0;
  } catch (e) {
    report.problems.push(`navigation failed: ${e.message}`);
  } finally {
    await browser.close();
  }
  return report;
}

// What a PWA needs to be installable: a manifest that loads and a registered service worker.
async function pwaInfo(page) {
  return page.evaluate(async () => {
    const link = document.querySelector('link[rel~="manifest"]');
    const info = { manifest: null, manifestOk: false, serviceWorker: false, viewport: !!document.querySelector('meta[name="viewport"]') };
    if (link) {
      info.manifest = link.href;
      try { const r = await fetch(link.href); info.manifestOk = r.ok && !!(await r.json()); } catch (_) { /* not valid */ }
    }
    if ('serviceWorker' in navigator) {
      try { info.serviceWorker = (await navigator.serviceWorker.getRegistrations()).length > 0; } catch (_) { /* blocked */ }
    }
    return info;
  });
}

module.exports = { launchOptions, contextOptions, launch, newPage, watch, smoke, pwaInfo, DEFAULT_EXECUTABLE };
