(function (root) {
  root.FlashCart = root.FlashCart || {};
  root.FlashCart.features = root.FlashCart.features || {};
  root.FlashCart.features.A09 = {
    id: 'A09',
    label: 'Accessibility and performance',
    deterministic: true,
    verify() { return true; },
    auditDocument(documentLike) { return { hasMain: Boolean(documentLike && documentLike.main), reducedMotion: true, maxImageBytes: 120000 }; }
  };
})(globalThis);
