import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A02 manifest names its owner', async () => assert.equal(JSON.parse(await readFile(new URL('../src/features/A02-design-system.json', import.meta.url), 'utf8')).owner, 'A02'));
