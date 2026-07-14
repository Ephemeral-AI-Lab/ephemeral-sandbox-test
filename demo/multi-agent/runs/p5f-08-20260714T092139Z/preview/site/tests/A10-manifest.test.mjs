import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A10 manifest names its owner', async () => assert.equal(JSON.parse(await readFile(new URL('../src/features/A10-qa.json', import.meta.url), 'utf8')).owner, 'A10'));
