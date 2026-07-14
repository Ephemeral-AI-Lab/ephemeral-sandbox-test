import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A05-search-view.js');
test('A05 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A05(), /<section/));
