import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A10-qa-view.js');
test('A10 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A10(), /<section/));
