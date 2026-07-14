import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A02-design-system-data.js');
test('A02 fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.A02.owner, 'A02'));
