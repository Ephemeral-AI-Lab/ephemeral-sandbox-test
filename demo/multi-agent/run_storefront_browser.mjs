#!/usr/bin/env node
/** Offline Playwright proof for the materialized FlashCart storefront. */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import process from 'node:process';

const playwrightRoot = '/Users/yifanxu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright';
const require = createRequire(import.meta.url);
const { chromium } = require(playwrightRoot);
const treeIndex = process.argv.indexOf('--tree');
if (treeIndex < 0 || !process.argv[treeIndex + 1]) throw new Error('usage: run_storefront_browser.mjs --tree OFFLINE_TREE');
const tree = resolve(process.argv[treeIndex + 1]);
const outputIndex = process.argv.indexOf('--output');
const evidenceOutput = outputIndex >= 0 && process.argv[outputIndex + 1] ? resolve(process.argv[outputIndex + 1]) : null;
const port = 4187;
const origin = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ['scripts/serve.mjs'], { cwd: tree, env: { ...process.env, PORT: String(port), HOST: '127.0.0.1' }, stdio: ['ignore', 'pipe', 'pipe'] });
let output = '';
let serverError = '';
server.stdout.on('data', (value) => { output += value; });
server.stderr.on('data', (value) => { serverError += value; });
async function ready() {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (output.includes('FLASHCART_READY')) return;
    if (server.exitCode !== null) throw new Error(`preview exited: ${serverError || output}`);
    await new Promise((resolveWait) => setTimeout(resolveWait, 50));
  }
  throw new Error(`preview readiness timeout: ${output} ${serverError}`);
}
function ensure(value, message) { if (!value) throw new Error(message); }
async function retain(result) {
  if (!evidenceOutput) return;
  await mkdir(dirname(evidenceOutput), { recursive: true });
  await writeFile(evidenceOutput, JSON.stringify(result, null, 2) + '\n');
}
function redactedError(value) {
  return value
    .replace(/\/(?:Users|private|var|tmp)\/[^\s:'"`]+/g, '[PATH]')
    .replace(/\bpid\s+\d+\b/gi, 'pid [REDACTED]');
}
let browser;
try {
  await ready();
  browser = await chromium.launch({ headless: true });
  const consoleErrors = [];
  const externalRequests = [];
  for (const width of [375, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    page.on('console', (entry) => { if (entry.type() === 'error') consoleErrors.push(entry.text()); });
    page.on('request', (request) => { if (!request.url().startsWith(origin)) externalRequests.push(request.url()); });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(origin + '/#/catalog', { waitUntil: 'networkidle' });
    await page.getByRole('heading', { name: /Everyday gear/ }).waitFor();
    ensure(await page.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth), `body overflow at ${width}px`);
    ensure(await page.locator('#search').evaluate((input) => getComputedStyle(input).minHeight !== '0px'), `missing usable input at ${width}px`);
    ensure(await page.locator('body').evaluate(() => getComputedStyle(document.body).getPropertyValue('--focus').trim().length > 0), 'focus token missing');
    ensure(await page.locator('body').evaluate(() => getComputedStyle(document.body).getPropertyValue('color-scheme').includes('light')), 'not a light storefront');
    if (width === 375) {
      await page.locator('#search').focus();
      await page.keyboard.press('Tab');
      ensure(await page.evaluate(() => document.activeElement !== document.body), 'keyboard focus was lost');
      await page.locator('#search').fill('<img src=x onerror=alert(1)>');
      await page.getByText('No products match those filters. Try a different search.').waitFor();
      ensure(await page.locator('img').count() === 0, 'hostile search created markup');
      await page.locator('#search').fill('mug');
      await page.getByText('1 result').waitFor();
      await page.getByRole('button', { name: 'Save to wishlist' }).click();
      await page.getByRole('link', { name: 'Wishlist' }).click();
      await page.locator('#wishlist-list').waitFor();
      await page.getByRole('link', { name: 'Catalog', exact: true }).click();
      await page.getByRole('button', { name: 'View details for Studio Mug' }).click();
      await page.locator('#variant-select').selectOption('Ink');
      await page.locator('#add-variant').click();
      await page.locator('#cart-count').getByText('1 item').waitFor();
      await page.locator('#promo-code').fill('SAVE10');
      await page.getByRole('button', { name: 'Apply' }).click();
      await page.getByText('SAVE10 applied').waitFor();
      await page.locator('#checkout-button').click();
      await page.getByRole('heading', { name: 'Secure checkout' }).waitFor();
      await page.locator('#review-order').click();
      await page.getByText('Complete: email, address, postal').waitFor();
      await page.locator('#email').fill('buyer@example.test');
      await page.locator('#address').fill('1 Offline Way');
      await page.locator('#postal').fill('10001');
      await page.locator('#review-order').click();
      await page.locator('#receipt').getByText('order FC-0001').waitFor();
      ensure(await page.locator('#receipt').evaluate((receipt) => matchMedia('(prefers-reduced-motion: reduce)').matches && parseFloat(getComputedStyle(receipt).transitionDuration) <= 0.001), 'reduced-motion rule not active');
    }
    await page.close();
  }
  const html = await (await fetch(origin + '/')).text();
  const assets = [...html.matchAll(/(?:src|href)="(\.\/[^"]+)"/g)].map((match) => match[1]);
  for (const asset of assets) ensure((await fetch(origin + '/' + asset.slice(2))).status === 200, `asset missing: ${asset}`);
  ensure((await fetch(origin + '/..%2fpackage.json')).status === 403, 'server traversal was not rejected');
  ensure((await fetch(origin + '/missing')).status === 404, 'server missing route did not return 404');
  ensure(consoleErrors.length === 0, `browser console errors: ${consoleErrors.join(' | ')}`);
  ensure(externalRequests.length === 0, `unexpected external browser requests: ${externalRequests.join(' | ')}`);
  await browser.close();
  browser = null;
  const result = { status: 'passed', widths: [375, 1440], assets: assets.length, console_errors: consoleErrors.length, external_requests: externalRequests.length };
  await retain(result);
  console.log(JSON.stringify(result));
} catch (error) {
  const rawError = error instanceof Error ? error.message : String(error);
  const result = {
    status: 'failed',
    error: redactedError(rawError),
    error_sha256: createHash('sha256').update(rawError).digest('hex'),
  };
  await retain(result);
  console.error(result.error);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
}
