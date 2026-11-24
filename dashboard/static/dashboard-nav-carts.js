/* dashboard-nav-carts.js — the header's cart buttons (basket, compilation).
 *
 * Each cart's button sits in the header slot #nav-carts and opens its panel
 * in one shared shape: a dropdown under the header on desktop, a bottom sheet
 * over a scrim on phones (CSS: .nav-cart-panel in dashboard-basket.css).
 * This module only coordinates them: one panel open at a time, a click
 * outside or on the scrim closes it, and a Basket | Compilation switch in each
 * panel's header (.nav-cart-switch) appears when the page has both carts.
 */

const carts = new Map();   // name -> { label, button, panel, count(), isOpen(), open(), close() }
let wired = false;

const shown = (c) => c.button?.isConnected && c.button.style.display !== 'none';

function scrim() {
  let el = document.getElementById('nav-cart-scrim');
  if (!el) {
    el = document.createElement('div');
    el.id = 'nav-cart-scrim';
    el.className = 'nav-cart-scrim';
    el.addEventListener('click', closeAll);
    document.body.appendChild(el);
  }
  return el;
}

function closeAll() {
  for (const c of carts.values()) if (c.isOpen()) c.close();
}

function paintSwitches(current) {
  // the basket first: every signed-in user has it, the compilation cart is admin-only
  const avail = [...carts.entries()].filter(([, c]) => shown(c))
    .sort(([a], [b]) => (a === 'basket' ? -1 : b === 'basket' ? 1 : 0));
  for (const sw of document.querySelectorAll('.nav-cart-switch')) {
    sw.hidden = avail.length < 2;
    if (sw.hidden) continue;
    sw.replaceChildren(...avail.map(([name, c]) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'nav-cart-switch-btn' + (name === current ? ' on' : '');
      b.setAttribute('aria-pressed', String(name === current));
      b.textContent = c.label + ' ';
      const n = document.createElement('b');
      n.textContent = String(c.count());
      b.appendChild(n);
      b.addEventListener('click', () => {
        if (name === current) return;
        carts.get(current)?.close();
        c.open();
      });
      return b;
    }));
  }
}

/** Register a cart. `cart`: { label, button, panel, count, isOpen, open, close }. */
export function registerNavCart(name, cart) {
  carts.set(name, cart);
  if (wired) return;
  wired = true;
  // A click outside the open panel and its button closes it (the scrim
  // covers the page on phones, so this is the desktop dropdown's path).
  document.addEventListener('pointerdown', (e) => {
    for (const c of carts.values()) {
      if (c.isOpen() && !c.panel.contains(e.target) && !c.button.contains(e.target)
          && !e.target.closest?.('.tp-root')) c.close();
    }
  }, true);
}

/** A cart's panel just opened: close the others, show the scrim, paint the switch. */
export function navCartOpened(name) {
  for (const [n, c] of carts) if (n !== name && c.isOpen()) c.close();
  scrim();
  document.documentElement.classList.add('nav-cart-open');
  paintSwitches(name);
}

/** A cart's panel just closed. */
export function navCartClosed() {
  if (![...carts.values()].some(c => c.isOpen())) document.documentElement.classList.remove('nav-cart-open');
}
