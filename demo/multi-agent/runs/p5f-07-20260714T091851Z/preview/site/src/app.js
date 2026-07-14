(function (root) {
  const app = document.getElementById('app');
  const money = (cents) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
  const productFeature = root.FlashCart.features.A03;
  const rawProducts = productFeature.products();
  const catalog = rawProducts.map((item, index) => ({ ...item, category: index ? 'Home' : 'Carry', variants: index ? ['Clay', 'Ink'] : ['Moss', 'Sand'], description: index ? 'A stackable mug for long desk sessions.' : 'A weather-ready day pack with a calm interior.' }));
  const state = { query: '', category: 'All', sort: 'featured', page: 1, selected: {}, cart: [], promo: '', wishlist: new Set(), order: null };
  function node(tag, attributes, text) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes || {})) {
      if (key === 'class') element.className = value;
      else if (key === 'type') element.type = value;
      else if (key === 'value') element.value = value;
      else if (key === 'checked') element.checked = Boolean(value);
      else element.setAttribute(key, value);
    }
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function button(label, onClick, attributes) { const value = node('button', { type: 'button', ...(attributes || {}) }, label); value.addEventListener('click', onClick); return value; }
  function navigate(route) { location.hash = route; }
  function route() { const value = location.hash.replace(/^#/, '') || '/catalog'; return value.startsWith('/') ? value : '/catalog'; }
  function quote() { const subtotal = state.cart.reduce((sum, item) => sum + item.cents, 0); const promotion = state.promo === 'SAVE10' ? Math.round(subtotal * 0.1) : 0; return { subtotal, promotion, ...root.FlashCart.features.A06.quoteCart(subtotal, promotion) }; }
  function header() {
    const header = node('header', { class: 'shell topbar' });
    const brand = node('a', { class: 'brand', href: '#/catalog' }, 'flashcart'); brand.setAttribute('aria-label', 'FlashCart catalog');
    const nav = node('nav', { 'aria-label': 'Primary navigation' });
    [['Catalog', '/catalog'], ['Wishlist', '/wishlist'], ['Checkout', '/checkout']].forEach(([label, target]) => nav.append(node('a', { href: '#' + target }, label)));
    header.append(brand, nav); return header;
  }
  function cartPanel() {
    const summary = node('aside', { id: 'cart-summary', class: 'cart-panel', 'aria-label': 'Shopping cart' });
    const current = quote();
    summary.append(node('h2', {}, 'Cart'), node('p', { id: 'cart-count' }, state.cart.length + (state.cart.length === 1 ? ' item' : ' items')));
    summary.append(node('p', { id: 'cart-total', class: 'total' }, money(current.totalCents)));
    const promo = node('label', { class: 'promo' }, 'Promo code');
    const input = node('input', { id: 'promo-code', type: 'text', value: state.promo, autocomplete: 'off' });
    promo.append(input, button('Apply', () => { state.promo = input.value.trim().toUpperCase(); render(); }));
    summary.append(promo, node('p', { id: 'promo-status' }, state.promo === 'SAVE10' ? 'SAVE10 applied' : 'Use SAVE10 for 10% off'));
    summary.append(button('Secure checkout', () => navigate('/checkout'), { id: 'checkout-button' }));
    return summary;
  }
  function productCard(product) {
    const card = node('article', { class: 'product-card' });
    card.append(node('p', { class: 'eyebrow' }, product.category), node('h2', {}, product.name), node('p', {}, product.description), node('p', {}, money(product.cents) + ' · ' + product.inventory + ' in stock'));
    card.append(button('View details', () => navigate('/product/' + product.id), { 'aria-label': 'View details for ' + product.name }));
    card.append(button(state.wishlist.has(product.id) ? 'Saved' : 'Save to wishlist', () => { state.wishlist.has(product.id) ? state.wishlist.delete(product.id) : state.wishlist.add(product.id); render(); }, { class: 'quiet', 'aria-pressed': String(state.wishlist.has(product.id)) }));
    card.append(button('Add to cart', () => { state.cart.push({ ...product, variant: product.variants[0] }); render(); }, { class: 'add-cart' }));
    return card;
  }
  function catalogPage() {
    const page = node('section', { class: 'catalog-page' });
    page.append(node('p', { class: 'eyebrow' }, 'Offline storefront'), node('h1', {}, 'Everyday gear with a lighter footprint.'));
    const controls = node('div', { class: 'controls', 'aria-label': 'Catalog filters' });
    const searchLabel = node('label', {}, 'Search products'); const search = node('input', { id: 'search', type: 'search', value: state.query, autocomplete: 'off' }); search.addEventListener('input', () => { state.query = search.value; state.page = 1; render(); }); searchLabel.append(search);
    const categoryLabel = node('label', {}, 'Category'); const category = node('select', { id: 'category-filter' }); ['All', 'Carry', 'Home'].forEach((item) => { const option = node('option', { value: item }, item); option.selected = item === state.category; category.append(option); }); category.addEventListener('change', () => { state.category = category.value; state.page = 1; render(); }); categoryLabel.append(category);
    const sortLabel = node('label', {}, 'Sort'); const sort = node('select', { id: 'sort' }); [['featured', 'Featured'], ['price-asc', 'Price: low to high'], ['price-desc', 'Price: high to low']].forEach(([value, label]) => { const option = node('option', { value }, label); option.selected = value === state.sort; sort.append(option); }); sort.addEventListener('change', () => { state.sort = sort.value; render(); }); sortLabel.append(sort);
    controls.append(searchLabel, categoryLabel, sortLabel); page.append(controls);
    let products = root.FlashCart.features.A05.search(catalog, state.query).filter((item) => state.category === 'All' || item.category === state.category);
    if (state.sort === 'price-asc') products = [...products].sort((a, b) => a.cents - b.cents); if (state.sort === 'price-desc') products = [...products].sort((a, b) => b.cents - a.cents);
    page.append(node('p', { id: 'result-count', role: 'status' }, products.length + (products.length === 1 ? ' result' : ' results')));
    const grid = node('div', { id: 'product-grid', class: 'grid' });
    if (!products.length) grid.append(node('p', { id: 'empty-state' }, 'No products match those filters. Try a different search.')); else products.forEach((product) => grid.append(productCard(product)));
    page.append(grid); return page;
  }
  function productPage(id) {
    const product = catalog.find((item) => item.id === id); if (!product) return notFound(); const page = node('section', { class: 'detail-page' });
    page.append(button('Back to catalog', () => navigate('/catalog'), { class: 'quiet' }), node('p', { class: 'eyebrow' }, product.category), node('h1', {}, product.name), node('p', {}, product.description), node('p', { class: 'total' }, money(product.cents)));
    const label = node('label', {}, 'Variant'); const select = node('select', { id: 'variant-select' }); product.variants.forEach((variant) => { const option = node('option', { value: variant }, variant); option.selected = variant === (state.selected[id] || product.variants[0]); select.append(option); }); select.addEventListener('change', () => { state.selected[id] = select.value; }); label.append(select); page.append(label);
    page.append(button('Add selected variant to cart', () => { state.cart.push(root.FlashCart.features.A04.selectVariant(product, state.selected[id] || product.variants[0])); navigate('/catalog'); }, { id: 'add-variant' })); return page;
  }
  function wishlistPage() { const page = node('section', {}); page.append(node('h1', {}, 'Wishlist')); const saved = catalog.filter((item) => state.wishlist.has(item.id)); if (!saved.length) page.append(node('p', { id: 'wishlist-empty' }, 'Your wishlist is ready when you are.')); else { const list = node('div', { id: 'wishlist-list', class: 'grid' }); saved.forEach((product) => list.append(productCard(product))); page.append(list); } return page; }
  function checkoutPage() {
    if (state.order) return receiptPage(); const page = node('section', { class: 'checkout-page' }); page.append(node('h1', {}, 'Secure checkout'), node('p', {}, 'Step 1 of 2 — delivery details'));
    const form = node('form', { id: 'checkout-form', novalidate: '' }); const values = [['email', 'Email', 'email'], ['address', 'Address', 'text'], ['postal', 'Postal code', 'text']]; values.forEach(([name, label, type]) => { const field = node('label', {}, label); field.append(node('input', { id: name, name, type, required: '' })); form.append(field); });
    const errors = node('p', { id: 'checkout-errors', role: 'alert' }); form.append(errors, button('Review order', () => { const formData = Object.fromEntries(new FormData(form)); const result = root.FlashCart.features.A08.validateCheckout(formData); if (!result.ok) { errors.textContent = 'Complete: ' + result.missing.join(', '); return; } state.order = { id: 'FC-0001', email: formData.email, quote: quote() }; render(); }, { id: 'review-order' })); page.append(form); return page;
  }
  function receiptPage() { const page = node('section', { id: 'receipt', class: 'receipt' }); page.append(node('p', { class: 'eyebrow' }, 'Order confirmed'), node('h1', {}, 'Thank you — order ' + state.order.id), node('p', {}, 'A receipt was prepared for ' + state.order.email + '.'), node('p', { class: 'total' }, money(state.order.quote.totalCents)), button('Continue shopping', () => { state.cart = []; state.promo = ''; state.order = null; navigate('/catalog'); })); return page; }
  function notFound() { const page = node('section', { id: 'not-found' }); page.append(node('h1', {}, 'That page is not in this cart.'), button('Return to catalog', () => navigate('/catalog'))); return page; }
  function render() { const current = route(); const frame = node('div', {}); frame.append(header()); const layout = node('div', { class: 'shell storefront-layout' }); if (current === '/catalog' || current === '/') layout.append(catalogPage()); else if (current === '/wishlist') layout.append(wishlistPage()); else if (current === '/checkout') layout.append(checkoutPage()); else if (current.startsWith('/product/')) layout.append(productPage(current.slice('/product/'.length))); else layout.append(notFound()); layout.append(cartPanel()); frame.append(layout); app.replaceChildren(frame); }
  window.addEventListener('hashchange', render); render();
})(globalThis);
