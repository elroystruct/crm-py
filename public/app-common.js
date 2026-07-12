/* Zocalo Capital CRM — shared frontend helpers */

const ZC = (() => {

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    let body = null;
    try { body = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const msg = (body && body.error) || `Request failed (${res.status})`;
      const err = new Error(msg);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  async function requireAuth() {
    const data = await api('/api/auth/me');
    if (!data.user) {
      window.location.href = '/login';
      return null;
    }
    applyUserChrome(data.user);
    return data.user;
  }

  function applyUserChrome(user) {
    document.querySelectorAll('[data-user-name]').forEach(el => {
      el.textContent = user.name || user.email;
    });
    document.querySelectorAll('[data-user-first]').forEach(el => {
      el.textContent = (user.name || user.email || '').split(' ')[0];
    });
    document.querySelectorAll('[data-user-avatar]').forEach(el => {
      if (user.avatar) el.src = user.avatar;
    });
  }

  function wireLogout(selector = '#logoutBtn') {
    const el = document.querySelector(selector);
    if (!el) return;
    el.addEventListener('click', async (e) => {
      e.preventDefault();
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (err) { /* ignore */ }
      window.location.href = '/login';
    });
  }

  // ---------------- SignalWire connect modal ----------------
  function ensureModalMarkup() {
    if (document.getElementById('zcConnectModal')) return;
    const wrap = document.createElement('div');
    wrap.id = 'zcConnectModal';
    wrap.innerHTML = `
      <style>
        #zcConnectModal{ position:fixed; inset:0; z-index:999; display:none; align-items:center; justify-content:center; }
        #zcConnectModal .zc-backdrop{ position:absolute; inset:0; background:rgba(15,13,30,0.45); backdrop-filter: blur(2px); }
        #zcConnectModal .zc-card{
          position:relative; width:100%; max-width:420px; background:var(--panel);
          border-radius:20px; padding:26px; box-shadow: var(--shadow-panel);
          border:1px solid var(--border-soft);
        }
        #zcConnectModal h3{ font-size:16px; margin-bottom:4px; }
        #zcConnectModal p.hint{ font-size:12.5px; color:var(--meta); margin-bottom:18px; }
        #zcConnectModal .zc-field{ margin-bottom:12px; }
        #zcConnectModal .zc-field label{ display:block; font-size:11.5px; font-weight:600; color:var(--ink-soft); margin-bottom:6px; }
        #zcConnectModal .zc-field input{
          width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--border);
          background:var(--panel-off); font-size:13px; outline:none; color: var(--ink);
        }
        #zcConnectModal .zc-field input:focus{ box-shadow:0 0 0 3px rgba(124,111,232,0.16); }
        #zcConnectModal .zc-actions{ display:flex; gap:10px; margin-top:18px; }
        #zcConnectModal .zc-error{ color:#D34747; font-size:12px; margin-top:10px; display:none; }
      </style>
      <div class="zc-backdrop" data-close></div>
      <div class="zc-card">
        <h3>Connect SignalWire</h3>
        <p class="hint">Add your Space, Project ID, and Auth Token to pull live messages and analytics.</p>
        <div class="zc-field"><label>Space</label><input id="zcSpace" placeholder="yourspace.signalwire.com"></div>
        <div class="zc-field"><label>Project ID</label><input id="zcProjectId" placeholder="00000000-0000-0000-0000-000000000000"></div>
        <div class="zc-field"><label>Auth Token</label><input id="zcAuthToken" type="password" placeholder="PT..."></div>
        <div class="zc-field"><label>From number (optional)</label><input id="zcFromNumber" placeholder="+1..."></div>
        <div class="zc-error" id="zcConnectError"></div>
        <div class="zc-actions">
          <button class="btn btn-ghost" style="flex:1;" data-close>Cancel</button>
          <button class="btn btn-primary" style="flex:1;" id="zcConnectSubmit">Connect</button>
        </div>
      </div>
    `;
    document.body.appendChild(wrap);
    wrap.querySelectorAll('[data-close]').forEach(el => el.addEventListener('click', closeConnectModal));
  }

  function openConnectModal(onConnected) {
    ensureModalMarkup();
    const modal = document.getElementById('zcConnectModal');
    modal.style.display = 'flex';
    const submit = document.getElementById('zcConnectSubmit');
    const errEl = document.getElementById('zcConnectError');
    submit.onclick = async () => {
      errEl.style.display = 'none';
      const space = document.getElementById('zcSpace').value.trim();
      const projectId = document.getElementById('zcProjectId').value.trim();
      const authToken = document.getElementById('zcAuthToken').value.trim();
      const fromNumber = document.getElementById('zcFromNumber').value.trim();
      if (!space || !projectId || !authToken) {
        errEl.textContent = 'Space, Project ID, and Auth Token are all required.';
        errEl.style.display = 'block';
        return;
      }
      try {
        await api('/api/connect', { method: 'POST', body: JSON.stringify({ space, projectId, authToken, fromNumber }) });
        closeConnectModal();
        if (onConnected) onConnected();
      } catch (e) {
        errEl.textContent = e.message;
        errEl.style.display = 'block';
      }
    };
  }

  function closeConnectModal() {
    const modal = document.getElementById('zcConnectModal');
    if (modal) modal.style.display = 'none';
  }

  async function ensureConnected() {
    const status = await api('/api/status');
    return status;
  }

  // ---------------- misc formatting ----------------
  function timeAgo(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const diffMs = Date.now() - d.getTime();
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h`;
    const days = Math.floor(hrs / 24);
    return `${days}d`;
  }

  function money(n) {
    if (n === null || n === undefined) return '—';
    return '$' + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function initials(name) {
    if (!name) return '?';
    return name.split(' ').map(p => p[0]).slice(0, 2).join('').toUpperCase();
  }

  return { api, requireAuth, applyUserChrome, wireLogout, openConnectModal, closeConnectModal, ensureConnected, timeAgo, money, initials };
})();
