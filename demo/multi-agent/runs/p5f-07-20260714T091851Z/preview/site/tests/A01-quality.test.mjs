import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A01-foundation.js');
test('A01 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A01.deterministic, true));
