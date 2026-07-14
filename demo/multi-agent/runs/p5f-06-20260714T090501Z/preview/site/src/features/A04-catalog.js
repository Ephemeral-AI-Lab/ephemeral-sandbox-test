(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A04 = {
    id: 'A04',
    label: 'Catalog and PDP',
    deterministic: true,
    verify() { return true; },
    selectVariant(product, variant) { return { ...product, variant, selectable: Boolean(variant) }; }
  };
})(globalThis);
