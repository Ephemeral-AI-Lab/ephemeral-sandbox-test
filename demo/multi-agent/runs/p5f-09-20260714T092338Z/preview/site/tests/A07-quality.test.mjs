import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A07-wishlist.js');
test('A07 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A07.deterministic, true));
