"""Deterministic, reviewable FlashCart payload and plan recipes.

This deliberately is *not* a workflow language.  The small named helpers
below build ordinary JSON records from explicit A01--A10 data.  Runtime IDs are
never baked into command strings; the runner resolves symbolic references.
"""

from __future__ import annotations

import hashlib
import json
import shlex
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
    Agent("A06", "cart", "Cart and pricing", "integer-money cart, promotion, tax, and shipping", "quoteCart(subtotalCents, promotionCents = 0) { const config = globalThis.FlashCart.config; const discounted = Math.max(0, subtotalCents - promotionCents); const shippingCents = discounted >= config.freeShippingCents ? 0 : config.standardShippingCents; return { discounted, shippingCents, taxCents: Math.round(discounted * config.taxRate), totalCents: discounted + shippingCents + Math.round(discounted * config.taxRate) }; }"),
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
    registry = "\n".join(
        ["window.FlashCart = window.FlashCart || {};", "window.FlashCart.features = {"]
        + [f"  {agent}: null," for agent in AGENT_IDS]
        + ["};", "window.FlashCart.registry = window.FlashCart.features;", ""]
    )
    return {
        "index.html": index_html(),
        "src/app.js": app_js(),
        "src/config.js": "window.FlashCart = window.FlashCart || {};\nwindow.FlashCart.config = {\n  freeShippingCents: 5000,\n  standardShippingCents: 700,\n  taxRate: 0.08,\n  checkoutRetry: 'pending',\n};\n",
        "src/registry.js": registry,
        "tests/storefront.test.mjs": shared_test_mjs(),
    }


def _shared_test_agent_line(agent: Agent, *, ready: bool) -> str:
    if not ready:
        return f"// {agent.id} contribution check"
    return (
        f"test('{agent.id} {agent.role} contribution is ready', () => "
        f"assert.match(registry, /{agent.id}: \\{{.*status: 'ready'/));"
    )


def shared_test_mjs(*, ready: bool = False) -> str:
    """One collaborative test surface with stable, separately owned lines."""
    lines = [
        "import test from 'node:test';",
        "import assert from 'node:assert/strict';",
        "import { readFile } from 'node:fs/promises';",
        "",
        "const registry = await readFile(new URL('../src/registry.js', import.meta.url), 'utf8');",
        "const config = await readFile(new URL('../src/config.js', import.meta.url), 'utf8');",
        "",
        "const expectedPolicy = {",
        "  freeShippingCents: 5000,",
        "  standardShippingCents: 700,",
        "  taxRate: 0.08,",
        "  checkoutRetry: 'pending',",
        "};",
        "",
        "test('published configuration matches the shared policy', () => Object.entries(expectedPolicy).forEach(([key, value]) => assert.ok(config.includes(`${key}: ${typeof value === 'string' ? `'${value}'` : value}`))));",
        "",
        *[_shared_test_agent_line(agent, ready=ready) for agent in AGENTS],
        "",
    ]
    return "\n".join(lines)


def shared_test_line(marker: str) -> int:
    """Return a stable one-based line for authored blame assertions."""
    for number, line in enumerate(shared_test_mjs().splitlines(), 1):
        if marker in line:
            return number
    raise ValueError(f"shared test marker is missing: {marker}")


def index_html() -> str:
    style = base_style().strip()
    return f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <meta name=\"description\" content=\"FlashCart offline storefront\">
    <title>FlashCart — offline storefront</title>
    <style>
{style}
    </style>
  </head>
  <body>
    <a class=\"skip\" href=\"#app\">Skip to storefront</a>
    <main id=\"app\" tabindex=\"-1\" aria-live=\"polite\"></main>
    <noscript>FlashCart needs JavaScript enabled for its offline checkout demo.</noscript>
    <script src=\"./src/config.js\"></script>
    <script src=\"./src/registry.js\"></script>
    <script src=\"./src/app.js\"></script>
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
    """Every agent collaborates in the same durable domain file."""
    del agent
    return {key: "src/registry.js" for key in ("core", "data", "view", "style", "a11y", "manifest")}


