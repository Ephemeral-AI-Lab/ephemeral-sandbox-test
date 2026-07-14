import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A06-cart-data.js');
test('A06 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A06.owner, 'A06'));
