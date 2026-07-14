#!/usr/bin/env node
/**
 * Browser proof that a recorded FlashCart export remains usable with no runner,
 * sandbox, or network server.  It also opens a separate hostile-text fixture;
 * that fixture is deliberately labelled as browser-safety input, never run
 * evidence.
 */

import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, extname, resolve, sep } from 'node:path';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';

const playwrightRoot = '/Users/yifanxu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright';
const require = createRequire(import.meta.url);
const { chromium } = require(playwrightRoot);
const option = (name) => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : null;
};
const demo = option('--demo');
const output = option('--output');
if (!demo || !output) throw new Error('usage: run_recorded_browser.mjs --demo FILE --output FILE');

const failures = [];
const external = [];
const ensure = (value, message) => { if (!value) throw new Error(message); };
const redactedError = (error) => String(error instanceof Error ? error.message : error)
  .replace(/\/(?:Users|var|private|tmp)\/[^\s\n)]+/g, '[ABSOLUTE_PATH]')
  .replace(/\bpid=\d+\b/g, 'pid=[REDACTED]')
  .replace(/Chromium\.MachPortRendezvousServer\.\d+/g, 'Chromium.MachPortRendezvousServer.[REDACTED]');

function hostileProjection() {
  const agents = Array.from({ length: 10 }, (_, index) => ({
    id: `A${String(index + 1).padStart(2, '0')}`,
    role: index === 7 ? '</script><img src=x onerror="window.__flashcartPwned=1">' : `Safety agent ${index + 1}`,
    planned: 48,
    completed: 48,
  }));
  return {
    schema_version: 'multiagent-demo/v1', projection_seq: 1,
    run: { id: 'browser-safety-fixture', status: 'passed', title: 'Hostile <img src=x>', elapsed_ms: 1, calls: { planned: 480, completed: 480 }, cleanup_verdict: 'clean', execution_verdict: 'passed' },
    agents,
    evidence: { raw_owner_mapping: {}, conflict: { state: 'verified', process: { status: 'ok', exit_code: 0 }, publication: { publish_rejected: true, class: 'source_conflict' } }, preview: null },
    presentation: { scenes: ['fanout', 'merge', 'conflict', 'network', 'evidence'].map((id) => ({ id, title: id, state: 'completed', summary: '</script><svg onload="window.__flashcartPwned=2">' })) },
    artifacts: { unsafe: { id: 'unsafe', path: 'javascript:window.__flashcartPwned=3', safe_for_demo: true, sha256: 'not-a-real-artifact' } },
    narrative: [{ provenance: 'simulated_narrative', text: '</script><img src=x onerror="window.__flashcartPwned=4">' }],
  };
}

async function hostileDemoDocument() {
  const source = await readFile(demo, 'utf8');
  const marker = /<script id="demo-data" type="application\/octet-stream" data-encoding="base64">[^<]*<\/script>/;
  const encoded = Buffer.from(JSON.stringify(hostileProjection()), 'utf8').toString('base64');
  ensure(marker.test(source), 'recorded export has no safe embedded-data marker');
  return source.replace(marker, `<script id="demo-data" type="application/octet-stream" data-encoding="base64">${encoded}</script>`);
}

const CONTENT_TYPES = { '.css': 'text/css; charset=utf-8', '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml' };
async function startStaticServer(root, hostileDocument) {
  const server = createServer(async (request, response) => {
    let pathname;
    try { pathname = decodeURIComponent(new URL(request.url || '/', 'http://localhost').pathname); } catch { response.writeHead(400).end(); return; }
    if (pathname === '/hostile.html') { response.writeHead(200, { 'Content-Type': CONTENT_TYPES['.html'] }); response.end(hostileDocument); return; }
    const target = resolve(root, '.' + pathname);
    if (target !== root && !target.startsWith(root + sep)) { response.writeHead(403).end(); return; }
    try {
      if (!(await stat(target)).isFile()) throw new Error('not a file');
      response.writeHead(200, { 'Content-Type': CONTENT_TYPES[extname(target)] || 'application/octet-stream' });
      response.end(await readFile(target));
    } catch { response.writeHead(404).end(); }
  });
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(0, '127.0.0.1', () => { server.off('error', rejectListen); resolveListen(); });
  });
  const address = server.address();
  ensure(address && typeof address === 'object', 'recorded static server did not bind');
  return { server, origin: `http://127.0.0.1:${address.port}` };
}

