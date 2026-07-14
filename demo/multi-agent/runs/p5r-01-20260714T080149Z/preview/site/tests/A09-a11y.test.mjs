import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A09-accessibility-a11y.js');
test('A09 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A09.reducedMotion, true));