def _registry_line(agent: Agent, stage: str) -> str:
    if stage == "pending":
        return f"  {agent.id}: null,"
    fields = [
        f"id: '{agent.id}'",
        f"role: '{agent.role}'",
        f"stage: '{stage}'",
        f"deterministic: {'false' if stage == 'broken' else 'true'}",
        f"verify() {{ return {'false' if stage == 'broken' else 'true'}; }}",
    ]
    if stage != "broken":
        fields.append(agent.implementation)
    if stage in {"data", "view", "style", "accessibility", "manifest", "ready"}:
        fields.append(f"fixture: '{agent.slug}-fixture'")
    if stage in {"view", "style", "accessibility", "manifest", "ready"}:
        fields.append(f"view: '<section data-domain=\"{agent.slug}\"></section>'")
    if stage in {"style", "accessibility", "manifest", "ready"}:
        fields.append("focusVisible: true")
    if stage in {"accessibility", "manifest", "ready"}:
        fields.extend(["keyboard: true", "reducedMotion: true"])
    if stage in {"manifest", "ready"}:
        fields.extend([f"owner: '{agent.id}'", "schemaVersion: 1"])
    if stage == "ready":
        fields.append("status: 'ready'")
    return f"  {agent.id}: {{ " + ", ".join(fields) + " },"


def _line_edit(agent: Agent, old_stage: str, new_stage: str) -> str:
    return canonical_json([{"old_string": _registry_line(agent, old_stage), "new_string": _registry_line(agent, new_stage)}])


def _inline_node(script: str) -> str:
    return "node --input-type=module --eval " + shlex.quote(script)


def _cycle_title(agent: Agent, cycle: str) -> str:
    titles = {
        "repair": "target settles after repair",
        "data": "fixture is deterministic",
        "view": "view has a semantic section",
        "style": "style keeps focus visible",
        "a11y": "honors reduced motion",
        "manifest": "manifest names its owner",
        "quality": "implementation is marked ready",
    }
    return f"{agent.id} {titles[cycle]}"


def _cycle_command(agent: Agent, cycle: str) -> str:
    markers = {
        "repair": "verify() { return true; }",
        "data": f"fixture: '{agent.slug}-fixture'",
        "view": f'data-domain="{agent.slug}"',
        "style": "focusVisible: true",
        "a11y": "reducedMotion: true",
        "manifest": f"owner: '{agent.id}'",
        "quality": "status: 'ready'",
    }
    title = _cycle_title(agent, cycle)
    reason = f"{agent.id} implementation should report verified" if cycle == "repair" else f"{agent.id} shared registry is missing {cycle} evidence"
    script = (
        "import { readFile } from 'node:fs/promises'; "
        "const source = await readFile('src/registry.js', 'utf8'); "
        f"const line = source.split('\\n').find(function (value) {{ return value.trimStart().startsWith('{agent.id}:'); }}); "
        f"if (!line || !line.includes({json.dumps(markers[cycle])})) {{ console.error({json.dumps(title)}); console.error({json.dumps(reason)}); process.exit(1); }} "
        f"console.log({json.dumps(title)});"
    )
    return _inline_node(script)


def _workspace_contract_command(agent: Agent) -> str:
    script = (
        "import { readFile } from 'node:fs/promises'; "
        "const [index, app, registry, tests] = await Promise.all(['index.html', 'src/app.js', 'src/registry.js', 'tests/storefront.test.mjs'].map(function (path) { return readFile(path, 'utf8'); })); "
        f"const line = registry.split('\\n').find(function (value) {{ return value.trimStart().startsWith('{agent.id}:'); }}); "
        f"if (!index.includes('./src/registry.js') || !app.includes('FlashCart.features') || !line || !line.includes('schemaVersion: 1') || !tests.includes('{agent.id} {agent.role} contribution is ready')) process.exit(1); "
        f"console.log('{agent.id} shared application contract verified');"
    )
    return _inline_node(script)


