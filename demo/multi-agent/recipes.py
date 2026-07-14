"""Deterministic, reviewable FlashCart payload and plan recipes.

This deliberately is *not* a workflow language.  The small named helpers
below build ordinary JSON records from explicit A01--A10 data.  Runtime IDs are
never baked into command strings; the runner resolves symbolic references.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


ANCHOR = "set -eu\ncd /workspace\nprintf '__DEMO_READY__\\n'\nIFS= read -r action\n[ \"$action\" = publish ]"
AGENT_IDS = tuple(f"A{number:02d}" for number in range(1, 11))


@dataclass(frozen=True)
class Agent:
    id: str
    slug: str
    role: str
    summary: str
    implementation: str


AGENTS = (
    Agent("A01", "foundation", "Foundation", "routing, shell, and local server", "routeFor(path) { return ['/', '/catalog', '/checkout'].includes(path) ? path : '/not-found'; }"),
    Agent("A02", "design-system", "Design system", "tokens, layout, and focus treatment", "tokens() { return { accent: '#175b50', surface: '#fffdf8', focus: '#124ea0' }; }"),
    Agent("A03", "products", "Product data", "deterministic products, variants, and inventory", "products() { return [{ id: 'trail-pack', name: 'Trail Pack', cents: 7400, inventory: 8 }, { id: 'studio-mug', name: 'Studio Mug', cents: 2600, inventory: 11 }]; }"),
    Agent("A04", "catalog", "Catalog and PDP", "catalog cards and variant selection", "selectVariant(product, variant) { return { ...product, variant, selectable: Boolean(variant) }; }"),
    Agent("A05", "search", "Search and facets", "query, URL facets, sort, and empty states", "search(items, query) { const needle = String(query).toLowerCase(); return items.filter((item) => item.name.toLowerCase().includes(needle)); }"),
    Agent("A06", "cart", "Cart and pricing", "integer-money cart, promotion, tax, and shipping", "quoteCart(subtotalCents, promotionCents = 0) { const config = root.FlashCart.config; const discounted = Math.max(0, subtotalCents - promotionCents); const shippingCents = discounted >= config.freeShippingCents ? 0 : config.standardShippingCents; return { discounted, shippingCents, taxCents: Math.round(discounted * config.taxRate), totalCents: discounted + shippingCents + Math.round(discounted * config.taxRate) }; }"),
    Agent("A07", "wishlist", "Wishlist", "persistent wishlist and recommendations", "toggleWishlist(ids, id) { const next = new Set(ids); next.has(id) ? next.delete(id) : next.add(id); return [...next].sort(); }"),
    Agent("A08", "checkout", "Checkout", "validated checkout, review, and receipt", "validateCheckout(form) { const missing = ['email', 'address', 'postal'].filter((key) => !String(form[key] || '').trim()); return { ok: missing.length === 0, missing }; }"),
    Agent("A09", "accessibility", "Accessibility and performance", "keyboard, live regions, reduced motion, and budgets", "auditDocument(documentLike) { return { hasMain: Boolean(documentLike && documentLike.main), reducedMotion: true, maxImageBytes: 120000 }; }"),
    Agent("A10", "qa", "Integration QA", "cross-feature status and final regression", "status(registry) { return { ready: Object.values(registry).every(Boolean), featureCount: Object.values(registry).filter(Boolean).length }; }"),
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_ref(agent: str, name: str) -> str:
    return f"payloads/{agent}/{name}"


def bootstrap_files() -> dict[str, str]:
    registry = "\n".join(["window.FlashCart = window.FlashCart || {};", "window.FlashCart.registry = {"] + [f"  {agent}: null," for agent in AGENT_IDS] + ["};", ""])
    return {
        "package.json": canonical_json({
            "name": "flashcart-offline-demo",
            "private": True,
            "type": "module",
            "scripts": {"test": "node --test tests/*.test.mjs", "serve": "node scripts/serve.mjs"},
        }),
        "src/config.js": "window.FlashCart = window.FlashCart || {};\nwindow.FlashCart.config = { freeShippingCents: 5000, standardShippingCents: 700, taxRate: 0.08 };\n",
        "src/registry.js": registry,
    }


def index_html() -> str:
    styles = "\n".join(["    <link rel=\"stylesheet\" href=\"./src/styles.css\">", *[f"    <link rel=\"stylesheet\" href=\"./src/features/{agent.id}-{agent.slug}.css\">" for agent in AGENTS[1:]]])
    scripts = "\n".join(["    <script src=\"./src/config.js\"></script>", "    <script src=\"./src/registry.js\"></script>", *[f"    <script src=\"./src/features/{agent.id}-{agent.slug}.js\"></script>" for agent in AGENTS], "    <script src=\"./src/app.js\"></script>"])
    return f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <meta name=\"description\" content=\"FlashCart offline storefront\">
    <title>FlashCart — offline storefront</title>
{styles}
  </head>
  <body>
    <a class=\"skip\" href=\"#app\">Skip to storefront</a>
    <main id=\"app\" tabindex=\"-1\" aria-live=\"polite\"></main>
    <noscript>FlashCart needs JavaScript enabled for its offline checkout demo.</noscript>
{scripts}
  </body>
</html>
"""


def app_js() -> str:
    return """(function (root) {
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
"""


