import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A10-qa.js');
test('A10 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A10.deterministic, true));