async function prepare(page, label, width, origin) {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  page.on('console', (entry) => { if (entry.type() === 'error') failures.push(`${label}: console ${entry.text()}`); });
  page.on('pageerror', (error) => failures.push(`${label}: page ${error.message}`));
  page.on('request', (request) => { if (!request.url().startsWith(origin)) external.push(`${label}: ${request.url()}`); });
}

async function verifyShell(page, mode, label) {
  const brand = page.locator('.brand');
  ensure(await brand.isVisible() && (await brand.textContent()).includes('FlashCart Control Room'), `${label}: FlashCart control-room brand is not visible`);
  ensure(await page.locator('h1').isVisible(), `${label}: control-room title is not visible`);
  await page.locator('#modeBadge').getByText(mode, { exact: true }).waitFor();
  ensure(await page.locator('#fatal').isHidden(), `${label}: fatal shell shown`);
  ensure(await page.locator('body').evaluate((node) => node.scrollWidth <= window.innerWidth), `${label}: body overflow`);
  ensure(await page.getByRole('tablist', { name: 'Proof scenes' }).count() === 1, `${label}: proof tablist absent`);
  ensure(await page.getByRole('tablist', { name: 'Evidence views' }).count() === 1, `${label}: evidence tablist absent`);
  const buttonTooSmall = await page.locator('button').evaluateAll((buttons) => buttons.some((button) => {
    const box = button.getBoundingClientRect();
    return box.width < 40 || box.height < 40;
  }));
  ensure(!buttonTooSmall, `${label}: a control is below the mobile target floor`);
}

let browser = null;
let staticServer = null;
try {
  // Launch is inside the capture guard: macOS policy failures can happen
  // before a page exists, but they must still leave a browser result artifact.
  browser = await chromium.launch({ headless: true });
  staticServer = await startStaticServer(dirname(resolve(demo)), await hostileDemoDocument());
  const screenshots = resolve(dirname(output), 'screenshots');
  await mkdir(screenshots, { recursive: true });
  const real = await browser.newPage();
  await prepare(real, 'recorded-real', 375, staticServer.origin);
  await real.goto(`${staticServer.origin}/demo.html`, { waitUntil: 'networkidle' });
  await verifyShell(real, 'recorded', 'recorded-real');
  await real.locator('#calls').getByText('482 / 482').waitFor();
  await real.getByRole('button', { name: /A08/ }).click();
  await real.locator('#process').getByText('exit 0', { exact: true }).waitFor();
  await real.locator('#publication').getByText('rejected: source_conflict', { exact: true }).waitFor();
  await real.frameLocator('#preview').locator('#app').waitFor();
  await real.screenshot({ path: resolve(screenshots, 'recorded-file-mobile.png'), fullPage: true });
  await real.close();

  const hostile = await browser.newPage();
  await prepare(hostile, 'hostile-fixture', 1440, staticServer.origin);
  await hostile.goto(`${staticServer.origin}/hostile.html`, { waitUntil: 'networkidle' });
  await verifyShell(hostile, 'recorded', 'hostile-fixture');
  await hostile.getByRole('button', { name: /A08/ }).click();
  await hostile.getByText('</script><img src=x onerror="window.__flashcartPwned=1">', { exact: true }).waitFor();
  ensure(await hostile.locator('img').count() === 0, 'hostile fixture: markup became an image element');
  ensure(await hostile.evaluate(() => window.__flashcartPwned === undefined), 'hostile fixture: event-handler text executed');
  ensure(await hostile.locator('#evidenceLinks a').count() === 0, 'hostile fixture: unsafe artifact URL became an evidence link');
  await hostile.screenshot({ path: resolve(screenshots, 'hostile-file-desktop.png'), fullPage: true });
  await hostile.close();

  ensure(failures.length === 0, `browser errors: ${failures.join(' | ')}`);
  ensure(external.length === 0, `unexpected browser requests: ${external.join(' | ')}`);
  await writeFile(output, JSON.stringify({ status: 'passed', runner: 'absent', sandbox: 'absent', static_server: 'loopback-export-only', modes: ['recorded', 'hostile-browser-safety-fixture'], screenshots: 2, console_errors: 0, external_requests: 0 }, null, 2) + '\n');
  console.log(JSON.stringify({ status: 'passed', output, demo: basename(demo) }));
} catch (error) {
  const redacted = redactedError(error);
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, JSON.stringify({
    status: 'failed', error: redacted,
    error_sha256: createHash('sha256').update(String(error instanceof Error ? error.message : error)).digest('hex'),
    browser_errors: failures, external_requests: external,
  }, null, 2) + '\n');
  console.error(redacted);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  if (staticServer) await new Promise((resolveClose) => staticServer.server.close(resolveClose));
}
