import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A07-wishlist-data.js');
test('A07 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A07.owner, 'A07'));
