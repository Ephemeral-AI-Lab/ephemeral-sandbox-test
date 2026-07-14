import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A10-qa-data.js');
test('A10 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A10.owner, 'A10'));
