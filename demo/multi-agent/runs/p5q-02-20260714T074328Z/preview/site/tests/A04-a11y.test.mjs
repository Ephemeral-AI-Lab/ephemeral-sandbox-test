import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A04-catalog-a11y.js');
test('A04 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A04.reducedMotion, true));
