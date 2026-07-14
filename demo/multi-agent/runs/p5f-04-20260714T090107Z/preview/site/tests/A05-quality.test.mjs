import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A05-search.js');
test('A05 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A05.deterministic, true));
