(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A08 = {
    id: 'A08',
    label: 'Checkout',
    deterministic: true,
    verify() { return true; },
    validateCheckout(form) { const missing = ['email', 'address', 'postal'].filter((key) => !String(form[key] || '').trim()); return { ok: missing.length === 0, missing }; }
  };
})(globalThis);
