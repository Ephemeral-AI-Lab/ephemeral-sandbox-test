import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A08-checkout-view.js');
test('A08 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A08(), /<section/));
