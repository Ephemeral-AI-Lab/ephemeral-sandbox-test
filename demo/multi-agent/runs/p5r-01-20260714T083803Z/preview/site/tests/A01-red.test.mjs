import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A01-foundation.js');
test('A01 target settles after repair', () => assert.equal(globalThis.FlashCart.features.A01.verify(), true, 'A01 implementation should report verified'));
