import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A08-checkout-data.js');
test('A08 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A08.owner, 'A08'));
