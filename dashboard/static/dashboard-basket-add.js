/*
 * dashboard-basket-add.js
 * ───────────────────────
 * Shared download-basket helper. Any clip surface that opens the VideoModal
 * (Highlights, Detections, Archive, overview GMN clips, Video DB) gets an
 * "Add to basket" toggle for signed-in users through this module, so a clip
 * can be basketed from anywhere on the site. Importing the module (the
 * VideoModal does) also mounts the floating basket widget — badge, item
 * panel, zip checkout — on every page that shows clips.
 *
 * Single source of truth for: the signed-in check, the items cache, and the
 * add/remove/clear/checkout calls — same pattern as dashboard-hof-add.js.
 */

import { registerNavCart, navCartOpened, navCartClosed } from './dashboard-nav-carts.js';
import { fetchAuthStatus } from './dashboard-common.js';

/** The /api/auth/status payload ({user, role, admin, ...}) or null: the
 *  page's one session request, shared with the header and the page. */
export function basketAuth() {
    return fetchAuthStatus();
}

/** Username of the current session, or null. */
export async function basketUser() {
    const a = await basketAuth();
    return (a && a.user) || null;
}

let _items = null;
let _itemsAt = 0;
const _ITEMS_TTL_MS = 15000;

/** Current basket entries. Cached with a short TTL; pass force=true after a
 *  mutation to refresh immediately. */
export async function basketGetItems(force = false) {
    if (force || !_items || (Date.now() - _itemsAt) > _ITEMS_TTL_MS) {
        try {
            const resp = await fetch('/api/basket');
            if (resp.ok) {
                _items = (await resp.json()).items || [];
                _itemsAt = Date.now();
            }
        } catch (_) {
            _items = _items || [];
        }
    }
    return _items || [];
}

export function basketKey(c) {
    // Match the server's canonicalization (routes/basket.py): camera
    // upper-cased, date normalised to YYYY-MM-DD — compact-date surfaces
    // like the Video DB send YYYYMMDD and would otherwise never match.
    const cam  = String(c.camera || '').toUpperCase();
    const d    = String(c.date || '');
    const date = /^\d{8}$/.test(d) ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : d;
    return `${cam}|${date}|${c.filename}`;
}

/** The stored entry holding this exact clip, or undefined. */
export async function basketFind(clip, force = false) {
    const k = basketKey(clip);
    return (await basketGetItems(force)).find(e => basketKey(e) === k);
}

async function _throwOnBad(resp) {
    if (resp.ok) return;
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || err.description || `HTTP ${resp.status}`);
}

/** The clip's selected sub-range {start, end} (seconds, 0.1s precision),
 *  or null when there is no live trim state or the handles still span the
 *  whole clip (within a small epsilon of handle jitter) — full-range adds
 *  must POST the exact payload they always have. */
function _trimRange(clip) {
    const ts = typeof clip.getTrim === 'function' ? clip.getTrim() : null;
    if (!ts || !(ts.duration > 0)) return null;
    const EPS = 0.05;
    const start = Math.max(0, ts.start || 0);
    const end   = Math.min(ts.duration, ts.end ?? ts.duration);
    if (start <= EPS && end >= ts.duration - EPS) return null;
    const r = { start: Math.round(start * 10) / 10, end: Math.round(end * 10) / 10 };
    return r.end > r.start ? r : null;
}

/** Add the clip if absent, remove it if present. Returns {added: bool}. */
export async function basketToggle(clip) {
    const existing = await basketFind(clip);
    if (existing) {
        // Content-Type + empty JSON body so the CSRF origin guard accepts
        // the request (same shape as dashboard-hof-add.js deletes).
        await _throwOnBad(await fetch(`/api/basket/${existing.id}`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        }));
    } else {
        // A proper sub-range picked with the modal's trim handles rides
        // along so checkout can deliver just those seconds; a full-range
        // add carries no trim fields at all.
        const trim = _trimRange(clip);
        await _throwOnBad(await fetch('/api/basket', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                camera:        clip.camera,
                date:          clip.date,
                filename:      clip.filename,
                stack:         clip.stack || null,
                station_label: clip.station_label || null,
                ...(trim ? { trim_start: trim.start, trim_end: trim.end } : {}),
            }),
        }));
    }
    await basketGetItems(true);
    _syncWidget();
    return { added: !existing };
}

