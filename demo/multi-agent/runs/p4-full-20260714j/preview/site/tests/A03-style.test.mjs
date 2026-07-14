import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A03 style keeps focus visible', async () => assert.match(await readFile(new URL('../src/features/A03-products.css', import.meta.url), 'utf8'), /focus-visible/));