def server_js() -> str:
    return """import { createReadStream, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, resolve, sep } from 'node:path';

const root = resolve(process.cwd());
const port = Number(process.env.PORT || 4173);
const host = process.env.HOST || '0.0.0.0';
const types = { '.css': 'text/css; charset=utf-8', '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml' };
const server = createServer((request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, 'http://flashcart.local').pathname);
  const file = resolve(root, pathname === '/' ? 'index.html' : '.' + pathname);
  if (!file.startsWith(root + sep) && file !== root) return response.writeHead(403).end('forbidden');
  try {
    if (!statSync(file).isFile()) throw new Error('not file');
    response.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    createReadStream(file).pipe(response);
  } catch { response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('not found'); }
});
process.stdin.setEncoding('utf8');
process.stdin.on('data', (value) => {
  if (value.trim() === 'stop') server.close(() => process.exit(0));
});
server.listen(port, host, () => console.log(`FLASHCART_READY ${host}:${port}`));
"""


def base_style() -> str:
    return """:root { color-scheme: light; --ink: #182522; --muted: #566560; --accent: #175b50; --paper: #fffdf8; --panel: #ffffff; --line: #d7ded9; --focus: #124ea0; --warm: #c45d28; font: 16px/1.5 system-ui, sans-serif; } * { box-sizing: border-box; } body { margin: 0; min-width: 320px; overflow-x: hidden; background: var(--paper); color: var(--ink); } .shell { max-width: 1180px; margin: auto; padding: 1rem 1.25rem; } .topbar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; border-bottom: 1px solid var(--line); } .brand { color: var(--accent); font-size: 1.45rem; font-weight: 850; letter-spacing: -.04em; text-decoration: none; } nav { display: flex; flex-wrap: wrap; gap: 1rem; } nav a { color: var(--ink); font-weight: 650; text-underline-offset: .22em; } .storefront-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(245px, 300px); gap: 2rem; align-items: start; } h1 { max-width: 18ch; line-height: 1.05; font-size: clamp(2rem, 5vw, 4rem); letter-spacing: -.06em; } h2 { margin-block: .25rem; } .eyebrow { color: var(--accent); font-size: .8rem; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; } .controls { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .75rem; margin-block: 2rem 1rem; } label { display: grid; gap: .35rem; font-weight: 650; } input, select, button { font: inherit; min-height: 44px; } input, select { width: 100%; border: 1px solid var(--line); border-radius: .4rem; background: white; color: var(--ink); padding: .5rem .65rem; } button { border: 0; border-radius: .4rem; background: var(--accent); color: white; cursor: pointer; padding: .5rem .8rem; } button:hover { background: #10453d; } button.quiet { margin-top: .55rem; background: transparent; border: 1px solid var(--accent); color: var(--accent); } .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 1rem; } .product-card, .cart-panel, .receipt { border: 1px solid var(--line); border-radius: .9rem; background: var(--panel); padding: 1rem; box-shadow: 0 8px 22px rgb(24 37 34 / 6%); } .product-card { display: grid; align-content: start; gap: .35rem; } .cart-panel { position: sticky; top: 1rem; } .total { font-size: 1.35rem; font-weight: 800; } .promo { margin-block: 1rem; } .promo button { margin-top: .3rem; } .checkout-page form { display: grid; gap: 1rem; max-width: 36rem; } #checkout-errors { color: #a82020; font-weight: 700; min-height: 1.5em; } :focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; } .skip { position: absolute; left: -999px; top: 0; } .skip:focus { left: 1rem; top: 1rem; z-index: 3; background: white; padding: .5rem; } @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; } } @media (max-width: 720px) { .topbar, .storefront-layout { align-items: stretch; grid-template-columns: 1fr; } .topbar { flex-direction: column; } .cart-panel { position: static; } .controls { grid-template-columns: 1fr; } }\n"""


def _feature_core(agent: Agent, implementation: str) -> str:
    return f"""(function (root) {{
  root.FlashCart = root.FlashCart || {{}};
  root.FlashCart.features = root.FlashCart.features || {{}};
  root.FlashCart.features.{agent.id} = {{
    id: '{agent.id}',
    label: '{agent.role}',
    deterministic: true,
    verify() {{ return true; }},
    {implementation}
  }};
}})(globalThis);
"""


def _broken_core(agent: Agent) -> str:
    return f"""(function (root) {{
  root.FlashCart = root.FlashCart || {{}};
  root.FlashCart.features = root.FlashCart.features || {{}};
  root.FlashCart.features.{agent.id} = {{ id: '{agent.id}', verify() {{ return false; }}, implementation: 'broken {agent.id}' }};
}})(globalThis);
"""


def _data_js(agent: Agent) -> str:
    return f"""(function (root) {{
  root.FlashCart = root.FlashCart || {{}};
  root.FlashCart.fixtures = root.FlashCart.fixtures || {{}};
  root.FlashCart.fixtures.{agent.id} = Object.freeze({{ owner: '{agent.id}', label: '{agent.role}', revision: 1 }});
}})(globalThis);
"""


def _view_js(agent: Agent) -> str:
    return f"""(function (root) {{
  root.FlashCart = root.FlashCart || {{}};
  root.FlashCart.views = root.FlashCart.views || {{}};
  root.FlashCart.views.{agent.id} = function () {{ return '<section data-feature="{agent.id}"><h2>{agent.role}</h2><p>{agent.summary}</p></section>'; }};
}})(globalThis);
"""