export async function basketRemove(entryId) {
    await _throwOnBad(await fetch(`/api/basket/${entryId}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
    }));
    await basketGetItems(true);
    _syncWidget();
}

export async function basketClear() {
    await _throwOnBad(await fetch('/api/basket/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
    }));
    await basketGetItems(true);
    _syncWidget();
}

function _paint(btn, inBasket) {
    // Kept as textContent so there's no HTML injection.
    btn.textContent = inBasket ? '✓ In basket' : '+ Add to basket';
    btn.classList.toggle('vm-basket-active', inBasket);
}

/**
 * Wire a button element to toggle the given clip's basket membership.
 * Hides the button entirely for anonymous sessions or clips missing an
 * identity (camera/date/filename) — e.g. multi-station event views. Safe to
 * call on every modal open; it re-resolves state each time.
 */
export async function wireBasketButton(btn, clip) {
    if (!btn) return;
    if (!clip || !clip.camera || !clip.date || !clip.filename || !(await basketUser())) {
        btn.style.display = 'none';
        return;
    }
    btn.style.display = '';
    _paint(btn, !!(await basketFind(clip)));
    btn.onclick = async () => {
        if (btn.disabled) return;
        btn.disabled = true;
        try {
            const { added } = await basketToggle(clip);
            _paint(btn, added);
        } catch (e) {
            alert('Basket update failed: ' + e.message);
        } finally {
            btn.disabled = false;
        }
    };
}

/* ── Basket widget ───────────────────────────────────────────────────────
 * A header icon button (count badge) in the shared #nav-carts slot that
 * opens a panel listing the basket; checkout hands /api/basket/checkout to
 * the browser's downloader. Mounted once per page, only for signed-in
 * sessions. The panel opens as a dropdown under the header, a bottom sheet
 * on phones (dashboard-nav-carts.js coordinates it with the compilation cart).
 */

const BASKET_ICON = '<svg class="nav-cart-ico" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9h16l-1.6 9.2a2 2 0 0 1-2 1.8H7.6a2 2 0 0 1-2-1.8z"/><path d="M8.5 9 11 4M15.5 9 13 4"/><path d="M12 12.5v4M12 16.5l-2-2M12 16.5l2-2"/></svg>';

/** The header's cart slot; a page without the shared header gets the old
 *  bottom-right floating stack instead. */
function _cartSlot() {
    const slot = document.getElementById('nav-carts');
    if (slot) return slot;
    let stack = document.getElementById('fab-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'fab-stack';
        stack.className = 'fab-stack';
        document.body.appendChild(stack);
    }
    return stack;
}

function _fmtItemLabel(e) {
    // RO0001_20260701_220001_color.mkv -> "22:00:01" time part if parseable.
    const m = String(e.filename || '').match(/_(\d{6})(?:_\d{6})?_[a-z]+\.\w+$/i);
    const t = m ? `${m[1].slice(0, 2)}:${m[1].slice(2, 4)}:${m[1].slice(4, 6)}` : '';
    // Trimmed entries show their selected window, e.g. "12.3s–15.4s".
    const trim = (e.trim_start != null && e.trim_end != null)
        ? `${Number(e.trim_start).toFixed(1)}s–${Number(e.trim_end).toFixed(1)}s`
        : '';
    return `${e.camera} · ${e.date}${t ? ' · ' + t : ''}${trim ? ' · ' + trim : ''}`;
}

function _renderPanel() {
    const list = document.getElementById('basket-panel-list');
    const dlBtn = document.getElementById('basket-dl-btn');
    const clearBtn = document.getElementById('basket-clear-btn');
    if (!list) return;
    const items = _items || [];
    list.textContent = '';
    if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'basket-empty';
        empty.textContent = 'Basket is empty. Open any clip and press “+ Add to basket”.';
        list.appendChild(empty);
    }
    for (const e of items) {
        const row = document.createElement('div');
        row.className = 'basket-item';
        const info = document.createElement('div');
        info.className = 'basket-item-info';
        const title = document.createElement('div');
        title.className = 'basket-item-title';
        title.textContent = _fmtItemLabel(e);
        const sub = document.createElement('div');
        sub.className = 'basket-item-sub';
        sub.textContent = e.filename;
        info.append(title, sub);
        const rm = document.createElement('button');
        rm.className = 'basket-item-rm';
        rm.textContent = '×';
        rm.type = 'button';
        rm.title = 'Remove from basket';
        rm.setAttribute('aria-label', 'Remove ' + _fmtItemLabel(e) + ' from basket');
        rm.onclick = async () => {
            rm.disabled = true;
            try { await basketRemove(e.id); }
            catch (err) { alert('Remove failed: ' + err.message); rm.disabled = false; }
        };
        row.append(info, rm);
        list.appendChild(row);
    }
    if (dlBtn) {
        dlBtn.disabled = !items.length;
        dlBtn.textContent = items.length
            ? `↓ Download zip (${items.length} clip${items.length === 1 ? '' : 's'} + stacks)`
            : '↓ Download zip';
    }
    if (clearBtn) clearBtn.disabled = !items.length;
}

function _syncWidget() {
    const badge = document.getElementById('basket-fab-badge');
    if (!badge) return;
    const n = (_items || []).length;
    badge.textContent = String(n);
    badge.style.display = n ? '' : 'none';
    _renderPanel();
}

function _togglePanel(open) {
    const panel = document.getElementById('basket-panel');
    if (!panel) return;
    const show = open ?? panel.style.display === 'none';
    panel.style.display = show ? '' : 'none';
    const fab = document.getElementById('basket-fab');
    if (fab) fab.setAttribute('aria-expanded', show ? 'true' : 'false');
    if (show) {
        _renderPanel();
        navCartOpened('basket');
        panel.querySelector('.basket-panel-close')?.focus();
    } else {
        if (fab && panel.contains(document.activeElement)) fab.focus();
        navCartClosed();
    }
}

async function _mountWidget() {
    if (document.getElementById('basket-fab')) return;
    const auth = await basketAuth();
    if (!auth || !auth.user) return;   // anonymous: no basket surface
    const isAdmin = auth.admin === true || auth.role === 'admin';

    const fab = document.createElement('button');
    fab.id = 'basket-fab';
    fab.type = 'button';
    fab.className = 'basket-fab hc nav-cart-btn';
    fab.title = 'Download basket';
    fab.setAttribute('aria-label', 'Download basket');
    fab.setAttribute('aria-controls', 'basket-panel');
    fab.setAttribute('aria-expanded', 'false');
    fab.innerHTML = BASKET_ICON + '<span id="basket-fab-badge" class="basket-fab-badge nav-cart-badge" style="display:none">0</span>';
    fab.onclick = () => _togglePanel();

    const panel = document.createElement('div');
    panel.id = 'basket-panel';
    panel.className = 'basket-panel nav-cart-panel';
    panel.style.display = 'none';
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-labelledby', 'basket-panel-title');
    panel.innerHTML = `
      <div class="nav-cart-grab" aria-hidden="true"></div>
      <div class="nav-cart-switch" hidden></div>
      <div class="basket-panel-header">
        <span id="basket-panel-title">Download basket</span>
        <button type="button" class="basket-panel-close" title="Close" aria-label="Close basket">&#10005;</button>
      </div>
      <div id="basket-panel-list" class="basket-panel-list" aria-live="polite"></div>
      <label id="basket-fireball-row" class="basket-fireball-row" style="display:none">
        <input type="checkbox" id="basket-fireball-chk">
        <span>Include fireball analysis packs — platepars, FF/FR frames,
        CALSTARS per clip, pulled live from the stations (slower)</span>
      </label>
      <div class="basket-panel-footer">
        <button type="button" id="basket-dl-btn" class="basket-dl-btn" disabled>↓ Download zip</button>
        <button type="button" id="basket-clear-btn" class="basket-clear-btn" disabled>Clear</button>
      </div>`;
    if (isAdmin) {
        panel.querySelector('#basket-fireball-row').style.display = '';
    }
    panel.querySelector('.basket-panel-close').onclick = () => _togglePanel(false);
    panel.querySelector('#basket-dl-btn').onclick = () => {
        // Plain navigation: the browser downloader streams the zip and the
        // session cookie authenticates the GET.
        const chk = panel.querySelector('#basket-fireball-chk');
        window.location.href = '/api/basket/checkout'
            + (chk && chk.checked ? '?fireball=1' : '');
    };
    panel.querySelector('#basket-clear-btn').onclick = async () => {
        try { await basketClear(); }
        catch (err) { alert('Clear failed: ' + err.message); }
    };

    // Header slot: the panel goes to <body> (fixed dropdown / sheet); the old
    // floating stack keeps it first so it opens above the buttons.
    panel.addEventListener('keydown', e => {
        if (e.key === 'Escape') { e.stopPropagation(); _togglePanel(false); }
    });
    const slot = _cartSlot();
    if (slot.id === 'nav-carts') document.body.appendChild(panel);
    else slot.prepend(panel);
    slot.append(fab);
    registerNavCart('basket', {
        label: 'Basket', button: fab, panel,
        count: () => (_items || []).length,
        isOpen: () => panel.style.display !== 'none',
        open: () => _togglePanel(true),
        close: () => _togglePanel(false),
    });
    await basketGetItems();
    _syncWidget();
}

function _initBasketUi() {
    _mountWidget().catch(() => { /* widget is best-effort chrome */ });
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _initBasketUi);
    } else {
        _initBasketUi();
    }
}
