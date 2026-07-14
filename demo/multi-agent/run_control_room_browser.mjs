#!/usr/bin/env node
/** Browser proof for sample/live/recorded FlashCart control-room modes. */

import { mkdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import process from 'node:process';

const playwrightRoot = '/Users/yifanxu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright';
const require = createRequire(import.meta.url);
const { chromium } = require(playwrightRoot);
const option = (name) => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : null;
};
const origin = option('--origin');
const runId = option('--run-id');
const output = option('--output');
if (!origin || !runId || !output) throw new Error('usage: run_control_room_browser.mjs --origin URL --run-id ID --output FILE');
const base = origin.replace(/\/$/, '');
const screenshots = resolve(dirname(output), 'screenshots');
const failures = [];
const consoleErrors = [];
const externalRequests = [];
const ensure = (value, message) => { if (!value) throw new Error(message); };

async function prepare(page, name, width) {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  page.on('console', (entry) => {
    if (entry.type() !== 'error') return;
    // The missing-run page deliberately exercises a failed fetch; Chromium logs
    // that expected HTTP status as a console error although application code
    // handles it and renders the visible live-mode failure state.
    if (name === 'live-failure' && entry.text().includes('server responded with a status of 404')) return;
    // The reconnect rehearsal intentionally aborts its only projection fetch.
    // The application renders that loss and then recovers after the route is
    // removed; Chromium's transport diagnostic is not an application error.
    if (name === 'live-reconnect' && entry.text().includes('net::ERR_FAILED')) return;
    consoleErrors.push(`${name}: ${entry.text()}`);
  });
  page.on('pageerror', (error) => failures.push(`${name}: ${error.message}`));
  page.on('request', (request) => { if (!request.url().startsWith(base)) externalRequests.push(`${name}: ${request.url()}`); });
}

async function visibleShell(page, expectedMode) {
  await page.getByRole('heading', { name: /FlashCart/ }).waitFor();
  await page.locator('#modeBadge').getByText(expectedMode, { exact: true }).waitFor();
  ensure(await page.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth), `${expectedMode}: horizontal overflow`);
  ensure(await page.locator('#fatal').isHidden(), `${expectedMode}: fatal panel is visible`);
  ensure(await page.getByRole('tablist', { name: 'Proof scenes' }).count() === 1, `${expectedMode}: proof scene tablist missing`);
  ensure(await page.getByRole('tablist', { name: 'Evidence views' }).count() === 1, `${expectedMode}: evidence tablist missing`);
  ensure(await page.locator('body').evaluate(() => getComputedStyle(document.documentElement).colorScheme.includes('light')), `${expectedMode}: control room is not light mode`);
}

async function screenshot(page, name) {
  await page.screenshot({ path: resolve(screenshots, `${name}.png`), fullPage: true });
}

