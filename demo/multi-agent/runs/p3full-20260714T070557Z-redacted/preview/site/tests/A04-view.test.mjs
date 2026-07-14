import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A04-catalog-view.js');
test('A04 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A04(), /<section/));
