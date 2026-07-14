import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A09-accessibility-data.js');
test('A09 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A09.owner, 'A09'));
