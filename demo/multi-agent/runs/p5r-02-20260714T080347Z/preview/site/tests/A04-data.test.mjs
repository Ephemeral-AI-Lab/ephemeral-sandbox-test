import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A04-catalog-data.js');
test('A04 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A04.owner, 'A04'));
