import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A03-products.js');
test('A03 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A03.deterministic, true));
