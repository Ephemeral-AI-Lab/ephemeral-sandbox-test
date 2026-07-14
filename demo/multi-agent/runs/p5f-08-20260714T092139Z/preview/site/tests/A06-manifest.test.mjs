import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A06 manifest names its owner', async () => assert.equal(JSON.parse(await readFile(new URL('../src/features/A06-cart.json', import.meta.url), 'utf8')).owner, 'A06'));
