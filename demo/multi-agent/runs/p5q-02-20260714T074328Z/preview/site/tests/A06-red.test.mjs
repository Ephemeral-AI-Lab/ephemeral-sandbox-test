import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A06-cart.js');
test('A06 target settles after repair', () => assert.equal(globalThis.FlashCart.features.A06.verify(), true, 'A06 implementation should report verified'));
