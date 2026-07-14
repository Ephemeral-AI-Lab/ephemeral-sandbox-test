import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A07-wishlist-a11y.js');
test('A07 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A07.reducedMotion, true));
