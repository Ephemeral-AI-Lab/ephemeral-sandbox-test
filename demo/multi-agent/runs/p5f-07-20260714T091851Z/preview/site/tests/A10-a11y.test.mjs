import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A10-qa-a11y.js');
test('A10 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A10.reducedMotion, true));