def _final_regression_command() -> str:
    script = (
        "import { readFile, readdir } from 'node:fs/promises'; "
        "const root = (await readdir('.')).sort(); const src = (await readdir('src')).sort(); const testsDir = (await readdir('tests')).sort(); "
        "const [index, app, config, registry, tests] = await Promise.all(['index.html', 'src/app.js', 'src/config.js', 'src/registry.js', 'tests/storefront.test.mjs'].map(function (path) { return readFile(path, 'utf8'); })); "
        "const ready = (registry.match(/status: 'ready'/g) || []).length; "
        "const contributions = (tests.match(/contribution is ready/g) || []).length; "
        "if (root.join(',') !== 'index.html,src,tests' || src.join(',') !== 'app.js,config.js,registry.js' || testsDir.join(',') !== 'storefront.test.mjs' || ready !== 10 || contributions !== 10 || !config.includes(\"checkoutRetry: 'complete'\") || !tests.includes(\"checkoutRetry: 'complete'\") || !index.includes('./src/app.js') || !index.includes(':focus-visible') || !app.includes('product-grid')) process.exit(1); "
        "globalThis.window = globalThis; globalThis.eval(config); globalThis.eval(registry); "
        "const quote = globalThis.FlashCart.features.A06.quoteCart(1000); "
        "if (quote.shippingCents !== 650 || quote.taxCents !== 75 || quote.totalCents !== 1725) process.exit(1); "
        "console.log('Shared storefront final regression passed');"
    )
    return _inline_node(script)


def _shared_policy_edit_command(replacements: tuple[tuple[str, str], ...]) -> str:
    serialized = json.dumps(replacements, ensure_ascii=False)
    script = (
        "import { readFile, writeFile } from 'node:fs/promises'; "
        "const paths = ['src/config.js', 'tests/storefront.test.mjs']; "
        "const values = await Promise.all(paths.map(function (path) { return readFile(path, 'utf8'); })); "
        f"const replacements = {serialized}; "
        "const updated = values.map(function (source) { for (const [before, after] of replacements) { if (!source.includes(before)) throw new Error('missing policy marker: ' + before); source = source.replace(before, after); } return source; }); "
        "await Promise.all(paths.map(function (path, index) { return writeFile(path, updated[index]); }));"
    )
    return _inline_node(script)


def preview_server_command(port: int = 4173) -> str:
    if not 1 <= port <= 65535:
        raise ValueError("preview port must be between 1 and 65535")
    script = (
        "import { createReadStream, statSync } from 'node:fs'; import { createServer } from 'node:http'; import { extname, resolve, sep } from 'node:path'; "
        "const root = resolve(process.cwd()); const types = { '.css': 'text/css; charset=utf-8', '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8' }; "
        "const server = createServer(function (request, response) { const pathname = decodeURIComponent(new URL(request.url, 'http://flashcart.local').pathname); const file = resolve(root, pathname === '/' ? 'index.html' : '.' + pathname); if (!file.startsWith(root + sep) && file !== root) return response.writeHead(403).end('forbidden'); try { if (!statSync(file).isFile()) throw new Error('not file'); response.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); createReadStream(file).pipe(response); } catch { response.writeHead(404).end('not found'); } }); "
        f"process.stdin.setEncoding('utf8'); process.stdin.on('data', function (value) {{ if (value.trim() === 'stop') server.close(function () {{ process.exit(0); }}); }}); server.listen({port}, '0.0.0.0', function () {{ console.log('FLASHCART_READY 0.0.0.0:{port}'); }});"
    )
    return _inline_node(script)