def _a11y_js(agent: Agent) -> str:
    return f"""(function (root) {{
  root.FlashCart = root.FlashCart || {{}};
  root.FlashCart.a11y = root.FlashCart.a11y || {{}};
  root.FlashCart.a11y.{agent.id} = {{ label: '{agent.role}', keyboard: true, reducedMotion: true }};
}})(globalThis);
"""


def _feature_style(agent: Agent) -> str:
    return f".feature-{agent.id.lower()} {{ border-inline-start: 4px solid #175b50; }} .feature-{agent.id.lower()} :focus-visible {{ outline: 3px solid #124ea0; }}\n"


def _test_prelude(path: str) -> str:
    return f"import assert from 'node:assert/strict';\nimport test from 'node:test';\nawait import('../{path}');\n"


def _red_test(agent: Agent, core_path: str) -> str:
    return _test_prelude(core_path) + f"test('{agent.id} target settles after repair', () => assert.equal(globalThis.FlashCart.features.{agent.id}.verify(), true, '{agent.id} implementation should report verified'));\n"


def _data_test(agent: Agent, path: str) -> str:
    return _test_prelude(path) + f"test('{agent.id} fixture is deterministic', () => assert.equal(globalThis.FlashCart.fixtures.{agent.id}.owner, '{agent.id}'));\n"


def _view_test(agent: Agent, path: str) -> str:
    return _test_prelude(path) + f"test('{agent.id} view has a semantic section', () => assert.match(globalThis.FlashCart.views.{agent.id}(), /<section/));\n"


def _style_test(agent: Agent, css_path: str) -> str:
    return f"import assert from 'node:assert/strict';\nimport {{ readFile }} from 'node:fs/promises';\nimport test from 'node:test';\ntest('{agent.id} style keeps focus visible', async () => assert.match(await readFile(new URL('../{css_path}', import.meta.url), 'utf8'), /focus-visible/));\n"


def _a11y_test(agent: Agent, path: str) -> str:
    return _test_prelude(path) + f"test('{agent.id} honors reduced motion', () => assert.equal(globalThis.FlashCart.a11y.{agent.id}.reducedMotion, true));\n"


def _manifest_test(agent: Agent, path: str) -> str:
    return f"import assert from 'node:assert/strict';\nimport {{ readFile }} from 'node:fs/promises';\nimport test from 'node:test';\ntest('{agent.id} manifest names its owner', async () => assert.equal(JSON.parse(await readFile(new URL('../{path}', import.meta.url), 'utf8')).owner, '{agent.id}'));\n"


def _quality_test(agent: Agent, core_path: str) -> str:
    return _test_prelude(core_path) + f"test('{agent.id} implementation is marked deterministic', () => assert.equal(globalThis.FlashCart.features.{agent.id}.deterministic, true));\n"


def paths_for(agent: Agent) -> dict[str, str]:
    prefix = f"src/features/{agent.id}-{agent.slug}"
    paths = {
        "core": f"{prefix}.js",
        "data": f"{prefix}-data.js",
        "view": f"{prefix}-view.js",
        "style": f"{prefix}.css",
        "a11y": f"{prefix}-a11y.js",
        "manifest": f"src/features/{agent.id}-{agent.slug}.json",
    }
    if agent.id == "A01":
        paths.update({"data": "index.html", "view": "src/app.js", "style": "src/styles.css", "a11y": "scripts/serve.mjs"})
    return paths


def payloads_for(agent: Agent) -> dict[str, str]:
    paths = paths_for(agent)
    core_final = _feature_core(agent, agent.implementation)
    core_broken = _broken_core(agent)
    core_edit = canonical_json([{"old_string": core_broken, "new_string": core_final}])
    data = index_html() if agent.id == "A01" else _data_js(agent)
    view = app_js() if agent.id == "A01" else _view_js(agent)
    style = base_style() if agent.id == "A01" else _feature_style(agent)
    a11y = server_js() if agent.id == "A01" else _a11y_js(agent)
    manifest = canonical_json({"owner": agent.id, "role": agent.role, "paths": paths, "schema_version": 1})
    registry_edit = canonical_json([{"old_string": f"  {agent.id}: null,", "new_string": f"  {agent.id}: {{ role: '{agent.role}', entry: '{paths['core']}' }},"}])
    data_test = _data_test(agent, paths["data"])
    view_test = _view_test(agent, paths["view"])
    a11y_test = _a11y_test(agent, paths["a11y"])
    if agent.id == "A01":
        data_test = "import assert from 'node:assert/strict';\nimport { readFile } from 'node:fs/promises';\nimport test from 'node:test';\ntest('A01 fixture is deterministic', async () => assert.match(await readFile(new URL('../index.html', import.meta.url), 'utf8'), /<main/));\n"
        view_test = "import assert from 'node:assert/strict';\nimport { readFile } from 'node:fs/promises';\nimport test from 'node:test';\ntest('A01 view has a semantic section', async () => assert.match(await readFile(new URL('../src/app.js', import.meta.url), 'utf8'), /product-grid/));\n"
        a11y_test = "import assert from 'node:assert/strict';\nimport { readFile } from 'node:fs/promises';\nimport test from 'node:test';\ntest('A01 honors reduced motion', async () => assert.match(await readFile(new URL('../scripts/serve.mjs', import.meta.url), 'utf8'), /FLASHCART_READY/));\n"
    payloads = {
        "01-broken.js": core_broken,
        "02-red.test.mjs": _red_test(agent, paths["core"]),
        "03-fix.json": core_edit,
        "04-data": data,
        "05-data.test.mjs": data_test,
        "06-view": view,
        "07-view.test.mjs": view_test,
        "08-style": style,
        "09-style.test.mjs": _style_test(agent, paths["style"]),
        "10-a11y": a11y,
        "11-a11y.test.mjs": a11y_test,
        "12-manifest.json": manifest,
        "13-manifest.test.mjs": _manifest_test(agent, paths["manifest"]),
        "14-quality.test.mjs": _quality_test(agent, paths["core"]),
        "15-registry-edit.json": registry_edit,
    }
    if agent.id == "A06":
        old = bootstrap_files()["src/config.js"]
        new = old.replace("freeShippingCents: 5000", "freeShippingCents: 6000")
        payloads["16-conflict-winner-edit.json"] = canonical_json([{"old_string": old, "new_string": new}])
    if agent.id == "A08":
        payloads["17-retry-receipt.json"] = canonical_json({"owner": "A08", "kind": "fresh-head-retry", "receipt": "checkout conflict retried from A06's published threshold"})
    return payloads


