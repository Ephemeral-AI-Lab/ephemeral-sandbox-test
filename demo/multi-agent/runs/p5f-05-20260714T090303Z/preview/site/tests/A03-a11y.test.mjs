import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A03-products-a11y.js');
test('A03 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A03.reducedMotion, true));