def payloads_for(agent: Agent) -> dict[str, str]:
    payloads = {
        "01-broken.js": _line_edit(agent, "pending", "broken"),
        "03-fix.json": _line_edit(agent, "broken", "core"),
        "04-data": _line_edit(agent, "core", "data"),
        "06-view": _line_edit(agent, "data", "view"),
        "08-style": _line_edit(agent, "view", "style"),
        "10-a11y": _line_edit(agent, "style", "accessibility"),
        "12-manifest.json": _line_edit(agent, "accessibility", "manifest"),
        "14-shared-test.json": canonical_json([{
            "old_string": _shared_test_agent_line(agent, ready=False),
            "new_string": _shared_test_agent_line(agent, ready=True),
        }]),
        "15-registry-edit.json": _line_edit(agent, "manifest", "ready"),
    }
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
    """One explicit 44-call recipe over one shared, line-owned registry."""
    attempt = f"{agent.id}.primary"
    workspace = f"{attempt}.workspace"
    anchor = f"{attempt}.anchor"
    registry = "src/registry.js"
    rows: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> None:
        kwargs.setdefault("workspace_ref", workspace)
        rows.append(_record(agent, len(rows) + 1, attempt_ref=attempt, **kwargs))

    def edit(name: str, phase: str, purpose: str, *, path: str = registry) -> None:
        add(
            scene="fanout",
            phase=phase,
            category="patch",
            purpose=purpose,
            op="file_edit",
            args=_payload_args(agent, name, path, edit=True),
            expect={"kind": "file_edit"},
            after=[rows[-1]["id"]],
            effects=[path],
        )

    def read(path: str, phase: str, purpose: str) -> None:
        add(
            scene="fanout",
            phase=phase,
            category="inspect",
            purpose=purpose,
            op="file_read",
            args={"path": path},
            expect={"kind": "file_read"},
            after=[rows[-1]["id"]] if rows else ["all-primary-workspaces-ready"],
        )

    def run(command: str, phase: str, category: str, purpose: str, *, cycle: str | None = None, expect: dict[str, Any] | None = None) -> None:
        add(
            scene="fanout",
            phase=phase,
            category=category,
            purpose=purpose,
            op="exec_command",
            args={"command": command, "timeout_ms": 60000, "yield_time_ms": 30000},
            expect=expect or {"kind": "command_ok"},
            after=[rows[-1]["id"]],
            test_cycle=cycle,
        )

    add(
        scene="fanout",
        phase="anchor",
        category="workspace_control",
        purpose="Hold the primary workspace open behind the publication gate",
        op="exec_command",
        args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0},
        expect={"kind": "command_running"},
        after=["bootstrap-published"],
        bind={"workspace_session_id": workspace, "command_session_id": anchor},
    )
    read("index.html", "context", "Inspect the shared storefront entry point")
    read(registry, "context", "Inspect this agent's unclaimed line in the shared feature registry")
    read("src/config.js", "context", "Inspect the shared commerce boundary")
    read("src/app.js", "context", "Inspect the browser shell that consumes the shared registry")
    edit("01-broken.js", "red", "Introduce a deliberately incomplete implementation on this agent's registry line")
    cycle = f"{agent.id}.repair"
    run(
        _cycle_command(agent, "repair"),
        "red",
        "test_debug",
        "Prove the registry contract fails before repair",
        cycle=cycle,
        expect={
            "kind": "expected_red",
            "child_exit_code": 1,
            "failing_subtests": [{"id": _cycle_title(agent, "repair"), "reason_contains": f"{agent.id} implementation should report verified"}],
            "forbid_output_contains": ["SyntaxError", "ERR_MODULE_NOT_FOUND", "Could not find"],
        },
    )
    read(registry, "diagnose", "Inspect the failed line before applying the smallest repair")
    edit("03-fix.json", "repair", "Repair this agent's implementation in the shared registry")
    run("node --check src/registry.js", "repair", "build_lint", "Syntax-check the repaired shared registry")
    run(_cycle_command(agent, "repair"), "green", "test_debug", "Prove the original red contract turns green", cycle=cycle)
    edit("04-data", "data", "Add deterministic fixture metadata to this agent's registry line")
    run("node --check src/registry.js", "data", "build_lint", "Syntax-check the registry after the data edit")
    read(registry, "data", "Inspect the deterministic fixture metadata")
    run(_cycle_command(agent, "data"), "data", "test_debug", "Verify the deterministic fixture contract", cycle=f"{agent.id}.data")
    edit("06-view", "view", "Add this domain's semantic view metadata to the shared registry")
    run("node --check src/registry.js", "view", "build_lint", "Syntax-check the registry after the view edit")
    read(registry, "view", "Inspect the semantic view metadata")
    run(_cycle_command(agent, "view"), "view", "test_debug", "Verify the semantic view contract", cycle=f"{agent.id}.view")
    edit("08-style", "style", "Add visible-focus metadata to this agent's shared line")
    read("index.html", "style", "Inspect the entry point's unified presentation and focus layer")
    run(_cycle_command(agent, "style"), "style", "test_debug", "Verify this contribution preserves visible focus", cycle=f"{agent.id}.style")
    edit("10-a11y", "accessibility", "Add keyboard and reduced-motion metadata to the shared registry")
    run("node --check src/registry.js", "accessibility", "build_lint", "Syntax-check the accessibility contribution")
    read(registry, "accessibility", "Inspect keyboard and reduced-motion metadata")
    run(_cycle_command(agent, "a11y"), "accessibility", "test_debug", "Verify the reduced-motion contract", cycle=f"{agent.id}.a11y")
    edit("12-manifest.json", "manifest", "Add durable ownership metadata to this agent's registry line")
    read(registry, "manifest", "Inspect the durable ownership metadata")
    run(_cycle_command(agent, "manifest"), "manifest", "test_debug", "Verify the line names its durable owner", cycle=f"{agent.id}.manifest")
    read("index.html", "review", "Review the compact shared storefront entry point")
    read("src/config.js", "review", "Review shared commerce configuration before publication")
    read("src/app.js", "review", "Review the browser shell that composes all contributions")
    read("index.html", "review", "Review the inline responsive style layer")
    read(registry, "review", "Review this agent's full contribution in shared context")
    read("src/config.js", "review", "Reconfirm the conflict-sensitive configuration head")
    run("node --check src/registry.js && node --check src/config.js && node --check src/app.js", "review", "build_lint", "Syntax-check every shared JavaScript surface")
    read(registry, "review", "Inspect ownership evidence before the integration checks")
    edit("14-shared-test.json", "review", "Add this agent's verification to the one shared test module", path="tests/storefront.test.mjs")
    run(_workspace_contract_command(agent), "review", "test_debug", "Verify the shared application and collaborative test contract")
    read(registry, "review", "Inspect the line immediately before its ready transition")
    run("node --check src/registry.js", "review", "build_lint", "Recheck the collaborative registry before finalization")
    edit("15-registry-edit.json", "merge", "Mark this agent's line ready for the coordinated publication")
    run(_cycle_command(agent, "quality"), "merge", "test_debug", "Prove this contribution is publication-ready", cycle=f"{agent.id}.quality")
    add(
        scene="merge",
        phase="release",
        category="workspace_control",
        purpose="Release the gated workspace only after every primary feature gate is green",
        op="write_command_stdin",
        args={"input": "publish\n", "yield_time_ms": 30000},
        expect={"kind": "publish_success"},
        after=["all-primary-feature-gates-green"],
        command_ref=anchor,
        workspace_ref=None,
    )
    if len(rows) != 44:
        raise AssertionError(f"{agent.id} recipe changed its reviewed call count: {len(rows)}")
    return rows


