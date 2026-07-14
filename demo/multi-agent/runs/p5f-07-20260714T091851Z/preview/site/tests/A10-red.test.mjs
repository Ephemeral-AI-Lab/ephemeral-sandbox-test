import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A10-qa.js');
test('A10 target settles after repair', () => assert.equal(globalThis.FlashCart.features.A10.verify(), true, 'A10 implementation should report verified'));
