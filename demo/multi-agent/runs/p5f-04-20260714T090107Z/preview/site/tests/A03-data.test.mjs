import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A03-products-data.js');
test('A03 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A03.owner, 'A03'));