def all_primary_plans() -> dict[str, list[dict[str, Any]]]:
    return {agent.id: primary_plan(agent) for agent in AGENTS}


def _append(rows: list[dict[str, Any]], agent: Agent, *, attempt: str, workspace: str | None, scene: str, phase: str, category: str, purpose: str, op: str, args: dict[str, Any], expect: dict[str, Any], after: list[str], effects: list[str] | None = None, bind: dict[str, str] | None = None, test_cycle: str | None = None, final_regression: bool = False, command_ref: str | None = None) -> None:
    rows.append(_record(agent, len(rows) + 1, attempt_ref=attempt, workspace_ref=workspace, scene=scene, phase=phase, category=category, purpose=purpose, op=op, args=args, expect=expect, after=after, effects=effects, bind=bind, test_cycle=test_cycle, final_regression=final_regression, command_ref=command_ref))


def _port_server(marker: str) -> str:
    script = (
        "import { createServer } from 'node:http'; "
        "const server = createServer(function (_, response) { response.end('ok'); }); "
        "server.once('error', function (error) { if (error.code === 'EADDRINUSE') { console.log('PORT_COLLISION'); process.exit(0); } throw error; }); "
        "process.stdin.setEncoding('utf8'); process.stdin.on('data', function (value) { if (value.trim() === 'stop') server.close(function () { process.exit(0); }); }); "
        f"server.listen(4173, '0.0.0.0', function () {{ console.log('PORT_BOUND {marker}'); }});"
    )
    return _inline_node(script)


