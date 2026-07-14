import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A09-accessibility.js');
test('A09 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A09.deterministic, true));
