import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A05-search-a11y.js');
test('A05 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A05.reducedMotion, true));
