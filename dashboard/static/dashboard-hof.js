import { escHtml, showerFullName, state, resolveUserRole, viewQuery, _toast, CLIP_PRE_S, CLIP_POST_S } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
import { hofGetEntries, hofPatch, hofDelete } from './dashboard-hof-add.js';

// Report links come from the server (/fireball/<event_id>), but only an
// http(s) or same-site URL may reach an href: anything else (javascript:,
// data:) is dropped rather than rendered.
function _safeReportUrl(url) {
    if (!url) return null;
    try {
        const u = new URL(String(url), window.location.href);
        return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : null;
    } catch { return null; }
}

/*
 * dashboard-hof.js
 * ────────────────
 * Hall of Fame cards: the admin-curated featured meteors served by
 * /api/hall_of_fame, played in a VideoModal. Two surfaces use it:
 *
 *  - the landing page (#ov-hof): a rolling window of the newest entries and
 *    a "See all" link to the Highlights page;
 *  - the Highlights page (#hall-of-fame): every entry, grouped by period,
 *    with inline caption / period / remove controls for admins.
 *
 * A section stays hidden when the list is empty or the fetch fails, so the
 * page degrades to exactly what it was before the feature existed. Featuring
 * a clip happens from any clip on the site (dashboard-hof-add.js).
 */

const HOF_WINDOW = 5;        // the landing shows the newest five
const HOF_PAGE_FOLD = 8;     // Highlights shows this many before "Show all"

let _hofEntries = [];
let _hofModal = null;

function _fmtMag(v) {
    if (v == null) return null;
    const n = parseFloat(v);
    return 'mag ' + (n > 0 ? '+' : '') + n.toFixed(1);
}

function _fmtDate(iso) {
    if (!iso) return null;
    const d = new Date(iso + 'T00:00:00Z');
    return d.toLocaleDateString('en-GB', {
        day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
    });
}

function _entryTitle(e) {
    if (e.caption) return e.caption;
    if (e.report_title) return e.report_title;
    return [
        e.shower ? showerFullName(e.shower) : null,
        _fmtMag(e.peak_magnitude),
        e.station_label,
    ].filter(Boolean).join(' · ') || 'Featured meteor';
}

// "2026-07" (the default period, the clip's month) reads as "July 2026";
// a hand-written label like "Week 28" is shown as typed.
function _fmtPeriod(p) {
    const m = /^(\d{4})-(\d{2})$/.exec(p || '');
    if (!m) return p || '';
    return new Date(Date.UTC(+m[1], +m[2] - 1, 1))
        .toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' });
}

function _openModal(idx) {
    const e = _hofEntries[idx];
    if (!e || !_hofModal) return;
    const detOffset = e.detection_offset_s ?? null;
    _hofModal.open({
        src:       e.clip_mp4_url || e.clip_url,
        title:     _entryTitle(e),
        detOffset,
        trimStart: detOffset != null ? Math.max(0, detOffset - CLIP_PRE_S) : 0,
        trimEnd:   detOffset != null ? detOffset + CLIP_POST_S : undefined,
        station:   e.host_key || '',
        camera:    e.camera,
        // The whole archived clip as MP4 (clip_mp4_url; clip_url is the
        // original MKV).
        download:  { originalUrl: e.clip_mp4_url || e.clip_url || null },
        date:      e.date,
        filename:  e.filename,
        stack:     e.stack_url ? { url: e.stack_url } : null,
        detection: {
            mag_apparent: e.peak_magnitude,
            shower:       e.shower || 'SPO',
            duration_s:   e.duration_s,
            time_utc:     e.time_utc,
            station:      e.station_label,
            camera:       e.camera,
        },
        nav: {
            hasPrev: idx > 0,
            hasNext: idx < _hofEntries.length - 1,
            onPrev:  () => _openModal(idx - 1),
            onNext:  () => _openModal(idx + 1),
        },
    });
}

