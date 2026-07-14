import assert from 'node:assert/strict';
import test from 'node:test';
await import('../src/features/A02-design-system-view.js');
test('A02 view has a semantic section', () => assert.match(globalThis.FlashCart.views.A02(), /<section/));
