#!/usr/bin/env node
'use strict';
const { smoke, launch, newPage, launchOptions } = require('./lib.cjs');

const USAGE = `agentbox-browser: drive the image's Chromium (headless) for web app testing

  agentbox-browser check                       launch Chromium, load a data: page; proves the install works
  agentbox-browser smoke <url> [options]       load <url>; fail on console errors, page errors, failed
                                               requests, HTTP >= 400; report PWA manifest / service worker
  agentbox-browser shot <url> <out.png> [--device NAME] [--viewport WxH]

smoke options:
  --device NAME        emulate a Playwright device, e.g. "Pixel 7" or "iPhone 14" (tether is a mobile PWA)
  --expect-text TEXT   fail unless the page text contains TEXT
  --expect-selector S  fail unless a CSS selector matches
  --ignore REGEX       drop problems matching REGEX (repeatable)
  --screenshot FILE    save a full-page screenshot
  --timeout MS         navigation timeout (30000)
  --json               print the report as JSON

Environment:
  AGENTBOX_CHROMIUM                       browser binary (/usr/bin/chromium)
  AGENTBOX_BROWSER_IGNORE_HTTPS_ERRORS=1  accept certificates from a private CA
  AGENTBOX_BROWSER_HEADED=1               headed (needs a display; none in the pod)

Scripts: require('agentbox-browser') gives launch, newPage, watch, smoke (see /opt/agentbox/browser/lib.cjs).
Exit status: 0 ok, 1 the page has problems, 2 usage error.
`;

function parse(argv) {
  const o = { _: [], ignore: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => { if (i + 1 >= argv.length) throw new Error(`${a} needs a value`); return argv[++i]; };
    if (a === '--json') o.json = true;
    else if (a === '--device') o.device = val();
    else if (a === '--expect-text') o.expectText = val();
    else if (a === '--expect-selector') o.expectSelector = val();
    else if (a === '--ignore') o.ignore.push(val());
    else if (a === '--screenshot') o.screenshot = val();
    else if (a === '--timeout') o.timeoutMs = Number(val());
    else if (a === '--viewport') o.viewport = val();
    else if (a.startsWith('--')) throw new Error(`unknown option ${a}`);
    else o._.push(a);
  }
  return o;
}

async function main(argv) {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd === '-h' || cmd === '--help' || cmd === 'help') { process.stdout.write(USAGE); return cmd ? 0 : 2; }
  const o = parse(rest);
  if (cmd === 'check') {
    const browser = await launch();
    try {
      const page = await newPage(browser);
      await page.goto('data:text/html,<title>agentbox-browser</title><h1>ok</h1>');
      const title = await page.title();
      if (title !== 'agentbox-browser') throw new Error(`unexpected title ${JSON.stringify(title)}`);
      console.log(`ok: ${browser.version()} via ${launchOptions().executablePath}`);
      return 0;
    } finally { await browser.close(); }
  }
  if (cmd === 'smoke') {
    if (o._.length !== 1) throw new Error('smoke takes one URL');
    const r = await smoke(o._[0], o);
    if (o.json) console.log(JSON.stringify(r, null, 2));
    else {
      console.log(`${r.ok ? 'PASS' : 'FAIL'} ${r.url} status=${r.status} title=${JSON.stringify(r.title)}`);
      if (r.pwa) console.log(`  pwa: manifest=${r.pwa.manifestOk} serviceWorker=${r.pwa.serviceWorker} viewport-meta=${r.pwa.viewport}`);
      for (const p of r.problems) console.log(`  - ${p}`);
    }
    return r.ok ? 0 : 1;
  }
  if (cmd === 'shot') {
    if (o._.length !== 2) throw new Error('shot takes <url> <out.png>');
    const browser = await launch();
    try {
      const page = await newPage(browser, { device: o.device });
      if (o.viewport) {
        const m = /^(\d+)x(\d+)$/.exec(o.viewport);
        if (!m) throw new Error('--viewport wants WxH');
        await page.setViewportSize({ width: +m[1], height: +m[2] });
      }
      await page.goto(o._[0], { waitUntil: 'networkidle' });
      await page.screenshot({ path: o._[1], fullPage: true });
      console.log(`wrote ${o._[1]}`);
      return 0;
    } finally { await browser.close(); }
  }
  throw new Error(`unknown command ${cmd}`);
}

main(process.argv.slice(2)).then((c) => process.exit(c), (e) => { console.error(`agentbox-browser: ${e.message}`); process.exit(2); });
