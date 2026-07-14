(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A03 = {
    id: 'A03',
    label: 'Product data',
    deterministic: true,
    verify() { return true; },
    products() { return [{ id: 'trail-pack', name: 'Trail Pack', cents: 7400, inventory: 8 }, { id: 'studio-mug', name: 'Studio Mug', cents: 2600, inventory: 11 }]; }
  };
})(globalThis);
