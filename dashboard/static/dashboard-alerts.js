// Fireball alerts page (templates/alerts.html): subscribe or unsubscribe the
// signed-in user's email address (POST/DELETE /api/alerts/email).
const form = document.getElementById('al-email-form');
if (form) {
  const input = document.getElementById('al-email');
  const status = document.getElementById('al-status');
  const sub = document.getElementById('al-subscribe');
  const unsub = document.getElementById('al-unsubscribe');
  const show = (text, ok) => {
    status.textContent = text;
    status.className = 'al-status ' + (ok ? 'ok' : 'err');
  };
  const call = async (method, body) => {
    const r = await fetch('/api/alerts/email', {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  };
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    sub.disabled = true;
    try {
      const d = await call('POST', { email: input.value.trim() });
      show(d.confirmation_sent
        ? `Subscribed as ${d.alert_email}. A confirmation email is on its way.`
        : `Subscribed as ${d.alert_email}. The confirmation email could not be sent (${d.confirmation_error}); check the address.`,
        d.confirmation_sent);
      sub.textContent = 'Update';
      unsub.hidden = false;
    } catch (err) {
      show(`Not saved: ${err.message}`, false);
    } finally {
      sub.disabled = false;
    }
  });
  unsub.addEventListener('click', async () => {
    unsub.disabled = true;
    try {
      await call('DELETE');
      show('Unsubscribed: no more fireball emails.', true);
      input.value = '';
      sub.textContent = 'Subscribe';
      unsub.hidden = true;
    } catch (err) {
      show(`Not saved: ${err.message}`, false);
    } finally {
      unsub.disabled = false;
    }
  });
}
