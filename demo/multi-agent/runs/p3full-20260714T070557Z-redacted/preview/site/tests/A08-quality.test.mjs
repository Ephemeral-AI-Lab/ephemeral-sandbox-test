import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A08-checkout.js');
test('A08 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A08.deterministic, true));
