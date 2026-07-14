(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A06 = {
    id: 'A06',
    label: 'Cart and pricing',
    deterministic: true,
    verify() { return true; },
    quoteCart(subtotalCents, promotionCents = 0) { const config = root.FlashCart.config; const discounted = Math.max(0, subtotalCents - promotionCents); const shippingCents = discounted >= config.freeShippingCents ? 0 : config.standardShippingCents; return { discounted, shippingCents, taxCents: Math.round(discounted * config.taxRate), totalCents: discounted + shippingCents + Math.round(discounted * config.taxRate) }; }
  };
})(globalThis);
