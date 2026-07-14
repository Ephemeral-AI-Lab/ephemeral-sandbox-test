(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A05 = {
    id: 'A05',
    label: 'Search and facets',
    deterministic: true,
    verify() { return true; },
    search(items, query) { const needle = String(query).toLowerCase(); return items.filter((item) => item.name.toLowerCase().includes(needle)); }
  };
})(globalThis);