def _record(agent: Agent, ordinal: int, *, scene: str, phase: str, category: str, purpose: str, op: str, args: dict[str, Any], expect: dict[str, Any], after: list[str], attempt_ref: str, effects: list[str] | None = None, bind: dict[str, str] | None = None, test_cycle: str | None = None, final_regression: bool = False, command_ref: str | None = None, workspace_ref: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": 1,
        "id": f"{agent.id}.{ordinal:03d}",
        "agent": agent.id,
        "ordinal": ordinal,
        "scene": scene,
        "phase": phase,
        "category": category,
        "purpose": purpose,
        "op": op,
        "args": args,
        "expect": expect,
        "after": after,
        "attempt_ref": attempt_ref,
    }
    if effects is not None:
        row["effects"] = {"paths": effects}
    if bind is not None:
        row["bind"] = bind
    if test_cycle is not None:
        row["test_cycle"] = test_cycle
        row["expect"] = dict(expect)
        row["expect"].setdefault("inventory_ref", f"test-inventory.json#{test_cycle}")
    if final_regression:
        row["final_regression"] = True
    if command_ref is not None:
        row["command_ref"] = command_ref
    if workspace_ref is not None:
        row["workspace_ref"] = workspace_ref
    return row


def _payload_args(agent: Agent, name: str, path: str, *, edit: bool = False) -> dict[str, Any]:
    content = payloads_for(agent)[name]
    key = "edits_from" if edit else "body_from"
    return {"path": path, key: payload_ref(agent.id, name), "payload_sha256": digest_text(content)}


def _javascript_checks(paths: list[str]) -> str:
    javascript = [path for path in paths if path.endswith((".js", ".mjs"))]
    if not javascript:
        raise ValueError("a syntax check needs a JavaScript artifact")
    return " && ".join(f"node --check {path}" for path in javascript)


def _artifact_check(path: str) -> str:
    if path.endswith((".js", ".mjs")):
        return _javascript_checks([path])
    if path.endswith(".html"):
        return "node --input-type=module --eval \"import { readFile } from 'node:fs/promises'; const html = await readFile('index.html', 'utf8'); if (!html.includes('<main')) process.exit(1);\""
    raise ValueError(f"no deterministic artifact check for {path}")


