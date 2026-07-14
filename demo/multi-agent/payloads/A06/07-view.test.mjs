import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A06-cart-view.js');
test('A06 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A06(), /<section/));
