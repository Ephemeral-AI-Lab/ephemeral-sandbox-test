(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A01 = {
    id: 'A01',
    label: 'Foundation',
    deterministic: true,
    verify() { return true; },
    routeFor(path) { return ['/', '/catalog', '/checkout'].includes(path) ? path : '/not-found'; }
  };
})(globalThis);
