import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A02-design-system.js');
test('A02 implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.A02.deterministic, true));
