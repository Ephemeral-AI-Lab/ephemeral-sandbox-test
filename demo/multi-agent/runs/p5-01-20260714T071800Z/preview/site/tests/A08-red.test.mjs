import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A08-checkout.js');
test('A08 target settles after repair', () => assert.equal(globalThis.FlashCart.features.A08.verify(), true, 'A08 implementation should report verified'));
