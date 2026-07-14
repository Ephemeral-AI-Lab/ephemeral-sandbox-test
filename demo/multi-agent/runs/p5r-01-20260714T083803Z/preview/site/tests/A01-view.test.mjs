import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A01 view has a semantic section', async () => assert.match(await readFile(new URL('../src/app.js', import.meta.url), 'utf8'), /product-grid/));
