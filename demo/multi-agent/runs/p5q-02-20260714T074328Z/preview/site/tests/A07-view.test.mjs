import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A07-wishlist-view.js');
test('A07 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A07(), /<section/));
