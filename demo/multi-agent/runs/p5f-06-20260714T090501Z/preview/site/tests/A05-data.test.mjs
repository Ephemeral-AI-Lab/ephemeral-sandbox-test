import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A05-search-data.js');
test('A05 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A05.owner, 'A05'));
