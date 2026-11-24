/* Build Guide (wiki) page.
   Static content — the only dynamic bit is a scroll-spy that highlights the
   current section in the table of contents. The header account and the
   admin-only links come from dashboard-common.js like on every page. */

import { openAppPanel, appPanelClose } from '/static/dashboard-common.js';

// Keep the imports referenced (they are used via inline onclick + below).
void openAppPanel; void appPanelClose;

/* ── Table-of-contents scroll-spy ───────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const links = Array.from(document.querySelectorAll('.wk-toc a'));
  if (!links.length) return;

  const byId = new Map();
  const sections = [];
  for (const a of links) {
    const id = (a.getAttribute('href') || '').replace(/^#/, '');
    const sec = id && document.getElementById(id);
    if (sec) { byId.set(id, a); sections.push(sec); }
  }
  if (!sections.length) return;

  let current = null;
  const setActive = (id) => {
    if (id === current) return;
    current = id;
    for (const a of links) { a.classList.remove('active'); a.removeAttribute('aria-current'); }
    const a = byId.get(id);
    if (a) { a.classList.add('active'); a.setAttribute('aria-current', 'location'); }
  };

  const observer = new IntersectionObserver((entries) => {
    // Pick the topmost section currently intersecting near the top of the view.
    const visible = entries
      .filter(e => e.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (visible.length) setActive(visible[0].target.id);
  }, { rootMargin: '-10% 0px -70% 0px', threshold: 0 });

  for (const sec of sections) observer.observe(sec);
  setActive(sections[0].id);

  // Smooth-scroll for TOC clicks (instant when the reader asks for less motion).
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  for (const a of links) {
    a.addEventListener('click', (e) => {
      const id = (a.getAttribute('href') || '').replace(/^#/, '');
      const sec = document.getElementById(id);
      if (!sec) return;
      e.preventDefault();
      sec.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
      history.replaceState(null, '', '#' + id);
    });
  }
});