def extended_plan(agent: Agent) -> list[dict[str, Any]]:
    """Append the required conflict, network, and post-merge proof scenes."""
    rows = primary_plan(agent)
    if agent.id == "A06":
        attempt = "A06.conflict"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="anchor", category="conflict_network_audit", purpose="Hold A06's fresh-head conflict contender until the atomic publish gate", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["all-primary-published"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="inspect", category="inspect", purpose="Read the seeded shipping threshold on the common conflict head", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="winner-edit", category="patch", purpose="Set matching conflict-winning policy values in the application and its shared test", op="exec_command", args={"command": _shared_policy_edit_command((("freeShippingCents: 5000", "freeShippingCents: 6000"), ("standardShippingCents: 700", "standardShippingCents: 650"), ("taxRate: 0.08", "taxRate: 0.075"))), "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], effects=["src/config.js", "tests/storefront.test.mjs"])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="winner-check", category="build_lint", purpose="Syntax-check both conflict-winning shared JavaScript surfaces", op="exec_command", args={"command": "node --check src/config.js && node --check tests/storefront.test.mjs", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="winner-review", category="inspect", purpose="Confirm the shared test sees the winning policy before release", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="winner-publish", category="conflict_network_audit", purpose="Publish A06's conflict winner before A08 attempts its stale source", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_success"}, after=["conflict-contenders-mutated"], command_ref=anchor)
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="winner-shared-read", category="inspect", purpose="Read the shared test policy after the winning publication", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="winner-blame", category="conflict_network_audit", purpose="Prove all contested shared-test policy lines belong to the A06 conflict winner", op="file_blame", args={"path": "tests/storefront.test.mjs", "line_start": shared_test_line("freeShippingCents:"), "line_end": shared_test_line("taxRate:")}, expect={"kind": "blame_owner", "owner_agent": "A06", "owner_attempt": "A06.conflict"}, after=[rows[-1]["id"]])
    if agent.id == "A08":
        attempt = "A08.conflict"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="anchor", category="conflict_network_audit", purpose="Hold A08's stale-head conflict contender until the rejection gate", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["all-primary-published"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="inspect", category="inspect", purpose="Read the original shipping boundary from A08's stale source", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="stale-exec-edit", category="patch", purpose="Create divergent policy values in both the application and its shared test", op="exec_command", args={"command": _shared_policy_edit_command((("freeShippingCents: 5000", "freeShippingCents: 7500"), ("standardShippingCents: 700", "standardShippingCents: 900"), ("taxRate: 0.08", "taxRate: 0.095"))), "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], effects=["src/config.js", "tests/storefront.test.mjs"])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="stale-review", category="inspect", purpose="Confirm A08's stale shared test contains its divergent policy", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="rejected-publish", category="conflict_network_audit", purpose="Require atomic source-conflict rejection for A08's stale publication", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_reject", "publish_reject_class": "source_conflict"}, after=["A06.050"], command_ref=anchor)
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="shared-threshold", category="inspect", purpose="Prove the rejected attempt did not advance the shared threshold", op="file_read", args={"path": "src/config.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="shared-blame", category="conflict_network_audit", purpose="Prove rejected A08 work did not replace A06 on the shared-test policy", op="file_blame", args={"path": "tests/storefront.test.mjs", "line_start": shared_test_line("freeShippingCents:"), "line_end": shared_test_line("taxRate:")}, expect={"kind": "blame_owner", "owner_agent": "A06", "owner_attempt": "A06.conflict"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="atomic-shared-read", category="inspect", purpose="Re-read the shared test after atomic rejection", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        attempt = "A08.retry"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-anchor", category="conflict_network_audit", purpose="Open a new A08 workspace from the post-winner head", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["A08.052"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-read", category="inspect", purpose="Confirm the retry starts from A06's published shared-test policy", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-patch", category="patch", purpose="Record the durable retry in the application and its shared test", op="exec_command", args={"command": _shared_policy_edit_command((("checkoutRetry: 'pending'", "checkoutRetry: 'complete'"),)), "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], effects=["src/config.js", "tests/storefront.test.mjs"])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="conflict", phase="retry-review", category="inspect", purpose="Read the shared-test retry state before its clean publication", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="retry-publish", category="conflict_network_audit", purpose="Publish the fresh-head A08 retry successfully", op="write_command_stdin", args={"input": "publish\n", "yield_time_ms": 30000}, expect={"kind": "publish_success"}, after=[rows[-1]["id"]], command_ref=anchor)
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="retry-shared-read", category="inspect", purpose="Verify the successful retry is present in the shared test", op="file_read", args={"path": "tests/storefront.test.mjs"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=None, scene="conflict", phase="retry-blame", category="conflict_network_audit", purpose="Prove the shared-test retry line has raw A08 ownership", op="file_blame", args={"path": "tests/storefront.test.mjs", "line_start": shared_test_line("checkoutRetry:"), "line_end": shared_test_line("checkoutRetry:")}, expect={"kind": "blame_owner", "owner_agent": "A08", "owner_attempt": "A08.retry"}, after=[rows[-1]["id"]])
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
            _append(rows, agent, attempt=attempt, workspace=workspace, scene="network", phase="port-bind", category="conflict_network_audit", purpose=f"Bind or observe the port-4173 {marker} trusted-session experiment", op="exec_command", args={"command": _port_server(marker), "timeout_ms": 60000, "yield_time_ms": 0 if kind == "command_running" else 30000}, expect=expect, after=after, bind=bind)
        for attempt, start_id in (("A09.network.shared1", "A09.045"), ("A09.network.isolated1", "A09.047"), ("A09.network.isolated2", "A09.048")):
            _append(rows, agent, attempt=attempt, workspace=None, scene="network", phase="port-ready", category="conflict_network_audit", purpose="Read the running server readiness marker without publishing experiment files", op="read_command_lines", args={"max_lines": 20, "wait_ms": 5000}, expect={"kind": "command_running", "output_contains": ["PORT_BOUND"]}, after=[start_id], command_ref=f"{attempt}.server")
        for attempt in ("A09.network.shared1", "A09.network.isolated1", "A09.network.isolated2"):
            _append(rows, agent, attempt=attempt, workspace=None, scene="network", phase="port-stop", category="conflict_network_audit", purpose="Stop the trusted-session port server before its explicit session is destroyed", op="write_command_stdin", args={"input": "stop\n", "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], command_ref=f"{attempt}.server")
    if agent.id == "A10":
        attempt = "A10.final"
        workspace = f"{attempt}.workspace"
        anchor = f"{attempt}.anchor"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-anchor", category="workspace_control", purpose="Open a fresh post-merge A10 regression workspace", op="exec_command", args={"command": ANCHOR, "timeout_ms": 180000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=["network-experiment-clean", "A08.057"], bind={"workspace_session_id": workspace, "command_session_id": anchor})
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-context", category="inspect", purpose="Read the fully merged feature registry from the fresh final workspace", op="file_read", args={"path": "src/registry.js"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-syntax", category="build_lint", purpose="Syntax-check the final storefront and collaborative test module", op="exec_command", args={"command": "node --check src/app.js && node --check src/config.js && node --check src/registry.js && node --check tests/storefront.test.mjs", "timeout_ms": 60000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]])
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-regression", category="test_debug", purpose="Run the frozen exact-inventory final regression from a fresh post-merge workspace", op="exec_command", args={"command": _final_regression_command(), "timeout_ms": 120000, "yield_time_ms": 30000}, expect={"kind": "command_ok"}, after=[rows[-1]["id"]], test_cycle="A10.final-regression", final_regression=True)
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="final-tree-read", category="inspect", purpose="Read the retained storefront shell after final regression", op="file_read", args={"path": "index.html"}, expect={"kind": "file_read"}, after=[rows[-1]["id"]])
        preview = f"{attempt}.preview"
        _append(rows, agent, attempt=attempt, workspace=workspace, scene="evidence", phase="preview-start", category="test_debug", purpose="Start the final offline storefront for a retained trusted-preview capture", op="exec_command", args={"command": preview_server_command(), "timeout_ms": 120000, "yield_time_ms": 0}, expect={"kind": "command_running"}, after=[rows[-1]["id"]], bind={"command_session_id": preview})
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
        for suffix in ("repair", "data", "view", "style", "a11y", "manifest", "quality"):
            tests[f"{agent.id}.{suffix}"] = {
                "command": _cycle_command(agent, suffix),
                "subtests": [_cycle_title(agent, suffix)],
                "subtest_count": 1,
                "allowed": {"skip": [], "todo": [], "cancelled": []},
            }
    tests["A10.final-regression"] = {
        "command": _final_regression_command(),
        "subtests": ["Shared storefront final regression passed"],
        "subtest_count": 1,
        "allowed": {"skip": [], "todo": [], "cancelled": []},
    }
    return {"schema_version": 1, "tests": tests}


def materialized_tree() -> dict[str, str]:
    """The independently oracle-reviewed final tree; no run data participates."""
    tree = dict(bootstrap_files())
    for agent in AGENTS:
        tree["src/registry.js"] = tree["src/registry.js"].replace(_registry_line(agent, "pending"), _registry_line(agent, "ready"))
        tree["tests/storefront.test.mjs"] = tree["tests/storefront.test.mjs"].replace(
            _shared_test_agent_line(agent, ready=False),
            _shared_test_agent_line(agent, ready=True),
        )
    tree["src/config.js"] = (
        tree["src/config.js"].replace("freeShippingCents: 5000", "freeShippingCents: 6000")
        .replace("standardShippingCents: 700", "standardShippingCents: 650")
        .replace("taxRate: 0.08", "taxRate: 0.075")
        .replace("checkoutRetry: 'pending'", "checkoutRetry: 'complete'")
    )
    tree["tests/storefront.test.mjs"] = (
        tree["tests/storefront.test.mjs"].replace("freeShippingCents: 5000", "freeShippingCents: 6000")
        .replace("standardShippingCents: 700", "standardShippingCents: 650")
        .replace("taxRate: 0.08", "taxRate: 0.075")
        .replace("checkoutRetry: 'pending'", "checkoutRetry: 'complete'")
    )
    return dict(sorted(tree.items()))


def assert_relative(path: str) -> None:
    item = PurePosixPath(path)
    if item.is_absolute() or ".." in item.parts or not path or "\\" in path:
        raise ValueError(f"unsafe relative path: {path!r}")
