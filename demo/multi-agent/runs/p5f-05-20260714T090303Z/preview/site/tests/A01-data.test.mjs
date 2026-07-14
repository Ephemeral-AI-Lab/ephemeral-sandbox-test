import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A01 fixture is deterministic', async () => assert.match(await readFile(new URL('../index.html', import.meta.url), 'utf8'), /<main/));
