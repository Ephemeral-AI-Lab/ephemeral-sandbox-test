(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A07 = {
    id: 'A07',
    label: 'Wishlist',
    deterministic: true,
    verify() { return true; },
    toggleWishlist(ids, id) { const next = new Set(ids); next.has(id) ? next.delete(id) : next.add(id); return [...next].sort(); }
  };
})(globalThis);
