import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A04 style keeps focus visible', async () => assert.match(await readFile(new URL('../src/features/A04-catalog.css', import.meta.url), 'utf8'), /focus-visible/));