def primary_plan(agent: Agent) -> list[dict[str, Any]]:
    """One explicit 44-call recipe. Every test command differs by test file."""
    paths = paths_for(agent)
    attempt = f"{agent.id}.primary"
    workspace = f"{attempt}.workspace"
    anchor = f"{attempt}.anchor"
    rows: list[dict[str, Any]] = []
    def add(**kwargs: Any) -> None:
        kwargs.setdefault("workspace_ref", workspace)
        rows.append(_record(agent, len(rows) + 1, attempt_ref=attempt, **kwargs))
    add(scene="fanout", phase="anchor", category="workspace_control", purpose="Hold the primary workspace open behind the publication gate", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["bootstrap-published"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
    for path, purpose, expected in (("package.json", "Inspect the standard-library-only package contract", "file_read"), ("src/registry.js", "Inspect the unclaimed feature registry line", "file_read"), ("src/config.js", "Inspect the seeded commerce boundary", "file_read"), (paths["core"], "Prove this workspace starts without its agent-owned implementation", "not_found")):
        add(scene="fanout", phase="context", category="inspect", purpose=purpose, op="file_read", args={"path": path}, expect={"kind": expected}, after=["all-primary-workspaces-ready"])
    add(scene="fanout", phase="red", category="patch", purpose="Create the deliberately incomplete feature contract", op="file_write", args=_payload_args(agent, "01-broken.js", paths["core"]), expect={"kind": "file_write"}, after=["all-primary-workspaces-ready"], effects=[paths["core"]])
    red_path = f"tests/{agent.id}-red.test.mjs"
    add(scene="fanout", phase="red", category="patch", purpose="Write the exact targeted red test", op="file_write", args=_payload_args(agent, "02-red.test.mjs", red_path), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[red_path])
    cycle = f"{agent.id}.repair"
    add(scene="fanout", phase="red", category="test_debug", purpose="Prove the feature contract fails before its repair", op="exec_command", args={"command": f"node --test {red_path}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "expected_red", "child_exit_code": 1, "failing_subtests": [{"id": f"{agent.id} target settles after repair", "reason_contains": f"{agent.id} implementation should report verified"}], "forbid_output_contains": ["SyntaxError", "ERR_MODULE_NOT_FOUND", "Could not find"]}, after=[rows[-1]["id"]], test_cycle=cycle)
    add(scene="fanout", phase="diagnose", category="inspect", purpose="Read the failed implementation before applying the smallest repair", op="file_read", args={"path": paths["core"]}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
    add(scene="fanout", phase="repair", category="patch", purpose="Replace the incomplete feature contract with its deterministic implementation", op="file_edit", args=_payload_args(agent, "03-fix.json", paths["core"], edit=True), expect={"kind": "file_edit"}, after=[rows[-1]["id"]], effects=[paths["core"]])
    add(scene="fanout", phase="repair", category="build_lint", purpose="Syntax-check the repaired feature module", op="exec_command", args={"command": f"node --check {paths['core']}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
    add(scene="fanout", phase="green", category="test_debug", purpose="Prove the original red contract turns green after the repair", op="exec_command", args={"command": f"node --test {red_path}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle=cycle)
    for key, test_name, suffix, description in (("data", "05-data.test.mjs", "data", "Add deterministic feature fixtures"), ("view", "07-view.test.mjs", "view", "Add a semantic feature view")):
        data_path = paths[key]
        payload_name = "04-data" if key == "data" else "06-view"
        add(scene="fanout", phase=key, category="patch", purpose=description, op="file_write", args=_payload_args(agent, payload_name, data_path), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[data_path])
        add(scene="fanout", phase=key, category="build_lint", purpose=f"Syntax-check the {key} artifact", op="exec_command", args={"command": _artifact_check(data_path), "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
        test_path = f"tests/{agent.id}-{suffix}.test.mjs"
        add(scene="fanout", phase=key, category="patch", purpose=f"Write the {key} contract test", op="file_write", args=_payload_args(agent, test_name, test_path), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[test_path])
        add(scene="fanout", phase=key, category="test_debug", purpose=f"Run the {key} contract test", op="exec_command", args={"command": f"node --test {test_path}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle=f"{agent.id}.{suffix}")
    style_path = paths["style"]
    add(scene="fanout", phase="style", category="patch", purpose="Add responsive visible-focus feature styling", op="file_write", args=_payload_args(agent, "08-style", style_path), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[style_path])
    style_test = f"tests/{agent.id}-style.test.mjs"
    add(scene="fanout", phase="style", category="patch", purpose="Write the focus-style assertion", op="file_write", args=_payload_args(agent, "09-style.test.mjs", style_test), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[style_test])
    add(scene="fanout", phase="style", category="test_debug", purpose="Verify the shipped style keeps keyboard focus visible", op="exec_command", args={"command": f"node --test {style_test}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle=f"{agent.id}.style")
    a11y_path = paths["a11y"]
    add(scene="fanout", phase="accessibility", category="patch", purpose="Add reduced-motion and keyboard behavior metadata", op="file_write", args=_payload_args(agent, "10-a11y", a11y_path), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[a11y_path])
    add(scene="fanout", phase="accessibility", category="build_lint", purpose="Syntax-check the accessibility behavior", op="exec_command", args={"command": _javascript_checks([a11y_path]), "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
    a11y_test = f"tests/{agent.id}-a11y.test.mjs"
    add(scene="fanout", phase="accessibility", category="patch", purpose="Write the reduced-motion contract test", op="file_write", args=_payload_args(agent, "11-a11y.test.mjs", a11y_test), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[a11y_test])
    add(scene="fanout", phase="accessibility", category="test_debug", purpose="Run the accessibility behavior test", op="exec_command", args={"command": f"node --test {a11y_test}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle=f"{agent.id}.a11y")
    manifest_path = paths["manifest"]
    add(scene="fanout", phase="manifest", category="patch", purpose="Add the feature manifest used by integration QA", op="file_write", args=_payload_args(agent, "12-manifest.json", manifest_path), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[manifest_path])
    manifest_test = f"tests/{agent.id}-manifest.test.mjs"
    add(scene="fanout", phase="manifest", category="patch", purpose="Write the manifest ownership assertion", op="file_write", args=_payload_args(agent, "13-manifest.test.mjs", manifest_test), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[manifest_test])
    add(scene="fanout", phase="manifest", category="test_debug", purpose="Verify the manifest names its durable feature owner", op="exec_command", args={"command": f"node --test {manifest_test}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle=f"{agent.id}.manifest")
    for path, purpose in ((paths["core"], "Inspect the final feature implementation"), (paths["data"], "Inspect the deterministic feature data"), (paths["view"], "Inspect the rendered feature surface"), (style_path, "Inspect the final responsive style"), (a11y_path, "Inspect the keyboard and reduced-motion behavior"), (manifest_path, "Inspect the feature ownership manifest"), (f"tests/{agent.id}-red.test.mjs", "Inspect the preserved repaired-contract test")):
        add(scene="fanout", phase="review", category="inspect", purpose=purpose, op="file_read", args={"path": path}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
    add(scene="fanout", phase="review", category="build_lint", purpose="Check every owned JavaScript artifact together", op="exec_command", args={"command": _javascript_checks([paths["core"], paths["data"], paths["view"], paths["a11y"]]), "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
    quality_test = f"tests/{agent.id}-quality.test.mjs"
    add(scene="fanout", phase="review", category="patch", purpose="Write the deterministic implementation quality check", op="file_write", args=_payload_args(agent, "14-quality.test.mjs", quality_test), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=[quality_test])
    add(scene="fanout", phase="review", category="inspect", purpose="Inspect the independent quality contract", op="file_read", args={"path": quality_test}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
    add(scene="fanout", phase="review", category="test_debug", purpose="Run the independent implementation quality check", op="exec_command", args={"command": f"node --test {quality_test}", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle=f"{agent.id}.quality")
    add(scene="fanout", phase="merge", category="patch", purpose="Claim exactly this agent's registry line", op="file_edit", args=_payload_args(agent, "15-registry-edit.json", "src/registry.js", edit=True), expect={"kind": "file_edit"}, after=[rows[-1]["id"]], effects=["src/registry.js"])
    add(scene="fanout", phase="merge", category="inspect", purpose="Verify this workspace sees its claimed registry value", op="file_read", args={"path": "src/registry.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
    add(scene="merge", phase="release", category="workspace_control", purpose="Release the gated workspace only after every primary feature gate is green", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_success"}, after=["all-primary-feature-gates-green"], command_ref=anchor, workspace_ref=None)
    if len(rows) != 44:
        raise AssertionError(f"{agent.id} recipe changed its reviewed call count: {len(rows)}")
    return rows


def all_primary_plans() -> dict[str, list[dict[str, Any]]]:
    return {agent.id: primary_plan(agent) for agent in AGENTS}


def _append(rows: list[dict[str, Any]], agent: Agent, *, attempt: str, workspace: str | None, scene: str, phase: str, category: str, purpose: str, op: str, args: dict[str, Any], expect: dict[str, Any], after: list[str], effects: list[str] | None = None, bind: dict[str, str] | None = None, test_cycle: str | None = None, final_regression: bool = False, command_ref: str | None = None) -> None:
    rows.append(_record(agent, len(rows) + 1, attempt_ref=attempt, workspace_ref=workspace, scene=scene, phase=phase, category=category, purpose=purpose, op=op, args=args, expect=expect, after=after, effects=effects, bind=bind, test_cycle=test_cycle, final_regression=final_regression, command_ref=command_ref))


def _port_server(marker: str) -> str:
    return "node --input-type=module --eval \"import { mkdir, writeFile } from 'node:fs/promises'; import { createServer } from 'node:http'; await mkdir('.flashcart-network', { recursive: true }); await writeFile('.flashcart-network/" + marker + ".txt', 'ephemeral network experiment\\n'); const server = createServer((_, response) => response.end('ok')); server.once('error', (error) => { if (error.code === 'EADDRINUSE') { console.log('PORT_COLLISION'); process.exit(0); } throw error; }); process.stdin.setEncoding('utf8'); process.stdin.on('data', (value) => { if (value.trim() === 'stop') server.close(() => process.exit(0)); }); server.listen(4173, '0.0.0.0', () => console.log('PORT_BOUND " + marker + "'));\""


def extended_plan(agent: Agent) -> list[dict[str, Any]]:
    """Append the required conflict, network, and post-merge proof scenes."""
    rows = primary_plan(agent)
    if agent.id == "A06":
        attempt = "A06.conflict"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="anchor", category="conflict_network_audit", purpose="Hold A06's fresh-head conflict contender until the atomic publish gate", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["all-primary-published"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="inspect", category="inspect", purpose="Read the seeded shipping threshold on the common conflict head", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="winner-edit", category="patch", purpose="Set the conflict-winning free-shipping threshold with file_edit", op="file_edit", args=_payload_args(agent, "16-conflict-winner-edit.json", "src/config.js", edit=True), expect={"kind": "file_edit"}, after=[rows[-1]["id"]], effects=["src/config.js"])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="winner-check", category="build_lint", purpose="Syntax-check the conflict-winning commerce configuration", op="exec_command", args={"command": "node --check src/config.js", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="winner-review", category="inspect", purpose="Confirm the contender sees the 6000-cent threshold before release", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="winner-publish", category="conflict_network_audit", purpose="Publish A06's conflict winner before A08 attempts its stale source", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_success"}, after=["conflict-contenders-mutated"], command_ref=anchor)
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="winner-shared-read", category="inspect", purpose="Read the shared threshold after the winning publication", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="winner-blame", category="conflict_network_audit", purpose="Prove raw line ownership belongs to the A06 conflict winner", op="file_blame", args={"path": "src/config.js", "line_start": 2, "line_end": 2}, expect={"kind": "blame_owner", "owner_agent": "A06", "owner_attempt": "A06.conflict"}, after=[rows[-1]["id"]])
    if agent.id == "A08":
        attempt = "A08.conflict"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="anchor", category="conflict_network_audit", purpose="Hold A08's stale-head conflict contender until the rejection gate", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["all-primary-published"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="inspect", category="inspect", purpose="Read the original shipping boundary from A08's stale source", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="stale-exec-edit", category="patch", purpose="Use exec_command to change the seeded line and create an unrelated stale artifact", op="exec_command", args={"command": "node --input-type=module --eval \"import { mkdir, readFile, writeFile } from 'node:fs/promises'; const path = 'src/config.js'; const value = await readFile(path, 'utf8'); await writeFile(path, value.replace('freeShippingCents: 5000', 'freeShippingCents: 7500')); await mkdir('src/conflict', { recursive: true }); await writeFile('src/conflict/A08-stale-attempt.txt', 'must not publish\\n');\"", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], effects=["src/config.js", "src/conflict/A08-stale-attempt.txt"])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="stale-review", category="inspect", purpose="Confirm A08's stale workspace contains its divergent threshold", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="rejected-publish", category="conflict_network_audit", purpose="Require atomic source-conflict rejection for A08's stale publication", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_reject", "publish_reject_class": "source_conflict"}, after=["A06.050"], command_ref=anchor)
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="shared-threshold", category="inspect", purpose="Prove the rejected attempt did not advance the shared threshold", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="shared-blame", category="conflict_network_audit", purpose="Prove rejected A08 work did not replace the A06 raw owner", op="file_blame", args={"path": "src/config.js", "line_start": 2, "line_end": 2}, expect={"kind": "blame_owner", "owner_agent": "A06", "owner_attempt": "A06.conflict"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="no-partial-file", category="inspect", purpose="Prove the unrelated stale artifact never reached shared content", op="file_read", args={"path": "src/conflict/A08-stale-attempt.txt"}, expect={"kind": "not_found"}, after=[rows[-1]["id"]])
        attempt = "A08.retry"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-anchor", category="conflict_network_audit", purpose="Open a new A08 workspace from the post-winner head", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["A08.052"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-read", category="inspect", purpose="Confirm the retry starts from A06's published threshold", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-patch", category="patch", purpose="Add the durable post-conflict checkout retry receipt", op="file_write", args=_payload_args(agent, "17-retry-receipt.json", "src/features/A08-checkout-retry.json"), expect={"kind": "file_write"}, after=[rows[-1]["id"]], effects=["src/features/A08-checkout-retry.json"])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-review", category="inspect", purpose="Read the durable retry receipt before its clean publication", op="file_read", args={"path": "src/features/A08-checkout-retry.json"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="retry-publish", category="conflict_network_audit", purpose="Publish the fresh-head A08 retry successfully", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_success"}, after=[rows[-1]["id"]], command_ref=anchor)
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="retry-shared-read", category="inspect", purpose="Verify the successful retry receipt is shared", op="file_read", args={"path": "src/features/A08-checkout-retry.json"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="retry-blame", category="conflict_network_audit", purpose="Prove the retry receipt has raw A08 ownership", op="file_blame", args={"path": "src/features/A08-checkout-retry.json", "line_start": 1, "line_end": 1}, expect={"kind": "blame_owner", "owner_agent": "A08", "owner_attempt": "A08.retry"}, after=[rows[-1]["id"]])
    if agent.id == "A09":
        for attempt, marker, after in (("A09.network.shared1", "shared-one", ["all-primary-published"]), ("A09.network.shared2", "shared-two", ["A09.045"]), ("A09.network.isolated1", "isolated-one", ["A09.046"]), ("A09.network.isolated2", "isolated-two", ["A09.047"])):
            workspace = f"{attempt}.workspace"
            command = f"{attempt}.server"
            kind = "command_ok" if attempt.endswith("shared2") else "command_running"
            expect = {"kind": kind}
            if kind == "command_ok":
                expect["output_contains"] = ["PORT_COLLISION"]
            # The collision process exits before a command session exists;
            # only successful servers can be bound for later probes and
            # explicit stdin shutdown.
            bind = {"command_session_id": command} if kind == "command_running" else None
            _append(rows, agent, attempt=attempt, workspace=workspace, scene="network", phase="port-bind", category="conflict_network_audit", purpose=f"Bind or observe the port-4173 {marker} trusted-session experiment", op="exec_command", args={"command": _port_server(marker), "timeout_ms": 60000, "yield_time_ms": 0 if kind == "command_running" else 30000}, expect=expect, after=after, effects=[f".flashcart-network/{marker}.txt"], bind=bind)
        for attempt, start_id in (("A09.network.shared1", "A09.045"), ("A09.network.isolated1", "A09.047"), ("A09.network.isolated2", "A09.048")):
            _append(rows, agent, attempt=attempt, workspace=None, scene="network", phase="port-ready", category="conflict_network_audit", purpose="Read the running server readiness marker without publishing experiment files", op="read_command_lines", args={"max_lines": 20, "wait_ms": 5000}, expect={"kind": "command_running", "output_contains": ["PORT_BOUND"]}, after=[start_id], command_ref=f"{attempt}.server")
        for attempt in ("A09.network.shared1", "A09.network.isolated1", "A09.network.isolated2"):
            _append(rows, agent, attempt=attempt, workspace=None, scene="network", phase="port-stop", category="conflict_network_audit", purpose="Stop the trusted-session port server before its explicit session is destroyed", op="write_command_stdin", args={"input": "stop\n", "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], command_ref=f"{attempt}.server")
    if agent.id == "A10":
        attempt = "A10.final"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-anchor", category="workspace_control", purpose="Open a fresh post-merge A10 regression workspace", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["network-experiment-clean"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-context", category="inspect", purpose="Read the fully merged feature registry from the fresh final workspace", op="file_read", args={"path": "src/registry.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-syntax", category="build_lint", purpose="Syntax-check the final offline storefront entry points", op="exec_command", args={"command": "node --check src/app.js && node --check scripts/serve.mjs", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-regression", category="test_debug", purpose="Run the frozen exact-inventory final regression from a fresh post-merge workspace", op="exec_command", args={"command": "node --test tests/*.test.mjs", "timeout_ms": 120000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle="A10.final-regression", final_regression=True)
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-tree-read", category="inspect", purpose="Read the retained storefront shell after final regression", op="file_read", args={"path": "index.html"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        preview = f"{attempt}.preview"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="preview-start", category="test_debug", purpose="Start the final offline storefront for a retained trusted-preview capture", op="exec_command", args={"command": "node scripts/serve.mjs", "timeout_ms": 120000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=[rows[-1]["id"]], bind={"command_session_id": preview})
        _append(rows, agent, attempt=attempt, workspace=None, scene="evidence", phase="preview-ready", category="test_debug", purpose="Read the final preview readiness marker before the trusted host probe", op="read_command_lines", args={"max_lines": 20, "wait_ms": 5000}, expect={"kind": "command_running", "output_contains": ["FLASHCART_READY"]}, after=[rows[-1]["id"]], command_ref=preview)
        _append(rows, agent, attempt=attempt, workspace=None, scene="evidence", phase="preview-stop", category="workspace_control", purpose="Stop the retained preview before closing the final workspace", op="write_command_stdin", args={"input": "stop\n", "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], command_ref=preview)
        _append(rows, agent, attempt=attempt, workspace=None, scene="evidence", phase="final-release", category="workspace_control", purpose="Close the clean final regression workspace with an explicit no-op publication", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_noop"}, after=[rows[-1]["id"]], command_ref=anchor)
    return rows


def all_plans() -> dict[str, list[dict[str, Any]]]:
    return {agent.id: extended_plan(agent) for agent in AGENTS}


def all_payloads() -> dict[str, str]:
    files: dict[str, str] = {}
    for agent in AGENTS:
        for name, content in payloads_for(agent).items():
            files[payload_ref(agent.id, name)] = content
    return files


def inventory() -> dict[str, Any]:
    tests: dict[str, Any] = {}
    for agent in AGENTS:
        for suffix, title in (("repair", f"{agent.id} target settles after repair"), ("data", f"{agent.id} fixture is deterministic"), ("view", f"{agent.id} view has a semantic section"), ("style", f"{agent.id} style keeps focus visible"), ("a11y", f"{agent.id} honors reduced motion"), ("manifest", f"{agent.id} manifest names its owner"), ("quality", f"{agent.id} implementation is marked deterministic")):
            filename = "red" if suffix == "repair" else suffix
            tests[f"{agent.id}.{suffix}"] = {"command": f"node --test tests/{agent.id}-{filename}.test.mjs", "subtests": [title], "subtest_count": 1, "allowed": {"skip": [], "todo": [], "cancelled": []}}
    all_subtests = [title for entry in tests.values() for title in entry["subtests"]]
    tests["A10.final-regression"] = {"command": "node --test tests/*.test.mjs", "subtests": all_subtests, "subtest_count": len(all_subtests), "allowed": {"skip": [], "todo": [], "cancelled": []}}
    return {"schema_version": 1, "tests": tests}


def materialized_tree() -> dict[str, str]:
    """The independently oracle-reviewed final tree; no run data participates."""
    tree = dict(bootstrap_files())
    for agent in AGENTS:
        paths = paths_for(agent)
        payloads = payloads_for(agent)
        tree[paths["core"]] = _feature_core(agent, agent.implementation)
        tree[paths["data"]] = payloads["04-data"]
        tree[paths["view"]] = payloads["06-view"]
        tree[paths["style"]] = payloads["08-style"]
        tree[paths["a11y"]] = payloads["10-a11y"]
        tree[paths["manifest"]] = payloads["12-manifest.json"]
        for suffix, payload in (("red", "02-red.test.mjs"), ("data", "05-data.test.mjs"), ("view", "07-view.test.mjs"), ("style", "09-style.test.mjs"), ("a11y", "11-a11y.test.mjs"), ("manifest", "13-manifest.test.mjs"), ("quality", "14-quality.test.mjs")):
            tree[f"tests/{agent.id}-{suffix}.test.mjs"] = payloads[payload]
        old = f"  {agent.id}: null,"
        new = f"  {agent.id}: {{ role: '{agent.role}', entry: '{paths['core']}' }},"
        tree["src/registry.js"] = tree["src/registry.js"].replace(old, new)
    tree["src/config.js"] = tree["src/config.js"].replace("freeShippingCents: 5000", "freeShippingCents: 6000")
    tree["src/features/A08-checkout-retry.json"] = payloads_for(AGENTS[7])["17-retry-receipt.json"]
    return dict(sorted(tree.items()))


def assert_relative(path: str) -> None:
    item = PurePosixPath(path)
    if item.is_absolute() or ".." in item.parts or not path or "\\" in path:
        raise ValueError(f"unsafe relative path: {path!r}")