function _cardHtml(e, i, { admin }) {
    const meta = [
        _fmtMag(e.peak_magnitude),
        e.shower ? showerFullName(e.shower) : null,
        e.station_label,
        _fmtDate(e.date),
    ].filter(Boolean);
    // Signed-in visitors get the station name as a link to its page on
    // that night and the camera as a link to its Video DB.
    const linked = !!(state.AUTH_USER && e.host_key);
    const night = String(e.date || '').replace(/-/g, '') || null;
    const metaHtml = meta.map(m => {
        if (linked && m === e.station_label) {
            const st = `/station/${encodeURIComponent(e.host_key)}/rms` + viewQuery({ date: night });
            const cam = `/station/${encodeURIComponent(e.host_key)}/videodb` + viewQuery({ cam: e.camera, date: night });
            return `<span><a class="ov-hof-link" href="${escHtml(st)}">${escHtml(m)}</a>` +
                   ` <a class="ov-hof-link" href="${escHtml(cam)}">${escHtml(e.camera)}</a></span>`;
        }
        return `<span>${escHtml(m)}</span>`;
    }).join('');
    const title = _entryTitle(e);
    // The caption is the keyboard-reachable control: a link to the report
    // when there is one, else a button that plays the clip.
    const reportUrl = _safeReportUrl(e.report_url);
    const open = reportUrl
        ? `<a class="ov-hof-open" href="${escHtml(reportUrl)}">${escHtml(title)}</a>`
        : `<button type="button" class="ov-hof-open" aria-label="${escHtml('Play: ' + title)}">${escHtml(title)}</button>`;
    const edit = admin ? `
        <div class="ov-hof-edit">
            <input class="ov-hof-edit-caption" data-field="caption" maxlength="300" autocomplete="off"
                   placeholder="Caption…" aria-label="Caption" value="${escHtml(e.caption || '')}">
            <input class="ov-hof-edit-period" data-field="period" maxlength="40" autocomplete="off"
                   placeholder="2026-07" aria-label="Period label, e.g. 2026-07 or Week 28"
                   title="Period label, e.g. 2026-07 or Week 28" value="${escHtml(e.period || '')}">
            <button type="button" class="ov-hof-edit-del" data-act="del">Remove</button>
        </div>` : '';
    return `
    <div class="ov-hof-card" data-idx="${i}" data-id="${escHtml(e.id)}">
        <div class="ov-hof-img-wrap">
            ${e.stack_url
                ? `<img class="ov-hof-img" src="${escHtml(e.stack_url)}" alt="" width="480" height="270" loading="lazy" onerror="this.style.display='none'">`
                : '<div class="ov-hof-img-placeholder"></div>'
            }
            <div class="ov-hof-play" aria-hidden="true"></div>
            ${e.period ? `<div class="ov-hof-badge">${escHtml(e.period)}</div>` : ''}
            ${e.report_url ? '<div class="ov-hof-report-badge">Report</div>' : ''}
        </div>
        <div class="ov-hof-body">
            <div class="ov-hof-caption">${open}</div>
            <div class="ov-hof-meta">${metaHtml}</div>
            ${edit}
        </div>
    </div>`;
}

function _wireCards(root, { admin, reload }) {
    root.querySelectorAll('.ov-hof-card').forEach(card => {
        card.addEventListener('click', (ev) => {
            // Station / camera links, the report link and the admin
            // controls act on their own.
            if (ev.target.closest('a.ov-hof-link, a.ov-hof-open, .ov-hof-edit')) return;
            const e = _hofEntries[parseInt(card.dataset.idx, 10)];
            // A fireball with a write-up opens the report, which embeds its clips.
            const reportUrl = e && _safeReportUrl(e.report_url);
            if (reportUrl) {
                window.location.href = reportUrl;
                return;
            }
            _openModal(parseInt(card.dataset.idx, 10));
        });
        if (!admin) return;
        const id = card.dataset.id;
        card.querySelectorAll('.ov-hof-edit input[data-field]').forEach(input => {
            input.addEventListener('change', async () => {
                try {
                    await hofPatch(id, { [input.dataset.field]: input.value });
                    await reload();
                } catch (err) {
                    _toast('Update failed: ' + err.message, 'error');
                }
            });
        });
        card.querySelector('.ov-hof-edit-del')?.addEventListener('click', async () => {
            try {
                await hofDelete(id);
                await reload();
            } catch (err) {
                _toast('Remove failed: ' + err.message, 'error');
            }
        });
    });
}

// ── Landing page: the newest few and a link to all of them ──────────────

