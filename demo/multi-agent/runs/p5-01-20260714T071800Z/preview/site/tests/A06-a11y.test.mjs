import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A06-cart-a11y.js');
test('A06 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A06.reducedMotion, true));
