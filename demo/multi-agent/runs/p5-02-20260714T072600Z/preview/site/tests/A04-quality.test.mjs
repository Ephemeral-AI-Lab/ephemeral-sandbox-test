import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A04-catalog.js');
test('A04 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A04.deterministic, true));