function _renderLanding(total) {
    const section = document.getElementById('ov-hof');
    const grid    = document.getElementById('ov-hof-grid');
    if (!section || !grid || !_hofEntries.length) return;

    const periodEl = document.getElementById('ov-hof-period');
    if (periodEl && _hofEntries[0].period) {
        periodEl.textContent = _fmtPeriod(_hofEntries[0].period);
    }
    const all = document.getElementById('ov-hof-all');
    if (all) {
        all.textContent = `See all ${total} →`;
        all.hidden = total <= _hofEntries.length;
    }

    grid.innerHTML = _hofEntries.map((e, i) => _cardHtml(e, i, { admin: false })).join('');
    _wireCards(grid, { admin: false });
    section.style.display = '';
}

// ── Highlights page: every entry, grouped by period ─────────────────────

/**
 * Render the full Hall of Fame into the Highlights section. Entries keep the
 * API's newest-featured-first order inside a period; periods are listed
 * newest first. Past HOF_PAGE_FOLD cards the rest wait behind "Show all",
 * unless the visitor arrived on #hall-of-fame (the landing's "See all").
 */
export async function mountHofGallery({ admin = false } = {}) {
    const section = document.getElementById('hall-of-fame');
    const body    = document.getElementById('hof-gallery');
    if (!section || !body) return;
    _hofModal ??= new VideoModal(document.getElementById('hof-modal-container'), { trim: true, nav: true });

    const reload = async () => {
        const entries = await hofGetEntries(true);
        // Stable sort by period, newest first; ties keep the API order.
        _hofEntries = entries.map((e, i) => ({ e, i }))
            .sort((a, b) => String(b.e.period || '').localeCompare(String(a.e.period || '')) || a.i - b.i)
            .map(x => x.e);
        draw();
    };

    let expanded = location.hash === '#hall-of-fame';
    const draw = () => {
        if (!_hofEntries.length && !admin) { section.hidden = true; return; }
        section.hidden = false;
        const count = document.getElementById('hof-count');
        if (count) count.textContent = _hofEntries.length ? `${_hofEntries.length} featured` : '';
        if (!_hofEntries.length) {
            body.innerHTML = '<p class="ov-hof-empty">No featured meteors yet. Open any clip and use “Add to Hall of Fame” in the player.</p>';
            return;
        }
        const shown = expanded ? _hofEntries.length : Math.min(HOF_PAGE_FOLD, _hofEntries.length);
        let html = '', period = null, open = false;
        for (let i = 0; i < shown; i++) {
            const e = _hofEntries[i];
            if (e.period !== period) {
                if (open) html += '</div>';
                period = e.period;
                html += `<h3 class="ov-hof-period-h">${escHtml(_fmtPeriod(period) || 'Featured')}</h3><div class="ov-hof-grid">`;
                open = true;
            }
            html += _cardHtml(e, i, { admin });
        }
        if (open) html += '</div>';
        if (shown < _hofEntries.length) {
            html += `<button type="button" class="ov-hof-more" id="hof-more">Show all ${_hofEntries.length}</button>`;
        }
        body.innerHTML = html;
        _wireCards(body, { admin, reload });
        document.getElementById('hof-more')?.addEventListener('click', () => {
            expanded = true;
            draw();
            // Keyboard users continue from the first card the button revealed.
            body.querySelectorAll('.ov-hof-card')[shown]?.querySelector('.ov-hof-open')?.focus();
        });
    };

    window.addEventListener('hashchange', () => {
        if (location.hash === '#hall-of-fame' && !expanded) { expanded = true; draw(); }
    });
    await reload();
    if (location.hash === '#hall-of-fame') section.scrollIntoView();
}

(async function initLandingHof() {
    // The landing section only exists on the overview page; on Highlights
    // the page calls mountHofGallery() itself.
    if (!document.getElementById('ov-hof')) return;
    _hofModal = new VideoModal(document.getElementById('hof-modal-container'), { trim: true, nav: true });
    try {
        const resp = await fetch('/api/hall_of_fame');
        if (!resp.ok) return;
        const data = await resp.json();
        // API returns newest-first; keep only the rolling window for the landing.
        const all = data.entries || [];
        _hofEntries = all.slice(0, HOF_WINDOW);
        // Links and the download need to know whether this is a signed-in visitor.
        await resolveUserRole().catch(() => {});
        _renderLanding(all.length);
    } catch (e) {
        // Leave the section hidden — the landing page must never break
        // because the curated list is unavailable.
    }
})();
