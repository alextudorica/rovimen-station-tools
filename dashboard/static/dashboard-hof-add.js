/*
 * dashboard-hof-add.js
 * ────────────────────
 * Shared hall-of-fame curation helper. Any clip surface that opens the
 * VideoModal (Highlights, Detections, Archive, overview GMN clips, Video DB)
 * gets an admin-only "Add to Hall of Fame" toggle through this module, so a
 * clip can be featured from anywhere on the site — not just the Highlights
 * page. The landing gallery (dashboard-hof.js) shows a rolling window of the
 * most-recently-added entries.
 *
 * Single source of truth for: the admin check, the entries cache, and the
 * add/remove/patch calls — so the modal button, the Highlights manager and
 * anything else stay consistent and never issue divergent requests.
 */

import { fetchAuthStatus } from './dashboard-common.js';

/** True when the current session is an admin, from the page's one session
 *  request (shared with the header and the page). */
export function hofIsAdmin() {
    return fetchAuthStatus().then(a => !!(a && a.admin));
}

let _entries = null;
let _entriesAt = 0;
const _ENTRIES_TTL_MS = 15000;

/** Current hall-of-fame entries, newest-first. Cached with a short TTL;
 *  pass force=true after a mutation to refresh immediately. */
export async function hofGetEntries(force = false) {
    if (force || !_entries || (Date.now() - _entriesAt) > _ENTRIES_TTL_MS) {
        try {
            const resp = await fetch('/api/hall_of_fame');
            if (resp.ok) {
                _entries = (await resp.json()).entries || [];
                _entriesAt = Date.now();
            }
        } catch (_) {
            _entries = _entries || [];
        }
    }
    return _entries || [];
}

export function hofKey(c) {
    // Match the server's canonicalization (routes/hall_of_fame.py): camera
    // upper-cased, date normalised to YYYY-MM-DD. Without this, compact-date
    // surfaces like the Video DB (which sends YYYYMMDD) never match a stored
    // entry, so an already-featured clip shows "☆ Add" and re-adding it hits
    // the server's duplicate check instead of toggling the feature off.
    const cam  = String(c.camera || '').toUpperCase();
    const d    = String(c.date || '');
    const date = /^\d{8}$/.test(d) ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : d;
    return `${cam}|${date}|${c.filename}`;
}

/** The stored entry featuring this exact clip, or undefined. */
export async function hofFind(clip, force = false) {
    const k = hofKey(clip);
    return (await hofGetEntries(force)).find(e => hofKey(e) === k);
}

function _payload(clip) {
    return {
        camera:             clip.camera,
        date:               clip.date,
        filename:           clip.filename,
        stack:              clip.stack || null,
        shower:             clip.shower || null,
        peak_magnitude:     clip.peak_magnitude,
        duration_s:         clip.duration_s,
        time_utc:           clip.time_utc || null,
        station_label:      clip.station_label || null,
        detection_offset_s: clip.detection_offset_s,
    };
}

async function _throwOnBad(resp) {
    if (resp.ok) return;
    const err = await resp.json().catch(() => ({}));
    throw Object.assign(new Error(err.error || err.description || `HTTP ${resp.status}`),
                        { status: resp.status });
}

/** Add the clip. Throws (with .status) on a refusal; 404 = not in the archive yet. */
export async function hofAdd(clip) {
    await _throwOnBad(await fetch('/api/hall_of_fame', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_payload(clip)),
    }));
    await hofGetEntries(true);
}

/** How long a just-locked clip gets to reach the archive (station re-encode + upload). */
export const hofArchiveWait = { everyMs: 15000, tries: 40 };

async function _addWhenArchived(clip) {
    for (let i = 0; i < hofArchiveWait.tries; i++) {
        await new Promise(r => setTimeout(r, hofArchiveWait.everyMs));
        try {
            await hofAdd(clip);
            return;
        } catch (e) {
            if (e.status !== 404) throw e;
        }
    }
    const mins = Math.round(hofArchiveWait.tries * hofArchiveWait.everyMs / 60000);
    throw new Error(`the clip is locked but still not in the archive after ${mins} min. ` +
                    'The station uploads it at dawn at the latest; add it again then.');
}

/** Add the clip if absent, remove it if present. Returns {added: bool}. */
export async function hofToggle(clip) {
    const existing = await hofFind(clip);
    if (existing) {
        await _throwOnBad(await fetch(`/api/hall_of_fame/${existing.id}`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        }));
        await hofGetEntries(true);
        return { added: false };
    }
    await hofAdd(clip);
    return { added: true };
}

export async function hofPatch(id, fields) {
    await _throwOnBad(await fetch(`/api/hall_of_fame/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields),
    }));
    await hofGetEntries(true);
}

export async function hofDelete(id) {
    await _throwOnBad(await fetch(`/api/hall_of_fame/${id}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
    }));
    await hofGetEntries(true);
}

function _paint(btn, inHof) {
    // ★ = featured, ☆ = not. Kept as textContent so there's no HTML injection.
    btn.textContent = inHof ? '★ In Hall of Fame' : '☆ Add to Hall of Fame';
    btn.classList.toggle('vm-hof-active', inHof);
}

/**
 * Wire a button element to toggle the given clip's hall-of-fame membership.
 * Hides the button entirely for non-admins or clips missing an identity
 * (camera/date/filename) — e.g. multi-station event views. Safe to call on
 * every modal open; it re-resolves state each time.
 *
 * Only archived clips can be featured. When the clip is not in the archive yet
 * and the caller passes clip.lockForUpload (an async fn locking it on its
 * station, which re-encodes and uploads it), the button locks the clip and
 * adds it once the upload lands.
 */
export async function wireHofButton(btn, clip) {
    if (!btn) return;
    if (!clip || !clip.camera || !clip.date || !clip.filename || !(await hofIsAdmin())) {
        btn.style.display = 'none';
        return;
    }
    // The modal reuses this button for the next clip while an add may still be
    // waiting on an upload; only the current wiring may repaint it.
    const wiring = btn._hofWiring = {};
    const mine = () => btn._hofWiring === wiring;
    btn.disabled = false;
    btn.style.display = '';
    _paint(btn, !!(await hofFind(clip)));
    btn.onclick = async () => {
        if (btn.disabled) return;
        btn.disabled = true;
        try {
            let added;
            try {
                ({ added } = await hofToggle(clip));
            } catch (e) {
                if (e.status !== 404 || !clip.lockForUpload) throw e;
                btn.textContent = '⏳ Locking…';
                await clip.lockForUpload();
                btn.textContent = '⏳ Uploading… adds when archived';
                await _addWhenArchived(clip);
                added = true;
            }
            if (mine()) _paint(btn, added);
        } catch (e) {
            if (mine()) _paint(btn, !!(await hofFind(clip, true)));
            alert('Hall of Fame update failed: ' + e.message);
        } finally {
            if (mine()) btn.disabled = false;
        }
    };
}
