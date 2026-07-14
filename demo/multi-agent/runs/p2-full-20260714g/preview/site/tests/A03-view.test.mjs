import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A03-products-view.js');
test('A03 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A03(), /<section/));