const browser = await chromium.launch({ headless: true });
try {
  await mkdir(screenshots, { recursive: true });

  const sample = await browser.newPage();
  await prepare(sample, 'sample-mobile', 375);
  await sample.goto(`${base}/multiagent/index.html?mode=sample`, { waitUntil: 'networkidle' });
  await visibleShell(sample, 'sample');
  await sample.getByText('Simulated fixture; it is not a live or recorded run.').waitFor();
  await sample.locator('#previewPlaceholder').getByText(/intentionally does not claim/).waitFor();
  const firstScene = (await sample.locator('#sceneSummary').textContent())?.trim();
  await sample.locator('#next').click();
  const advancedScene = (await sample.locator('#sceneSummary').textContent())?.trim();
  ensure(advancedScene && advancedScene !== firstScene, 'sample: scene did not advance');
  await sample.locator('#previous').click();
  ensure((await sample.locator('#sceneSummary').textContent())?.trim() === firstScene, 'sample: retained scene rewind failed');
  await sample.locator('#play').click();
  ensure(await sample.locator('#play').getAttribute('aria-pressed') === 'true', 'sample: scene playback did not start');
  await sample.locator('#play').click();
  ensure(await sample.locator('#play').getAttribute('aria-pressed') === 'false', 'sample: scene playback did not pause');
  await sample.reload({ waitUntil: 'networkidle' });
  await visibleShell(sample, 'sample');
  await sample.getByRole('tab', { name: /Summary/ }).focus();
  await sample.keyboard.press('ArrowRight');
  ensure(await sample.locator('#evidencePanel').evaluate((node) => document.activeElement === node || document.activeElement?.getAttribute('role') === 'tab'), 'sample: evidence keyboard navigation lost focus');
  await screenshot(sample, 'sample-mobile');
  await sample.close();

  const live = await browser.newPage();
  await prepare(live, 'live-desktop', 1440);
  await live.goto(`${base}/multiagent/index.html?mode=live&run=${encodeURIComponent(runId)}`, { waitUntil: 'networkidle' });
  await visibleShell(live, 'live');
  await live.locator('#calls').getByText('482 / 482').waitFor();
  await live.getByRole('button', { name: /A08/ }).click();
  await live.getByText(/Selected agent · A08/).waitFor();
  await live.locator('#process').getByText('exit 0', { exact: true }).waitFor();
  await live.locator('#publication').getByText('rejected: source_conflict', { exact: true }).waitFor();
  await live.getByRole('tab', { name: 'Artifacts' }).click();
  await live.getByRole('link', { name: /call-matrix\.json/ }).waitFor();
  const liveFrame = live.frameLocator('#preview');
  await liveFrame.locator('#app').waitFor();
  await liveFrame.getByRole('heading', { name: /Everyday gear/ }).waitFor();
  ensure((await live.locator('#preview').getAttribute('src'))?.includes(`/runs/${runId}/preview/site/index.html`), 'live: iframe did not select the verified retained tree');
  ensure(await live.locator('#previewPlaceholder').isHidden(), 'live: retained preview placeholder remains visible');
  await screenshot(live, 'live-desktop');
  await live.reload({ waitUntil: 'networkidle' });
  await visibleShell(live, 'live');
  await live.close();

  const reconnect = await browser.newPage();
  await prepare(reconnect, 'live-reconnect', 1920);
  await reconnect.route(`**/runs/${runId}/presentation.json`, (route) => route.abort('failed'));
  await reconnect.goto(`${base}/multiagent/index.html?mode=live&run=${encodeURIComponent(runId)}`, { waitUntil: 'networkidle' });
  await reconnect.locator('#fatal').getByText(/Live mode could not load/).waitFor();
  ensure((await reconnect.locator('#modeBadge').textContent())?.trim() === 'live', 'reconnect: disconnected live mode fell back to sample');
  await reconnect.unroute(`**/runs/${runId}/presentation.json`);
  await reconnect.reload({ waitUntil: 'networkidle' });
  await visibleShell(reconnect, 'live');
  await reconnect.locator('#calls').getByText('482 / 482').waitFor();
  ensure(await reconnect.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth), 'projector: horizontal overflow');
  await reconnect.close();

  const recorded = await browser.newPage();
  await prepare(recorded, 'recorded-mobile', 375);
  await recorded.goto(`${base}/multiagent/generated/${encodeURIComponent(runId)}/demo.html`, { waitUntil: 'networkidle' });
  await visibleShell(recorded, 'recorded');
  await recorded.locator('#calls').getByText('482 / 482').waitFor();
  await recorded.getByRole('button', { name: /A06/ }).click();
  await recorded.getByText(/Selected agent · A06/).waitFor();
  const recordedFrame = recorded.frameLocator('#preview');
  await recordedFrame.locator('#app').waitFor();
  await recordedFrame.getByRole('heading', { name: /Everyday gear/ }).waitFor();
  ensure((await recorded.locator('#preview').getAttribute('src'))?.endsWith(`/generated/${runId}/preview/site/index.html`), 'recorded: iframe did not select the packaged preview');
  ensure(await recorded.locator('#previewPlaceholder').isHidden(), 'recorded: retained preview placeholder remains visible');
  await screenshot(recorded, 'recorded-mobile');
  await recorded.close();

  const failure = await browser.newPage();
  await prepare(failure, 'live-failure', 375);
  await failure.goto(`${base}/multiagent/index.html?mode=live&run=missing-run`, { waitUntil: 'networkidle' });
  await failure.locator('#fatal').getByText(/Live mode could not load/).waitFor();
  ensure((await failure.locator('#modeBadge').textContent())?.trim() === 'live', 'failure: live mode was replaced');
  ensure(!((await failure.locator('#summary').textContent()) || '').includes('Simulated fixture; it is not a live or recorded run.'), 'failure: live error fell back to sample');
  await screenshot(failure, 'live-failure-mobile');
  await failure.close();

  const traversal = await fetch(`${base}/multiagent/%2e%2e/%2e%2e/etc/passwd`);
  ensure(traversal.status === 404, `control server traversal expected 404, got ${traversal.status}`);
  ensure(consoleErrors.length === 0, `control room console errors: ${consoleErrors.join(' | ')}`);
  ensure(externalRequests.length === 0, `unexpected external browser requests: ${externalRequests.join(' | ')}`);
  ensure(failures.length === 0, `control room page errors: ${failures.join(' | ')}`);
  await writeFile(output, JSON.stringify({ status: 'passed', modes: ['sample', 'live', 'recorded', 'live-failure'], screenshots: 4, console_errors: 0, external_requests: 0, rehearsals: { refresh: true, disconnect_reconnect: true, pause_rewind: true, projector_mobile: true } }, null, 2) + '\n');
  console.log(JSON.stringify({ status: 'passed', output, screenshots: 4 }));
} catch (error) {
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, JSON.stringify({ status: 'failed', error: error instanceof Error ? error.message : String(error), console_errors: consoleErrors, external_requests: externalRequests, page_errors: failures }, null, 2) + '\n');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser.close();
}
