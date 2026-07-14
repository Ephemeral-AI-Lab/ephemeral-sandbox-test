import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A08-checkout-a11y.js');
test('A08 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A08.reducedMotion, true));
