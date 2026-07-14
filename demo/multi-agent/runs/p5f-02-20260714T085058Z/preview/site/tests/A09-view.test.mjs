import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A09-accessibility-view.js');
test('A09 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A09(), /<section/));
