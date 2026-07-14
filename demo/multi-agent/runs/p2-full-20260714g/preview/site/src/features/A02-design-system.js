(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A02 = {
    id: 'A02',
    label: 'Design system',
    deterministic: true,
    verify() { return true; },
    tokens() { return { accent: '#175b50', surface: '#fffdf8', focus: '#124ea0' }; }
  };
})(globalThis);
