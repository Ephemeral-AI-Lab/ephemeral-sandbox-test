import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A05-search.js');
test('A05 target settles after repair', () => assert.equal(globalThis.FlashCart.features.A05.verify(), true, 'A05 implementation should report verified'));
