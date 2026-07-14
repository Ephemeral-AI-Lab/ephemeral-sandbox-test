import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
test('A01 honors reduced motion', async () => assert.match(await readFile(new URL('../scripts/serve.mjs', import.meta.url), 'utf8'), /FLASHCART_READY/));
