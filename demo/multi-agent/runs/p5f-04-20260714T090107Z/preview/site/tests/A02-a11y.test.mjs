import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A02-design-system-a11y.js');
test('A02 honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.A02.reducedMotion, true));
