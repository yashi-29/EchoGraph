/* ============================================================
   APP.JS — shared behaviour across all Digital Twin pages
   ============================================================ */

const DT = (() => {

  /* ---------- backend API ---------- */
  const API_BASE = window.DT_API_BASE || 'http://localhost:8000/api/v1';

  async function apiFetch(path, options){
    const res = await fetch(API_BASE + path, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      ...options
    });
    let body = null;
    try{ body = await res.json(); }catch(e){ /* no/invalid JSON body */ }
    return { ok: res.ok, status: res.status, body };
  }

  /* ---------- auth forms (login / signup) ---------- */
  function initAuthForm(formId, redirectTo){
    const form = document.getElementById(formId);
    if(!form) return;
    const isSignup = formId.toLowerCase().startsWith('signup');
    const prefix = isSignup ? 'signup' : 'login';
    const endpoint = isSignup ? '/auth/signup' : '/auth/login';

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const alertBox = form.querySelector('.form-alert');
      const btn = form.querySelector('button[type="submit"]');
      const requiredFields = [...form.querySelectorAll('[required]')];
      const allFilled = requiredFields.every(f => f.value.trim().length > 0);

      const showError = (message) => {
        if(alertBox){
          alertBox.textContent = message;
          alertBox.classList.remove('success');
          alertBox.classList.add('show','error');
        }
      };

      if(!allFilled){
        showError('Fill in every field before continuing.');
        return;
      }

      const email = document.getElementById(`${prefix}Email`).value.trim();
      const password = document.getElementById(`${prefix}Password`).value;

      if(btn){ btn.textContent = 'Please wait…'; btn.disabled = true; }

      const nameInput = document.getElementById(`${prefix}Name`);
      const payload = { email, password };
      if(nameInput && nameInput.value.trim()) payload.full_name = nameInput.value.trim();
      const { ok, body } = await apiFetch(endpoint, {
        method: 'POST',
        body: JSON.stringify(payload)
      });

      if(!ok){
        showError((body && body.detail) || 'Something went wrong. Please try again.');
        if(btn){ btn.textContent = isSignup ? 'Create my twin' : 'Log in'; btn.disabled = false; }
        return;
      }

      if(alertBox){
        alertBox.textContent = 'Success — redirecting to your twin…';
        alertBox.classList.remove('error');
        alertBox.classList.add('show','success');
      }
      setTimeout(() => { window.location.href = redirectTo; }, 700);
    });
  }

  /* ---------- auth guard + logout (app-shell / onboarding pages) ---------- */
  async function initAuthGuard(redirectTo){
    const { ok, body } = await apiFetch('/auth/me', { method: 'GET' });
    if(!ok){
      window.location.href = redirectTo || 'login.html';
      return;
    }
    fillSidebarAccount(body);
  }

  // Every app-shell page has the same sidebar footer — fill it with the real account. Without a
  // full name we show the email's username and nudge towards Settings rather than guess a name.
  function fillSidebarAccount(user){
    const fullName = (user.full_name || '').trim();
    document.querySelectorAll('.rail-foot .who').forEach(who => {
      const name = who.querySelector('.name');
      const plan = who.querySelector('.plan');
      if(name){
        name.textContent = fullName || user.email.split('@')[0];
        name.title = fullName || user.email;
      }
      if(plan){
        if(fullName){
          plan.textContent = user.email;
          plan.title = user.email;
        }else{
          plan.innerHTML = '<a href="settings.html#profile" class="add-name-link">Add your name</a>';
        }
      }
    });
  }

  function initLogout(selector, redirectTo){
    document.querySelectorAll(selector || '[data-logout]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        await apiFetch('/auth/logout', { method: 'POST' });
        window.location.href = redirectTo || 'login.html';
      });
    });
  }

  /* ---------- connectors page ---------- */
  function initConnectors(){
    const grid = document.querySelector('[data-connector-grid]');
    if(!grid) return;

    const providerCards = [...grid.querySelectorAll('.connector-card[data-provider]')];

    function setCardConnected(card, connected){
      const btn = card.querySelector('.connector-toggle-btn');
      const statusEl = card.querySelector('.connector-status');
      btn.classList.toggle('is-connected', connected);
      btn.textContent = connected ? 'Disconnect' : 'Connect';
      statusEl.classList.toggle('connected', connected);
      statusEl.innerHTML = connected
        ? '<span class="dot"></span> Connected'
        : '<span class="dot"></span> Not connected';
    }

    async function refreshConnectedState(){
      const { ok, body } = await apiFetch('/connectors/', { method: 'GET' });
      if(!ok || !Array.isArray(body)) return;
      const connectedProviders = new Set(body.map(c => c.provider));
      providerCards.forEach(card => setCardConnected(card, connectedProviders.has(card.dataset.provider)));
      updateConnectorCount();
    }

    grid.addEventListener('click', async (e) => {
      const btn = e.target.closest('.connector-toggle-btn');
      if(!btn || btn.disabled) return;
      const card = btn.closest('.connector-card');
      const provider = card.dataset.provider;
      if(!provider) return; // "coming soon" cards have no backend wiring yet

      if(btn.classList.contains('is-connected')){
        btn.disabled = true;
        await apiFetch(`/connectors/${provider}`, { method: 'DELETE' });
        btn.disabled = false;
        await refreshConnectedState();
      } else {
        // Full page nav (not fetch) — this has to be a real browser redirect
        // so the provider's own consent screen can render and redirect back.
        window.location.href = `${API_BASE}/connectors/${provider}/authorize`;
      }
    });

    // category filter chips
    const chips = document.querySelectorAll('.connector-category-tabs .filter-chip');
    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        chips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        const cat = chip.dataset.filter;
        grid.querySelectorAll('.connector-card').forEach(card => {
          card.style.display = (cat === 'all' || card.dataset.category === cat) ? '' : 'none';
        });
      });
    });

    // the OAuth callback redirects back here with ?connector=<provider>&status=connected|error
    const params = new URLSearchParams(window.location.search);
    const returnedProvider = params.get('connector');
    const returnedStatus = params.get('status');
    const alertBox = document.querySelector('[data-connector-alert]');
    if(returnedProvider && alertBox){
      const connected = returnedStatus === 'connected';
      const name = { gmail: 'Gmail', google_calendar: 'Google Calendar' }[returnedProvider] || returnedProvider;
      alertBox.textContent = connected
        ? `${name} connected successfully.`
        : returnedStatus === 'missing_permissions'
          ? `${name} wasn't connected: Google asks you to tick each permission box on its consent screen. Connect again and tick all of them (or "Select all").`
          : `Couldn't connect ${name}. Please try again.`;
      alertBox.classList.add('show', connected ? 'success' : 'error');
      window.history.replaceState(null, '', window.location.pathname);
    }

    refreshConnectedState();
  }

  function updateConnectorCount(){
    const countEl = document.querySelector('[data-connected-count]');
    if(!countEl) return;
    const connected = document.querySelectorAll('.connector-toggle-btn.is-connected').length;
    countEl.textContent = connected;
  }

  /* ---------- building / onboarding sequence ---------- */
  function initBuildingSequence(nextPage){
    const ring = document.querySelector('[data-progress-bar]');
    const pctLabel = document.querySelector('[data-progress-pct]');
    const logItems = [...document.querySelectorAll('.build-log .log-item')];
    if(!ring) return;

    const circumference = 2 * Math.PI * 118; // matches r=118 in svg
    ring.style.strokeDasharray = `${circumference}`;
    ring.style.strokeDashoffset = `${circumference}`;

    let step = 0;
    const totalSteps = logItems.length;

    function advance(){
      if(step > 0) logItems[step-1].classList.replace('active','done');
      if(step >= totalSteps){
        setTimeout(() => { window.location.href = nextPage; }, 600);
        return;
      }
      logItems[step].classList.add('active');
      const pct = Math.round(((step+1) / totalSteps) * 100);
      const offset = circumference - (pct/100) * circumference;
      ring.style.strokeDashoffset = `${offset}`;
      if(pctLabel) pctLabel.textContent = `${pct}%`;
      step++;
      setTimeout(advance, 1100);
    }
    setTimeout(advance, 500);
  }

  /* ---------- chat ---------- */
  // Conversations live server-side (/chat/conversations); the URL's ?c=<id> is the open
  // thread, so reload, back/forward and shared tabs all land on the same conversation.
  function initChat(){
    const scroll = document.querySelector('[data-chat-scroll]');
    const form = document.querySelector('[data-composer-form]');
    const textarea = document.querySelector('[data-composer-input]');
    const list = document.querySelector('[data-convo-list]');
    if(!scroll || !form || !textarea || !list) return;

    const rail = document.querySelector('[data-convo-rail]');
    const titleEl = document.querySelector('[data-chat-title]');
    const statusEl = document.querySelector('[data-chat-status]');
    const sendBtn = form.querySelector('.send-btn');

    let conversations = [];   // [{id, title, preview, updated_at}], newest first
    let currentId = new URLSearchParams(location.search).get('c');
    let userName = '';
    let memoryTotal = null;
    let sending = false;
    let loadToken = 0;        // guards against a slow load rendering over a newer one
    let listLoaded = false;

    /* ----- composer ----- */
    textarea.addEventListener('input', () => {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 140) + 'px';
    });
    textarea.addEventListener('keydown', (e) => {
      if(e.key === 'Enter' && !e.shiftKey){
        e.preventDefault();
        form.requestSubmit();
      }
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = textarea.value.trim();
      if(!text || sending) return;

      const conversationId = currentId;
      if(!conversationId) scroll.innerHTML = '';   // drop the empty-state hero
      appendMessage('user', text, { at: new Date() });
      textarea.value = '';
      textarea.style.height = 'auto';
      setSending(true);
      showTyping();

      const { ok, status, body } = await apiFetch('/chat/', {
        method: 'POST',
        // The browser's zone lets the twin resolve "tomorrow" / "28th Sept" to the user's own day.
        body: JSON.stringify({ message: text, conversation_id: conversationId, timezone: userTimezone() })
      });

      setSending(false);
      // The user may have switched threads while waiting — only render into the one we sent to.
      const stillHere = currentId === conversationId;
      if(stillHere) removeTyping();

      if(!ok){
        if(!stillHere) return;
        if(status === 404 && conversationId){
          appendMessage('twin', 'That conversation no longer exists. Start a new one.', { error: true });
          return;
        }
        appendMessage('twin', (body && body.detail) || 'Something went wrong reaching your twin.', { error: true });
        return;
      }

      upsertConversation({
        id: body.conversation_id,
        title: body.conversation_title,
        preview: body.reply.slice(0, 120),
        updated_at: new Date().toISOString()
      });

      if(!stillHere) return;
      if(!conversationId){
        currentId = body.conversation_id;
        history.replaceState({ c: currentId }, '', `?c=${currentId}`);
      }
      setHeader(body.conversation_title);
      renderList();
      appendMessage('twin', body.reply, { citations: body.citations, at: new Date() });
      (body.pending_actions || []).forEach(appendActionCard);
    });

    function setSending(value){
      sending = value;
      if(sendBtn) sendBtn.disabled = value;
    }

    /* ----- sidebar ----- */
    document.querySelector('[data-new-chat]')?.addEventListener('click', () => {
      startNewConversation(true);
      rail?.classList.remove('open');
    });
    document.querySelector('[data-convo-toggle]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      rail?.classList.toggle('open');
    });
    // Mobile slide-over: a tap anywhere outside the rail closes it.
    document.addEventListener('click', (e) => {
      if(rail?.classList.contains('open') && !rail.contains(e.target)) rail.classList.remove('open');
    });

    list.addEventListener('click', async (e) => {
      const item = e.target.closest('[data-convo-id]');
      if(!item || e.target.closest('.convo-rename')) return;
      const id = item.dataset.convoId;

      if(e.target.closest('[data-convo-delete]')){
        const convo = conversations.find(c => c.id === id);
        if(!confirm(`Delete "${convo ? convo.title : 'this conversation'}"? This can't be undone.`)) return;
        const { ok } = await apiFetch(`/chat/conversations/${id}`, { method: 'DELETE' });
        if(!ok){ alert('Could not delete that conversation.'); return; }
        conversations = conversations.filter(c => c.id !== id);
        renderList();
        if(id === currentId) startNewConversation(true);
        return;
      }
      if(e.target.closest('[data-convo-rename]')){
        beginRename(item, id);
        return;
      }
      if(id !== currentId){
        history.pushState({ c: id }, '', `?c=${id}`);
        openConversation(id);
      }
      rail?.classList.remove('open');
    });

    window.addEventListener('popstate', () => {
      const id = new URLSearchParams(location.search).get('c');
      if(id) openConversation(id); else startNewConversation(false);
    });

    function beginRename(item, id){
      const titleNode = item.querySelector('.title');
      const convo = conversations.find(c => c.id === id);
      if(!titleNode || !convo) return;
      const input = document.createElement('input');
      input.className = 'convo-rename';
      input.value = convo.title;
      input.maxLength = 200;
      titleNode.replaceWith(input);
      input.focus();
      input.select();

      let done = false;
      const finish = async (save) => {
        if(done) return;
        done = true;
        const next = input.value.trim();
        if(save && next && next !== convo.title){
          const { ok, body } = await apiFetch(`/chat/conversations/${id}`, {
            method: 'PATCH', body: JSON.stringify({ title: next })
          });
          if(ok){
            convo.title = body.title;
            if(id === currentId) setHeader(body.title);
          }
        }
        renderList();
      };
      input.addEventListener('keydown', (e) => {
        if(e.key === 'Enter'){ e.preventDefault(); finish(true); }
        if(e.key === 'Escape'){ e.preventDefault(); finish(false); }
      });
      input.addEventListener('blur', () => finish(true));
    }

    function upsertConversation(convo){
      const existing = conversations.find(c => c.id === convo.id);
      conversations = [ { ...existing, ...convo }, ...conversations.filter(c => c.id !== convo.id) ];
    }

    function renderList(){
      if(!listLoaded) return;
      if(!conversations.length){
        list.innerHTML = '<div class="convo-list-empty">No conversations yet. Ask your twin something to start one.</div>';
        return;
      }
      let lastGroup = null;
      list.innerHTML = conversations.map(c => {
        const group = dateGroup(c.updated_at);
        const label = group !== lastGroup ? `<div class="convo-group-label">${group}</div>` : '';
        lastGroup = group;
        return `${label}
          <div class="convo-item${c.id === currentId ? ' active' : ''}" data-convo-id="${c.id}">
            <div class="title">${escapeHTML(c.title)}</div>
            ${c.preview ? `<div class="preview">${escapeHTML(c.preview)}</div>` : ''}
            <div class="convo-actions">
              <button type="button" data-convo-rename title="Rename" aria-label="Rename">✎</button>
              <button type="button" data-convo-delete title="Delete" aria-label="Delete">🗑</button>
            </div>
          </div>`;
      }).join('');
    }

    function dateGroup(iso){
      const startOfToday = new Date(); startOfToday.setHours(0, 0, 0, 0);
      const days = (startOfToday - new Date(iso)) / 86400000;
      if(days <= 0) return 'Today';
      if(days <= 1) return 'Yesterday';
      if(days <= 7) return 'Previous 7 days';
      if(days <= 30) return 'Previous 30 days';
      return 'Older';
    }

    /* ----- main pane ----- */
    function setHeader(title){
      titleEl.textContent = title || 'Your Twin';
      document.title = `${title || 'Chat'} — Digital Twin`;
    }

    function startNewConversation(updateUrl){
      loadToken++;
      currentId = null;
      if(updateUrl) history.pushState({}, '', location.pathname);
      setHeader(null);
      renderList();
      renderEmptyState();
      textarea.focus();
    }

    function renderEmptyState(){
      const greeting = userName ? `What do you want to remember, ${escapeHTML(userName)}?` : 'What do you want to remember?';
      let sub;
      if(memoryTotal === null) sub = 'Ask anything about your emails and calendar.';
      else if(memoryTotal === 0) sub = 'Your twin has no memories to search yet. <a href="connectors.html">Connect a source</a> to get started.';
      else sub = `Ask anything. Your twin will search ${memoryTotal.toLocaleString()} ${memoryTotal === 1 ? 'memory' : 'memories'} to answer.`;
      scroll.innerHTML = `
        <div class="chat-empty">
          <div class="thread-badge"><div class="core"></div></div>
          <h2>${greeting}</h2>
          <p>${sub}</p>
        </div>`;
    }

    async function openConversation(id){
      const token = ++loadToken;
      currentId = id;
      renderList();
      scroll.innerHTML = '';
      showTyping();

      const { ok, body } = await apiFetch(`/chat/conversations/${id}`, { method: 'GET' });
      if(token !== loadToken) return;
      if(!ok){
        conversations = conversations.filter(c => c.id !== id);
        startNewConversation(false);
        history.replaceState({}, '', location.pathname);
        return;
      }
      setHeader(body.title);
      scroll.innerHTML = '';
      body.messages.forEach(m => {
        appendMessage(m.role === 'user' ? 'user' : 'twin', m.content, { citations: m.citations, at: new Date(m.created_at) });
        (m.pending_actions || []).forEach(appendActionCard);
      });
      scroll.scrollTop = scroll.scrollHeight;
    }

    /* ----- action confirmation cards (email drafts, calendar events) ----- */
    function userTimezone(){
      try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; }
      catch(_) { return null; }
    }

    // Per tool: card heading, confirm-button label, in-flight text, and the "done" status.
    const ACTION_KINDS = {
      draft_email: { icon: '📧', title: 'Draft email', review: 'review before sending', confirm: 'Confirm &amp; Send', busy: 'Sending…', done: '✓ Sent' },
      create_calendar_event: { icon: '📅', title: 'New calendar event', review: 'review before adding', confirm: 'Add to Calendar', busy: 'Adding…', done: '✓ Added to Google Calendar' },
      update_calendar_event: { icon: '✏️', title: 'Change calendar event', review: 'review before saving', confirm: 'Save changes', busy: 'Saving…', done: '✓ Event updated' },
      delete_calendar_event: { icon: '🗑️', title: 'Delete calendar event', review: 'review before deleting', confirm: 'Delete event', busy: 'Deleting…', done: '✓ Event deleted' },
      respond_to_invite: { icon: '✉️', title: 'Reply to invite', review: 'review before replying', confirm: 'Send reply', busy: 'Replying…', done: '✓ Reply sent' }
    };

    scroll.addEventListener('click', async (e) => {
      const confirmBtn = e.target.closest('[data-action-confirm]');
      const cancelBtn = e.target.closest('[data-action-cancel]');
      const btn = confirmBtn || cancelBtn;
      if(!btn) return;
      const card = btn.closest('[data-action-id]');
      const actionId = card.dataset.actionId;
      const kind = ACTION_KINDS[card.dataset.actionTool] || ACTION_KINDS.draft_email;
      const endpoint = confirmBtn ? 'confirm' : 'cancel';

      card.querySelectorAll('button').forEach(b => b.disabled = true);
      const statusNode = card.querySelector('.action-status');
      statusNode.textContent = confirmBtn ? kind.busy : 'Cancelling…';

      const { ok, body } = await apiFetch(`/chat/actions/${actionId}/${endpoint}`, { method: 'POST' });
      if(!ok){
        statusNode.textContent = (body && body.detail) || 'Something went wrong.';
        card.querySelectorAll('button').forEach(b => b.disabled = false);
        return;
      }
      card.querySelectorAll('button').forEach(b => b.remove());
      statusNode.innerHTML = actionStatusHTML(body);
    });

    function actionStatusHTML(action){
      const kind = ACTION_KINDS[action.tool_name] || ACTION_KINDS.draft_email;
      if(action.status === 'executed'){
        const link = action.result && action.result.html_link;
        const safeLink = typeof link === 'string' && link.startsWith('https://') ? link : null;
        return escapeHTML(kind.done) + (safeLink ? ` · <a href="${escapeHTML(safeLink)}" target="_blank" rel="noopener">Open</a>` : '');
      }
      if(action.status === 'cancelled') return 'Cancelled';
      if(action.status === 'failed') return escapeHTML(`Failed: ${(action.result && action.result.error) || 'unknown error'}`);
      return escapeHTML(action.status);
    }

    // `p` is any event shape from the backend: start/end as 'YYYY-MM-DD' (all-day, with
    // Google's exclusive end date) or local 'YYYY-MM-DDTHH:MM'.
    function formatEventWhen(p){
      if(!p || !p.start) return '';
      const opts = { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' };
      if(p.all_day){
        // Plain dates: build them as local days so no timezone shift moves them.
        const [y, m, d] = p.start.split('-').map(Number);
        const startDay = new Date(y, m - 1, d);
        const [ey, em, ed] = (p.end || p.start).split('-').map(Number);
        const lastDay = new Date(ey, em - 1, ed - 1); // Google's all-day end is exclusive
        const range = lastDay > startDay
          ? `${startDay.toLocaleDateString(undefined, opts)} – ${lastDay.toLocaleDateString(undefined, opts)}`
          : startDay.toLocaleDateString(undefined, opts);
        return `${range} · All day`;
      }
      const start = new Date(p.start), end = new Date(p.end || p.start);
      const time = { hour: 'numeric', minute: '2-digit' };
      return `${start.toLocaleDateString(undefined, opts)} · ${start.toLocaleTimeString(undefined, time)} – ${end.toLocaleTimeString(undefined, time)}`;
    }

    const RECURRENCE_LABELS = { daily: 'Every day', weekly: 'Every week', monthly: 'Every month', yearly: 'Every year' };
    const RSVP_LABELS = { accepted: '✅ Yes, going', declined: '❌ No, not going', tentative: '❔ Maybe' };

    function remindersText(minutes){
      if(!minutes || !minutes.length) return 'No reminders';
      return minutes.map(m => {
        if(m === 0) return 'at start';
        if(m % 1440 === 0) return `${m / 1440} day${m === 1440 ? '' : 's'} before`;
        if(m % 60 === 0) return `${m / 60} hr before`;
        return `${m} min before`;
      }).join(', ');
    }

    function guestsText(attendees){
      return (attendees || []).map(a => typeof a === 'string' ? a : a.email).join(', ') || 'None';
    }

    function eventSummaryHTML(ev, { struck = false } = {}){
      const strike = struck ? 'text-decoration:line-through;opacity:.75;' : '';
      return `
        <div style="${strike}"><strong>${escapeHTML(ev.summary)}</strong></div>
        <div style="margin-top:4px;${strike}">${escapeHTML(formatEventWhen(ev))}</div>
        ${ev.location ? `<div>📍 ${escapeHTML(ev.location)}</div>` : ''}`;
    }

    // One "Label: old → new" line per field the update actually changes.
    function changeRowsHTML(before, after){
      const rows = [];
      const row = (label, from, to) => rows.push(
        `<div style="margin-top:4px;"><strong>${label}:</strong> <span style="text-decoration:line-through;opacity:.6;">${escapeHTML(from || '—')}</span> → ${escapeHTML(to || '—')}</div>`);
      if(before.summary !== after.summary) row('Title', before.summary, after.summary);
      if(before.start !== after.start || before.end !== after.end || before.all_day !== after.all_day) row('When', formatEventWhen(before), formatEventWhen(after));
      if((before.location || '') !== (after.location || '')) row('Where', before.location, after.location);
      if((before.description || '') !== (after.description || '')) row('Notes', before.description, after.description);
      if('recurrence' in after) row('Repeats', before.recurring ? 'Repeating' : 'Does not repeat', RECURRENCE_LABELS[after.recurrence] || 'Does not repeat');
      if('reminder_minutes' in after) row('Reminders', 'Default', remindersText(after.reminder_minutes));
      if(guestsText(before.attendees) !== guestsText(after.attendees)) row('Guests', guestsText(before.attendees), guestsText(after.attendees));
      return rows.join('');
    }

    function actionDetailsHTML(action){
      const p = action.payload;
      const seriesNote = p.apply_to === 'series' ? '<div style="margin-top:4px;">🔁 Applies to every event in the series</div>' : '';
      if(action.tool_name === 'create_calendar_event'){
        return `
          ${eventSummaryHTML(p)}
          ${p.recurrence ? `<div>🔁 ${escapeHTML(RECURRENCE_LABELS[p.recurrence] || p.recurrence)}</div>` : ''}
          ${p.reminder_minutes ? `<div>⏰ ${escapeHTML(remindersText(p.reminder_minutes))}</div>` : ''}
          ${p.attendees && p.attendees.length ? `<div><strong>Invite:</strong> ${escapeHTML(p.attendees.join(', '))}</div>` : ''}
          ${p.description ? `<div style="margin-top:6px;white-space:pre-wrap;">${escapeHTML(p.description)}</div>` : ''}
          <div style="margin-top:4px;font-size:12px;opacity:.6;">${escapeHTML(p.calendar_name ? `${p.calendar_name} · ${p.timezone}` : p.timezone)}</div>`;
      }
      if(action.tool_name === 'update_calendar_event'){
        return `
          <div><strong>${escapeHTML(p.before.summary)}</strong></div>
          ${changeRowsHTML(p.before, p.after)}
          ${seriesNote}`;
      }
      if(action.tool_name === 'delete_calendar_event'){
        return `
          ${eventSummaryHTML(p.event, { struck: true })}
          ${seriesNote}
          ${p.send_updates === 'all' ? '<div style="margin-top:4px;">Guests will be told it is cancelled.</div>' : ''}`;
      }
      if(action.tool_name === 'respond_to_invite'){
        return `
          ${eventSummaryHTML(p.event)}
          ${p.event.organizer ? `<div>From ${escapeHTML(p.event.organizer)}</div>` : ''}
          <div style="margin-top:6px;"><strong>Your reply:</strong> ${escapeHTML(RSVP_LABELS[p.response] || p.response)}</div>
          ${seriesNote}`;
      }
      return emailDraftHTML(p);
    }

    function fileSizeText(bytes){
      if(!bytes) return '';
      if(bytes < 1024) return `${bytes} B`;
      if(bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }

    function emailDraftHTML(p){
      const files = p.attachments || [];
      const fromEmail = f => f.source === 'gmail' ? ' · from an email' : '';
      return `
        ${p.reply_to ? `<div style="margin-bottom:4px;opacity:.75;">↩ Reply in the thread “${escapeHTML(p.reply_to.subject || '(no subject)')}”</div>` : ''}
        <div><strong>To:</strong> ${escapeHTML(p.to)}</div>
        ${p.cc && p.cc.length ? `<div><strong>Cc:</strong> ${escapeHTML(p.cc.join(', '))}</div>` : ''}
        <div><strong>Subject:</strong> ${escapeHTML(p.subject)}</div>
        <div style="margin-top:6px;white-space:pre-wrap;">${escapeHTML(p.body)}</div>
        ${files.length ? `<div style="margin-top:8px;">${files.map(f =>
          `<div>📎 ${escapeHTML(f.filename)} <span style="font-size:12px;opacity:.6;">${escapeHTML(fileSizeText(f.size) + fromEmail(f))}</span></div>`).join('')}</div>` : ''}`;
    }

    function appendActionCard(action){
      const kind = ACTION_KINDS[action.tool_name];
      if(!kind) return; // a tool this page doesn't know how to preview — never offer to confirm it blind
      const pending = action.status === 'pending';
      const msg = document.createElement('div');
      msg.className = 'msg from-twin';
      msg.innerHTML = `
        <div class="avatar">${twinBadgeHTML()}</div>
        <div>
          <div class="bubble action-card" data-action-id="${action.id}" data-action-tool="${escapeHTML(action.tool_name)}">
            <div style="font-weight:600;margin-bottom:8px;">${kind.icon} ${kind.title}${pending ? ` — ${kind.review}` : ''}</div>
            ${actionDetailsHTML(action)}
            <div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              ${pending ? `<button class="btn btn-primary btn-sm" data-action-confirm>${kind.confirm}</button>
              <button class="btn btn-ghost btn-sm" data-action-cancel>Cancel</button>` : ''}
              <span class="action-status" style="font-size:13px;opacity:.7;">${pending ? '' : actionStatusHTML(action)}</span>
            </div>
          </div>
        </div>`;
      scroll.appendChild(msg);
      scroll.scrollTop = scroll.scrollHeight;
    }

    /* ----- messages ----- */
    function appendMessage(who, text, { citations = [], at = null, error = false } = {}){
      const msg = document.createElement('div');
      msg.className = `msg from-${who}${error ? ' is-error' : ''}`;
      const content = who === 'twin' && !error ? renderMarkdown(text) : escapeHTML(text);
      msg.innerHTML = `
        <div class="avatar">${who === 'twin' ? twinBadgeHTML() : '🙂'}</div>
        <div>
          <div class="bubble">${content}</div>
          ${memoryResultsHTML(citations)}
          ${at ? `<div class="msg-meta">${formatMessageTime(at)}</div>` : ''}
        </div>`;
      scroll.appendChild(msg);
      scroll.scrollTop = scroll.scrollHeight;
    }

    function memoryResultsHTML(citations){
      if(!citations || !citations.length) return '';
      const noun = citations.length === 1 ? 'memory' : 'memories';
      const items = citations.map(c => `
        <div class="memory-result">
          <div class="memory-result-icon">${sourceIconSVG(c.source)}</div>
          <div class="memory-result-body">
            <div class="memory-result-top">
              <div class="memory-result-title">${escapeHTML(c.title)}</div>
              <div class="memory-result-date">${formatCitationDate(c.occurred_at)}</div>
            </div>
            <div class="memory-result-source">${SOURCE_LABELS[c.source] || c.source}</div>
            <div class="memory-result-snippet">${escapeHTML(c.snippet)}</div>
          </div>
        </div>`).join('');
      return `<div class="memory-results">
          <div class="memory-results-head">⟡ ${citations.length} ${noun} referenced</div>
          ${items}
        </div>`;
    }

    function sourceIconSVG(source){
      const path = ACTIVITY_ICONS[source] || ACTIVITY_ICONS.connected;
      return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${path}</svg>`;
    }

    function formatCitationDate(iso){
      const d = new Date(iso);
      const sameYear = d.getFullYear() === new Date().getFullYear();
      const dateOpts = sameYear
        ? { month: 'short', day: 'numeric' }
        : { month: 'short', day: 'numeric', year: 'numeric' };
      return `${d.toLocaleDateString([], dateOpts)}, ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    }

    function formatMessageTime(date){
      const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const sameDay = date.toDateString() === new Date().toDateString();
      return sameDay ? time : `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
    }

    function showTyping(){
      removeTyping();
      const t = document.createElement('div');
      t.className = 'msg from-twin';
      t.id = 'typingMsg';
      t.innerHTML = `
        <div class="avatar">${twinBadgeHTML()}</div>
        <div class="bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>`;
      scroll.appendChild(t);
      scroll.scrollTop = scroll.scrollHeight;
    }
    function removeTyping(){
      const t = document.getElementById('typingMsg');
      if(t) t.remove();
    }
    function twinBadgeHTML(){
      return `<div class="thread-badge" style="width:32px;height:32px;"><div class="core" style="width:10px;height:10px;"></div></div>`;
    }
    function escapeHTML(str){
      const div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    }

    /* ----- boot ----- */
    (async () => {
      if(currentId) openConversation(currentId); else renderEmptyState();

      const [convos, summary] = await Promise.all([
        apiFetch('/chat/conversations', { method: 'GET' }),
        apiFetch('/dashboard/', { method: 'GET' })
      ]);
      if(convos.ok) conversations = convos.body;
      listLoaded = true;
      renderList();

      if(summary.ok){
        userName = firstName(summary.body.full_name);
        memoryTotal = summary.body.memories.total;
        statusEl.textContent = `● ${memoryTotal.toLocaleString()} ${memoryTotal === 1 ? 'memory' : 'memories'} indexed`;
        if(!currentId) renderEmptyState();
      }
    })();
  }

  /* Minimal, safe markdown for twin replies: everything is HTML-escaped first, then a small
     whitelist (fenced/inline code, bold, italic, headings, lists, paragraphs) is re-applied. */
  function renderMarkdown(src){
    const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const inline = (s) => s
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');

    const blocks = [];
    const codeBlocks = [];
    const text = esc(src || '').replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => {
      codeBlocks.push(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`);
      return `\u0000${codeBlocks.length - 1}\u0000`;
    });

    let listType = null, listItems = [], para = [];
    const flushPara = () => { if(para.length){ blocks.push(`<p>${inline(para.join('<br>'))}</p>`); para = []; } };
    const flushList = () => {
      if(listType){ blocks.push(`<${listType}>${listItems.map(li => `<li>${inline(li)}</li>`).join('')}</${listType}>`); }
      listType = null; listItems = [];
    };

    text.split('\n').forEach(line => {
      const trimmed = line.trim();
      const code = trimmed.match(/^\u0000(\d+)\u0000$/);
      const bullet = trimmed.match(/^[-*•]\s+(.*)$/);
      const numbered = trimmed.match(/^\d+[.)]\s+(.*)$/);
      const heading = trimmed.match(/^#{1,6}\s+(.*)$/);
      if(code){ flushPara(); flushList(); blocks.push(codeBlocks[+code[1]]); }
      else if(bullet || numbered){
        flushPara();
        const type = bullet ? 'ul' : 'ol';
        if(listType !== type) flushList();
        listType = type;
        listItems.push((bullet || numbered)[1]);
      }
      else if(heading){ flushPara(); flushList(); blocks.push(`<h4>${inline(heading[1])}</h4>`); }
      else if(!trimmed){ flushPara(); flushList(); }
      else { flushList(); para.push(trimmed); }
    });
    flushPara(); flushList();
    // Fences that sat inside a paragraph line rather than on their own line.
    return blocks.join('').replace(/\u0000(\d+)\u0000/g, (_, i) => codeBlocks[+i]);
  }

  /* ---------- dashboard ---------- */
  const SOURCE_LABELS = {
    gmail: 'Gmail', google_calendar: 'Google Calendar',
    document: 'Documents', image: 'Images', voice_note: 'Voice notes', bookmark: 'Bookmarks'
  };
  // Sources that come from files on the Uploads page rather than a connected account.
  const UPLOAD_SOURCES = ['document', 'image', 'voice_note', 'bookmark'];
  const UPLOAD_NOUNS = {
    document: ['document', 'documents'], image: ['image', 'images'], voice_note: ['voice note', 'voice notes'], bookmark: ['bookmark', 'bookmarks']
  };
  const ACTIVITY_ICONS = {
    gmail: '<path d="M4 4h16v16H4z"/><polyline points="4,6 12,13 20,6"/>',
    google_calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>',
    document: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    voice_note: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><line x1="12" y1="18" x2="12" y2="22"/>',
    bookmark: '<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
    connected: '<circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><path d="M8.5 8.5 15.5 15.5"/>'
  };

  async function initDashboard(){
    const el = (key) => document.querySelector(`[data-dash="${key}"]`);
    if(!el('total')) return;

    const { ok, body } = await apiFetch('/dashboard/', { method: 'GET' });
    if(!ok){
      el('subtitle').textContent = "Couldn't reach your twin right now. Try refreshing.";
      el('activity').innerHTML = rowHTML(null, 'Nothing to show', 'Dashboard data failed to load', '');
      return;
    }

    const { email, full_name: fullName, memories: m, sources, last_indexed_at: lastIndexed, activity } = body;
    const first = firstName(fullName);
    el('greeting').textContent = first ? `Good to see you, ${first}.` : 'Good to see you.';

    if(!sources.connected){
      el('subtitle').innerHTML = 'No sources connected yet. <a href="connectors.html">Connect one</a> to start building your twin.';
    }else if(!lastIndexed){
      el('subtitle').textContent = 'Your sources are connected. The first sync lands within 15 minutes.';
    }else{
      el('subtitle').textContent = `Your twin has been listening. Last new memory indexed ${timeAgo(lastIndexed)}.`;
    }

    el('total').textContent = m.total.toLocaleString();
    setDelta(el('total-delta'), m.today ? `↑ ${m.today.toLocaleString()} today` : 'Nothing new today', m.today ? 'up' : '');

    el('sources').textContent = sources.connected;
    setDelta(el('sources-delta'), `of ${sources.available} available`, '');

    el('week').textContent = m.this_week.toLocaleString();
    const diff = m.this_week - m.last_week;
    setDelta(el('week-delta'),
      diff === 0 ? 'Same as last week' : `${diff > 0 ? '↑' : '↓'} ${Math.abs(diff).toLocaleString()} vs last week`,
      diff > 0 ? 'up' : diff < 0 ? 'down' : '');

    if(m.total){
      const pct = (m.searchable / m.total) * 100;
      el('searchable').textContent = `${pct === 100 ? 100 : pct.toFixed(1)}%`;
      setDelta(el('searchable-delta'), `${m.searchable.toLocaleString()} of ${m.total.toLocaleString()} embedded`, '');
    }else{
      setDelta(el('searchable-delta'), 'No memories yet', '');
    }

    el('activity').innerHTML = activity.length
      ? activity.map(a => {
          const label = SOURCE_LABELS[a.source] || a.source;
          if(a.kind === 'connected'){
            return rowHTML(ACTIVITY_ICONS.connected, `${label} connected`, 'New source linked to your twin', timeAgo(a.at));
          }
          if(UPLOAD_SOURCES.includes(a.source)){
            const noun = UPLOAD_NOUNS[a.source];
            return rowHTML(ACTIVITY_ICONS[a.source], `${a.count.toLocaleString()} ${noun[a.count === 1 ? 0 : 1]} added`, 'Uploaded by you', timeAgo(a.at));
          }
          return rowHTML(ACTIVITY_ICONS[a.source] || ACTIVITY_ICONS.connected,
                `${label} synced — ${a.count.toLocaleString()} new ${a.count === 1 ? 'item' : 'items'} indexed`,
                label, timeAgo(a.at));
        }).join('')
      : rowHTML(null, 'No activity yet', 'Connect a source and your twin will start indexing', '');

    function setDelta(node, text, cls){
      node.textContent = text;
      node.classList.remove('up', 'down');
      if(cls) node.classList.add(cls);
    }
    function rowHTML(iconPaths, title, meta, time){
      const icon = iconPaths
        ? `<div class="row-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${iconPaths}</svg></div>`
        : '';
      return `<div class="row-item">${icon}<div class="row-body"><div class="title">${escapeText(title)}</div><div class="meta">${escapeText(meta)}</div></div><div class="row-time">${escapeText(time)}</div></div>`;
    }
  }

  // First name for greetings — only from a real full name (signup / Settings → Profile).
  // Guessing from the email gives things like "Myashi1229", so no name beats a wrong one.
  function firstName(fullName){
    return (fullName || '').trim().split(/\s+/)[0] || '';
  }

  function timeAgo(iso){
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if(seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if(minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if(hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if(days === 1) return 'Yesterday';
    if(days < 7) return `${days} days ago`;
    return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function escapeText(str){
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  /* ---------- timeline ---------- */
  // Newest-first feed from /memory/timeline, keyset-paginated (infinite scroll). Source filter
  // and search run server-side and are mirrored in the URL (?source=&q=) so reloads keep them.
  function initTimeline(){
    const wrap = document.querySelector('[data-timeline]');
    if(!wrap) return;
    const filters = document.querySelector('[data-timeline-filters]');
    const statusEl = document.querySelector('[data-timeline-status]');
    const search = document.querySelector('[data-timeline-search]');
    const sentinel = document.querySelector('[data-timeline-sentinel]');

    const params = new URLSearchParams(location.search);
    let source = params.get('source') || '';
    let query = params.get('q') || '';
    if(search) search.value = query;

    let cursor = null;
    let done = false;
    let loading = false;
    let token = 0;            // bumps on every reset so stale responses are dropped
    let lastGroupKey = null;
    let lastGroupEl = null;
    let shown = 0;

    async function load(reset){
      if(reset){
        token++;
        cursor = null; done = false; lastGroupKey = null; lastGroupEl = null; shown = 0;
        wrap.innerHTML = '';
        statusEl.textContent = 'Loading your timeline…';
      }else if(loading || done){
        return;
      }
      loading = true;
      const myToken = token;
      const firstPage = !cursor;

      const qs = new URLSearchParams({ limit: '30' });
      if(source) qs.set('source', source);
      if(query) qs.set('q', query);
      if(cursor) qs.set('cursor', cursor);
      const { ok, body } = await apiFetch(`/memory/timeline?${qs}`, { method: 'GET' });
      if(myToken !== token) return;
      loading = false;

      if(!ok){
        done = true;
        statusEl.textContent = "Couldn't load your timeline. Try refreshing.";
        return;
      }

      if(firstPage){
        renderFilters(body.sources);
        if(body.upcoming.length){
          const group = document.createElement('div');
          group.className = 'timeline-group upcoming';
          group.innerHTML = `<div class="timeline-date">Coming up</div>${body.upcoming.map(i => entryHTML(i, true)).join('')}`;
          wrap.appendChild(group);
        }
        shown += body.upcoming.length;
      }
      appendItems(body.items);
      shown += body.items.length;
      cursor = body.next_cursor;
      done = !cursor;
      statusEl.innerHTML = statusHTML(body.sources, firstPage);

      // A short first page can leave the sentinel on screen, which the observer won't re-report.
      if(!done && sentinel && sentinel.getBoundingClientRect().top < window.innerHeight + 400) load(false);
    }

    function statusHTML(sources, firstPage){
      if(!done) return 'Loading more…';
      if(shown) return shown > 8 ? "You've reached the beginning of your timeline." : '';
      if(query) return `Nothing in your timeline matches “${escapeText(query)}”.`;
      if(source) return 'No memories from this source yet.';
      if(firstPage && !sources.length){
        return 'No memories yet. <a href="connectors.html">Connect a source</a> or <a href="uploads.html">upload files</a> and your twin will start filling this in.';
      }
      return 'Nothing here yet.';
    }

    function appendItems(items){
      items.forEach(item => {
        const at = itemDate(item);
        const key = at.toDateString();
        if(key !== lastGroupKey){
          lastGroupKey = key;
          lastGroupEl = document.createElement('div');
          lastGroupEl.className = 'timeline-group';
          lastGroupEl.innerHTML = `<div class="timeline-date">${escapeText(dayLabel(at))}</div>`;
          wrap.appendChild(lastGroupEl);
        }
        lastGroupEl.insertAdjacentHTML('beforeend', entryHTML(item, false));
      });
    }

    function renderFilters(sources){
      if(!filters) return;
      if(!sources.length){ filters.innerHTML = ''; return; }
      const total = sources.reduce((n, s) => n + s.count, 0);
      const chip = (value, label, count) =>
        `<button class="filter-chip${value === source ? ' active' : ''}" data-source="${value}">${escapeText(label)} · ${count.toLocaleString()}</button>`;
      filters.innerHTML = chip('', 'All sources', total)
        + sources.map(s => chip(s.source, SOURCE_LABELS[s.source] || s.source, s.count)).join('');
    }

    // All-day events are stored at UTC midnight of their date; read them back in UTC so they
    // don't land on the previous day for anyone west of Greenwich.
    function itemDate(item){
      const at = new Date(item.occurred_at);
      return item.all_day ? new Date(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate()) : at;
    }

    function entryHTML(item, upcoming){
      const at = itemDate(item);
      const d = item.details || {};
      let time = item.all_day ? 'All day' : clockTime(at);
      if(item.source === 'google_calendar' && !item.all_day && d.end){
        const end = new Date(d.end);
        if(!isNaN(end) && end.toDateString() === at.toDateString()) time += ` – ${clockTime(end)}`;
      }
      if(upcoming) time = `${shortDay(at)} · ${time}`;

      const meta = [`<span class="t-source">${escapeText(SOURCE_LABELS[item.source] || item.source)}</span>`];
      if(item.source === 'gmail' && d.from) meta.push(`<span>From ${escapeText(senderName(d.from))}</span>`);
      if(item.source === 'gmail' && d.email_subject) meta.push(`<span>📎 Attached to “${escapeText(d.email_subject)}”</span>`);
      if(item.source === 'gmail' && d.attachments && d.attachments.length) meta.push(`<span>📎 ${d.attachments.length} ${d.attachments.length === 1 ? 'attachment' : 'attachments'}</span>`);
      if(item.source === 'google_calendar'){
        if(d.location) meta.push(`<span>📍 ${escapeText(d.location)}</span>`);
        if(d.attendee_count) meta.push(`<span>${d.attendee_count} ${d.attendee_count === 1 ? 'attendee' : 'attendees'}</span>`);
        if(typeof d.html_link === 'string' && d.html_link.startsWith('https://')){
          meta.push(`<a href="${escapeText(d.html_link).replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">Open in Google Calendar ↗</a>`);
        }
      }
      if(item.source === 'image'){
        if(d.width && d.height) meta.push(`<span>${d.width}×${d.height}</span>`);
        if(d.camera) meta.push(`<span>📷 ${escapeText(d.camera)}</span>`);
      }
      if(item.source === 'document' || item.source === 'image' || item.source === 'voice_note'){
        if(d.pages) meta.push(`<span>${d.pages} ${d.pages === 1 ? 'page' : 'pages'}</span>`);
        // Browsing shows a file once; a search hit can be any part of it.
        if(d.chunk_count > 1 && d.chunk_index > 0) meta.push(`<span>Part ${d.chunk_index + 1} of ${d.chunk_count}</span>`);
        if(d.upload_id) meta.push(`<a href="${API_BASE}/uploads/${encodeURIComponent(d.upload_id)}/file">Download original</a>`);
      }
      if(item.source === 'bookmark'){
        if(d.folder) meta.push(`<span>📁 ${escapeText(d.folder)}</span>`);
        if(typeof d.url === 'string' && /^https?:\/\//.test(d.url)){
          meta.push(`<a href="${escapeText(d.url).replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">Open ↗</a>`);
        }
      }
      return `
        <div class="timeline-entry" data-source="${escapeText(item.source)}">
          <div class="t-head"><h4>${escapeText(item.title)}</h4><span class="time">${escapeText(time)}</span></div>
          ${item.body ? `<p>${escapeText(item.body)}</p>` : ''}
          <div class="t-meta">${meta.join('')}</div>
        </div>`;
    }

    function clockTime(date){
      return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }
    function monthDay(date){
      const opts = { month: 'long', day: 'numeric' };
      if(date.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
      return date.toLocaleDateString([], opts);
    }
    function dayOffset(date){
      const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
      return Math.round((start(date) - start(new Date())) / 86400000);
    }
    function dayLabel(date){
      const offset = dayOffset(date);
      if(offset === 0) return `Today · ${monthDay(date)}`;
      if(offset === -1) return `Yesterday · ${monthDay(date)}`;
      return monthDay(date);
    }
    function shortDay(date){
      const offset = dayOffset(date);
      if(offset === 0) return 'Today';
      if(offset === 1) return 'Tomorrow';
      return date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    }
    function senderName(from){
      // "Priya Nandan <priya@x.com>" → "Priya Nandan"; bare addresses pass through.
      const match = from.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>\s*$/);
      return match ? match[1] : from;
    }

    function syncUrl(){
      const next = new URLSearchParams();
      if(source) next.set('source', source);
      if(query) next.set('q', query);
      const qs = next.toString();
      history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
    }

    filters?.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-source]');
      if(!chip || chip.dataset.source === source) return;
      source = chip.dataset.source;
      syncUrl();
      load(true);
    });

    let debounce;
    search?.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        const next = search.value.trim();
        if(next === query) return;
        query = next;
        syncUrl();
        load(true);
      }, 300);
    });

    if(sentinel && 'IntersectionObserver' in window){
      new IntersectionObserver((entries) => {
        if(entries.some(e => e.isIntersecting)) load(false);
      }, { rootMargin: '400px' }).observe(sentinel);
    }

    load(true);
  }

  /* ---------- uploads ---------- */
  // Manual ingestion: files are parsed, embedded and stored inside the POST itself, so each
  // row in the queue goes straight from "Indexing…" to done/failed — nothing to poll.
  const UPLOAD_KIND_LABELS = { document: 'Document', image: 'Image', voice_note: 'Voice note', bookmark: 'Bookmark export' };

  function formatBytes(bytes){
    if(bytes < 1024) return `${bytes} B`;
    if(bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function initUploads(){
    const listEl = document.querySelector('[data-upload-list]');
    if(!listEl) return;
    const dropzone = document.querySelector('[data-dropzone]');
    const input = document.querySelector('[data-upload-input]');
    const queueEl = document.querySelector('[data-upload-queue]');
    const countEl = document.querySelector('[data-upload-count]');
    const queue = [];
    let running = false;

    async function loadList(){
      const { ok, body } = await apiFetch('/uploads/', { method: 'GET' });
      if(!ok){
        listEl.innerHTML = '<div class="row-item"><div class="row-body"><div class="meta">Could not load your files. Try refreshing.</div></div></div>';
        return;
      }
      countEl.textContent = body.length ? `${body.length} ${body.length === 1 ? 'file' : 'files'}` : '';
      listEl.innerHTML = body.length
        ? body.map(fileRowHTML).join('')
        : '<div class="empty-state"><h3>Nothing uploaded yet</h3><p>Upload a certificate, resume or note, then ask your twin about it in chat.</p></div>';
    }

    function fileRowHTML(u){
      const d = u.details || {};
      const facts = [UPLOAD_KIND_LABELS[u.kind] || u.kind];
      if(d.pages) facts.push(`${d.pages} ${d.pages === 1 ? 'page' : 'pages'}`);
      facts.push(u.kind === 'bookmark'
        ? `${u.items.toLocaleString()} ${u.items === 1 ? 'bookmark' : 'bookmarks'}`
        : `${u.items.toLocaleString()} ${u.items === 1 ? 'passage' : 'passages'} indexed`);
      facts.push(formatBytes(u.size_bytes));
      if(d.truncated) facts.push('only the first part was indexed');
      const id = encodeURIComponent(u.id);
      return `
        <div class="row-item">
          <div class="row-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${ACTIVITY_ICONS[u.kind] || ACTIVITY_ICONS.document}</svg></div>
          <div class="row-body"><div class="title">${escapeText(u.filename)}</div><div class="meta">${escapeText(facts.join(' · '))}</div></div>
          <div class="row-time">${escapeText(timeAgo(u.created_at))}</div>
          <div class="upload-actions">
            <a class="btn btn-ghost btn-sm" href="${API_BASE}/uploads/${id}/file">Download</a>
            <button type="button" class="btn btn-ghost btn-sm" data-delete-upload="${escapeText(u.id)}" data-filename="${escapeText(u.filename)}" data-items="${u.items}">Delete</button>
          </div>
        </div>`;
    }

    function enqueue(files){
      [...files].forEach(file => {
        const row = document.createElement('div');
        row.className = 'upload-job';
        row.innerHTML = `<span class="upload-job-name">${escapeText(file.name)}</span><span class="upload-job-status">Waiting…</span>`;
        queueEl.prepend(row);
        queue.push({ file, row });
      });
      run();
    }

    // One at a time: each upload embeds its whole file, and parallel uploads would just
    // trip the embedding provider's rate limit.
    async function run(){
      if(running) return;
      running = true;
      while(queue.length){
        const { file, row } = queue.shift();
        const status = row.querySelector('.upload-job-status');
        status.textContent = file.type.startsWith('audio/') ? 'Transcribing & indexing…' : 'Indexing…';
        row.classList.add('busy');

        const form = new FormData();
        form.append('file', file);
        if(file.lastModified) form.append('occurred_at', new Date(file.lastModified).toISOString());
        let result;
        try{
          // Not apiFetch: that forces a JSON Content-Type, and the browser must set the
          // multipart boundary itself.
          const res = await fetch(`${API_BASE}/uploads/`, { method: 'POST', credentials: 'include', body: form });
          let body = null;
          try{ body = await res.json(); }catch(e){ /* no/invalid JSON body */ }
          result = { ok: res.ok, body };
        }catch(e){
          result = { ok: false, body: { detail: "Couldn't reach your twin. Check your connection." } };
        }

        row.classList.remove('busy');
        if(result.ok){
          const n = result.body.items;
          row.classList.add('done');
          status.textContent = result.body.kind === 'bookmark'
            ? `Added ${n.toLocaleString()} ${n === 1 ? 'bookmark' : 'bookmarks'}`
            : `Indexed ${n.toLocaleString()} ${n === 1 ? 'passage' : 'passages'}`;
          loadList();
        }else{
          row.classList.add('failed');
          const detail = result.body && result.body.detail;
          status.textContent = typeof detail === 'string' ? detail : 'Upload failed.';
        }
      }
      running = false;
    }

    input.addEventListener('change', () => { enqueue(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach(type => dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.add('dragging');
    }));
    ['dragleave', 'drop'].forEach(type => dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragging');
    }));
    dropzone.addEventListener('drop', (e) => { if(e.dataTransfer?.files.length) enqueue(e.dataTransfer.files); });

    listEl.addEventListener('click', async (e) => {
      const button = e.target.closest('[data-delete-upload]');
      if(!button) return;
      const items = Number(button.dataset.items);
      const what = items ? ` and the ${items.toLocaleString()} ${items === 1 ? 'memory' : 'memories'} indexed from it` : '';
      if(!confirm(`Delete “${button.dataset.filename}”${what}? This can't be undone.`)) return;
      button.disabled = true;
      button.textContent = 'Deleting…';
      const { ok } = await apiFetch(`/uploads/${encodeURIComponent(button.dataset.deleteUpload)}`, { method: 'DELETE' });
      if(!ok){
        button.disabled = false;
        button.textContent = 'Delete';
        alert('Could not delete that file. Try again.');
        return;
      }
      loadList();
    });

    loadList();
  }

  /* ---------- generic filter chips (timeline / connectors category use this too) ---------- */
  function initFilterChips(containerSelector, itemSelector, attr){
    const container = document.querySelector(containerSelector);
    if(!container) return;
    const chips = container.querySelectorAll('.filter-chip');
    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        chips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        const val = chip.dataset.filter;
        document.querySelectorAll(itemSelector).forEach(item => {
          item.style.display = (val === 'all' || item.dataset[attr] === val) ? '' : 'none';
        });
      });
    });
  }

  /* ---------- search page ---------- */
  // Hybrid keyword + meaning search via /memory/search. Query, source filter and sort live in
  // the URL (?q=&source=&sort=). Searches fire on Enter or after a pause in typing — the
  // meaning half costs an embedding call, and the free Voyage tier allows only a few a minute.
  function initSearchPage(){
    const input = document.querySelector('[data-live-search]');
    if(!input) return;
    const form = document.querySelector('[data-search-form]');
    const hintEl = document.querySelector('[data-search-hint]');
    const suggestionsEl = document.querySelector('[data-search-suggestions]');
    const toolbar = document.querySelector('[data-search-toolbar]');
    const sourcesEl = document.querySelector('[data-search-sources]');
    const statusEl = document.querySelector('[data-search-status]');
    const resultsCard = document.querySelector('[data-results-card]');
    const resultsEl = document.querySelector('[data-search-results]');
    const moreBtn = document.querySelector('[data-load-more]');
    const emptyEl = document.querySelector('[data-search-empty]');
    const emptyText = document.querySelector('[data-search-empty-text]');
    const sortGroup = toolbar.querySelector('.search-sort');

    const params = new URLSearchParams(location.search);
    let query = (params.get('q') || '').trim();
    let source = params.get('source') || '';
    let sort = params.get('sort') === 'newest' ? 'newest' : 'relevance';
    let sources = [];
    let offset = 0;
    let token = 0;
    let terms = [];
    let cursor = null; // browse mode (no query) pages through the timeline instead
    input.value = query;

    const PAGE = 20;
    const DEBOUNCE_MS = 550;

    /* ----- starting points ----- */
    (async () => {
      const { ok, body } = await apiFetch('/memory/search/suggestions', { method: 'GET' });
      if(!ok) return;
      sources = body.sources;
      renderSources();
      if(!query && sources.length >= 2) toolbar.hidden = false;
      if(!body.total){
        hintEl.innerHTML = 'Nothing to search yet. <a href="connectors.html">Connect a source</a> or <a href="uploads.html">upload files</a> first.';
        return;
      }
      const names = body.sources.map(s => SOURCE_LABELS[s.source] || s.source);
      hintEl.textContent = `Searching ${body.total.toLocaleString()} ${body.total === 1 ? 'memory' : 'memories'} across ${listJoin(names)}.`;
      suggestionsEl.innerHTML = body.suggestions
        .map(s => `<button type="button" class="filter-chip" data-suggest="${escapeText(s).replace(/"/g, '&quot;')}">${escapeText(s)}</button>`)
        .join('');
    })();

    suggestionsEl.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-suggest]');
      if(!chip) return;
      input.value = chip.dataset.suggest;
      run(true);
    });

    function listJoin(items){
      if(items.length <= 1) return items.join('');
      return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
    }

    /* ----- filters & sort ----- */
    function renderSources(){
      if(sources.length < 2){ sourcesEl.innerHTML = ''; return; }
      const chip = (value, label) =>
        `<button type="button" class="filter-chip${value === source ? ' active' : ''}" data-source="${value}">${escapeText(label)}</button>`;
      sourcesEl.innerHTML = chip('', 'All sources') + sources.map(s => chip(s.source, SOURCE_LABELS[s.source] || s.source)).join('');
    }
    sourcesEl.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-source]');
      if(!chip || chip.dataset.source === source) return;
      source = chip.dataset.source;
      renderSources();
      run(true);
    });
    toolbar.querySelectorAll('[data-sort]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.sort === sort);
      btn.addEventListener('click', () => {
        if(btn.dataset.sort === sort) return;
        sort = btn.dataset.sort;
        toolbar.querySelectorAll('[data-sort]').forEach(b => b.classList.toggle('active', b === btn));
        run(true);
      });
    });

    /* ----- running a search ----- */
    let debounce;
    input.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => run(false), DEBOUNCE_MS);
    });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      clearTimeout(debounce);
      run(true);
    });
    moreBtn.addEventListener('click', () => query ? load(false) : browse(false));

    function syncUrl(){
      const next = new URLSearchParams();
      if(query) next.set('q', query);
      if(source) next.set('source', source);
      if(sort !== 'relevance') next.set('sort', sort);
      const qs = next.toString();
      history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
    }

    function run(force){
      const next = input.value.trim();
      if(!force && next === query && offset) return;
      query = next;
      syncUrl();
      if(!query){ browse(true); return; }
      load(true);
    }

    // With nothing typed, show the newest memories (same cards, filters and paging) so the
    // page is never blank; typing switches to search, clearing the box comes back here.
    async function browse(reset){
      if(reset){ cursor = null; offset = 0; }
      const myToken = ++token;
      terms = [];
      sortGroup.hidden = true;
      emptyEl.hidden = true;
      moreBtn.hidden = !reset;
      if(!reset){ moreBtn.disabled = true; moreBtn.textContent = 'Loading…'; }

      const qs = new URLSearchParams({ limit: String(PAGE) });
      if(source) qs.set('source', source);
      if(cursor) qs.set('cursor', cursor);
      const { ok, body } = await apiFetch(`/memory/timeline?${qs}`, { method: 'GET' });
      if(myToken !== token) return;
      moreBtn.disabled = false;
      moreBtn.textContent = 'Load more';
      if(!ok){ statusEl.textContent = "Couldn't load your recent memories."; return; }

      if(reset) resultsEl.innerHTML = '';
      toolbar.hidden = sources.length < 2;
      cursor = body.next_cursor;
      offset += body.items.length;
      moreBtn.hidden = !cursor;
      if(!offset){
        resultsCard.hidden = true;
        statusEl.textContent = '';
        return;
      }
      resultsCard.hidden = false;
      resultsEl.insertAdjacentHTML('beforeend', body.items.map(resultHTML).join(''));
      statusEl.textContent = source
        ? `Most recent from ${SOURCE_LABELS[source] || source}`
        : 'Your most recent memories';
    }

    async function load(reset){
      if(reset){ offset = 0; }
      const myToken = ++token;
      moreBtn.hidden = true;
      if(reset){
        statusEl.innerHTML = '<span class="search-spinner"></span>Searching…';
        emptyEl.hidden = true;
      }else{
        moreBtn.hidden = false;
        moreBtn.disabled = true;
        moreBtn.textContent = 'Loading…';
      }

      const qs = new URLSearchParams({ q: query, sort, limit: String(PAGE), offset: String(offset) });
      if(source) qs.set('source', source);
      const { ok, body } = await apiFetch(`/memory/search?${qs}`, { method: 'GET' });
      if(myToken !== token) return;
      moreBtn.disabled = false;
      moreBtn.textContent = 'Load more';

      if(!ok){
        statusEl.textContent = (body && body.detail && typeof body.detail === 'string') ? body.detail : 'Search failed. Try again.';
        return;
      }
      terms = body.terms;
      toolbar.hidden = false;
      sortGroup.hidden = false;
      if(reset) resultsEl.innerHTML = '';

      if(!body.total){
        resultsCard.hidden = true;
        emptyEl.hidden = false;
        emptyText.textContent = source
          ? `Nothing from ${SOURCE_LABELS[source] || source} matches “${query}”. Try all sources, a different word, or ask your twin in chat.`
          : `Nothing matches “${query}”. Try a different word, or ask your twin in chat instead.`;
        statusEl.innerHTML = body.semantic ? '' : semanticNote();
        return;
      }

      resultsCard.hidden = false;
      resultsEl.insertAdjacentHTML('beforeend', body.results.map(resultHTML).join(''));
      offset += body.results.length;
      moreBtn.hidden = !body.has_more;
      statusEl.innerHTML = `${body.total.toLocaleString()} ${body.total === 1 ? 'result' : 'results'} for “${escapeText(query)}”`
        + (body.semantic ? '' : ` · ${semanticNote()}`);
    }

    function semanticNote(){
      return '<span class="search-note">Exact words only right now; meaning-based search is temporarily unavailable.</span>';
    }

    /* ----- rendering ----- */
    function highlight(text){
      const safe = escapeText(text);
      if(!terms.length) return safe;
      const pattern = terms
        .map(t => escapeText(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
        .sort((a, b) => b.length - a.length)
        .join('|');
      return safe.replace(new RegExp(`(${pattern})`, 'gi'), '<mark>$1</mark>');
    }

    function resultHTML(r){
      const at = new Date(r.occurred_at);
      const d = r.details || {};
      const meta = [escapeText(SOURCE_LABELS[r.source] || r.source)];
      meta.push(escapeText(r.all_day
        ? at.toLocaleDateString([], { month: 'long', day: 'numeric', year: 'numeric' })
        : at.toLocaleString([], { month: 'long', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })));
      if(r.source === 'gmail' && d.from) meta.push(`From ${escapeText(senderName(d.from))}`);
      if(r.source === 'gmail' && d.email_subject) meta.push(`📎 Attached to “${escapeText(d.email_subject)}”`);
      if(r.source === 'google_calendar' && d.location) meta.push(escapeText(d.location));
      if((r.source === 'document' || r.source === 'image' || r.source === 'voice_note') && d.filename) meta.push(escapeText(d.filename));
      if(r.source === 'bookmark' && d.folder) meta.push(escapeText(d.folder));

      const badge = r.match === 'semantic'
        ? '<span class="match-badge related" title="Found by meaning — none of your exact words appear">Related</span>'
        : '';
      const open = r.url
        ? `<a class="result-open" href="${escapeText(r.url).replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">Open ↗</a>`
        : '';
      const icon = ACTIVITY_ICONS[r.source] || ACTIVITY_ICONS.connected;
      return `
        <div class="result-item">
          <div class="row-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${icon}</svg></div>
          <div class="row-body">
            <div class="result-head"><h4>${highlight(r.title)}</h4>${badge}</div>
            ${r.body ? `<p class="snippet">${highlight(r.body)}</p>` : ''}
            <div class="result-meta"><span class="src">${meta.join(' · ')}</span>${open}</div>
          </div>
        </div>`;
    }

    function senderName(from){
      const match = from.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>\s*$/);
      return match ? match[1] : from;
    }

    if(query) load(true); else browse(true);
  }

  /* ---------- settings toggles ---------- */
  async function initSettings(){
    const nav = [...document.querySelectorAll('.settings-nav a[data-section]')];
    const sections = [...document.querySelectorAll('[data-settings-section]')];
    if(!nav.length) return;
    const $ = (sel) => document.querySelector(sel);

    /* ----- helpers ----- */
    function showAlert(el, text, kind){
      if(!el) return;
      el.textContent = text || '';
      el.classList.remove('error', 'success');
      el.classList.toggle('show', !!text);
      if(text && kind) el.classList.add(kind);
    }
    function errorText(body, fallback){
      const detail = body && body.detail;
      if(Array.isArray(detail)) return detail.map(d => (d.msg || '').replace(/^Value error, /, '')).join(' ');
      return detail || fallback;
    }
    function setBusy(button, busy, busyText){
      if(!button) return;
      if(busy){ button.dataset.label = button.textContent; button.textContent = busyText; }
      else if(button.dataset.label){ button.textContent = button.dataset.label; }
      button.disabled = busy;
    }

    /* ----- tabs (deep-linkable via #hash) ----- */
    function showSection(name){
      if(!sections.some(s => s.dataset.settingsSection === name)) name = 'profile';
      nav.forEach(l => l.classList.toggle('active', l.dataset.section === name));
      sections.forEach(s => { s.hidden = s.dataset.settingsSection !== name; });
      if(name === 'privacy') loadDataSummary();
    }
    nav.forEach(link => link.addEventListener('click', (e) => {
      e.preventDefault();
      history.replaceState(null, '', `#${link.dataset.section}`);
      showSection(link.dataset.section);
    }));
    window.addEventListener('hashchange', () => showSection(location.hash.slice(1)));

    const me = await apiFetch('/auth/me', { method: 'GET' });
    if(!me.ok) return; // initAuthGuard handles the redirect
    let user = me.body;
    showSection(location.hash.slice(1));

    /* ----- profile ----- */
    const profileForm = $('[data-profile-form]');
    const nameInput = $('#setName');
    const emailInput = $('#setEmail');
    const emailPasswordField = $('[data-email-password]');
    const emailPasswordInput = $('#setEmailPassword');
    const profileAlert = $('[data-profile-alert]');

    function fillProfile(){
      nameInput.value = user.full_name || '';
      emailInput.value = user.email;
      emailPasswordInput.value = '';
      emailPasswordField.hidden = true;
    }
    const emailChanged = () => emailInput.value.trim().toLowerCase() !== user.email;
    emailInput.addEventListener('input', () => { emailPasswordField.hidden = !emailChanged(); });
    fillProfile();

    profileForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      showAlert(profileAlert, '');
      const payload = { full_name: nameInput.value.trim() || null };
      if(emailChanged()){
        if(!emailInput.checkValidity()){ showAlert(profileAlert, 'Enter a valid email address.', 'error'); return; }
        if(!emailPasswordInput.value){ showAlert(profileAlert, 'Enter your current password to change your email.', 'error'); return; }
        payload.email = emailInput.value.trim();
        payload.current_password = emailPasswordInput.value;
      }
      const btn = profileForm.querySelector('button[type="submit"]');
      setBusy(btn, true, 'Saving…');
      const { ok, body } = await apiFetch('/account/profile', { method: 'PATCH', body: JSON.stringify(payload) });
      setBusy(btn, false);
      if(!ok){ showAlert(profileAlert, errorText(body, 'Could not save your profile.'), 'error'); return; }
      user = body;
      fillProfile();
      fillSidebarAccount(user);
      showAlert(profileAlert, 'Profile saved.', 'success');
    });

    /* ----- password ----- */
    const passwordForm = $('[data-password-form]');
    const passwordAlert = $('[data-password-alert]');
    passwordForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      showAlert(passwordAlert, '');
      const current = $('#setCurrentPassword').value;
      const next = $('#setNewPassword').value;
      const confirmed = $('#setConfirmPassword').value;
      if(!current || !next){ showAlert(passwordAlert, 'Fill in every field.', 'error'); return; }
      if(next.length < 8){ showAlert(passwordAlert, 'Your new password needs at least 8 characters.', 'error'); return; }
      if(next !== confirmed){ showAlert(passwordAlert, "The new passwords don't match.", 'error'); return; }
      const btn = passwordForm.querySelector('button[type="submit"]');
      setBusy(btn, true, 'Updating…');
      const { ok, body } = await apiFetch('/account/password', {
        method: 'POST', body: JSON.stringify({ current_password: current, new_password: next })
      });
      setBusy(btn, false);
      if(!ok){ showAlert(passwordAlert, errorText(body, 'Could not update your password.'), 'error'); return; }
      passwordForm.reset();
      showAlert(passwordAlert, 'Password updated.', 'success');
    });

    /* ----- privacy: content toggle ----- */
    const contentToggle = $('[data-pref-content]');
    const privacyAlert = $('[data-privacy-alert]');
    contentToggle.checked = user.allow_content_in_chat;
    contentToggle.addEventListener('change', async () => {
      const wanted = contentToggle.checked;
      contentToggle.disabled = true;
      const { ok, body } = await apiFetch('/account/preferences', {
        method: 'PATCH', body: JSON.stringify({ allow_content_in_chat: wanted })
      });
      contentToggle.disabled = false;
      if(!ok){
        contentToggle.checked = !wanted;
        showAlert(privacyAlert, errorText(body, 'Could not save that setting.'), 'error');
        return;
      }
      user = body;
      showAlert(privacyAlert, wanted
        ? 'Saved. Your twin can read memory content again.'
        : 'Saved. Your twin now sees only titles, sources and dates.', 'success');
    });

    /* ----- privacy: stored data, forget, clear ----- */
    const summaryEl = $('[data-data-summary]');
    let summaryToken = 0;

    async function loadDataSummary(){
      const token = ++summaryToken;
      const { ok, body } = await apiFetch('/account/data', { method: 'GET' });
      if(token !== summaryToken) return;
      if(!ok){ summaryEl.innerHTML = '<div class="hint">Could not load your data summary.</div>'; return; }

      const rows = body.sources.map(s => {
        const label = SOURCE_LABELS[s.source] || s.source;
        const account = UPLOAD_SOURCES.includes(s.source)
          ? '<a href="uploads.html">Uploaded by you</a>'
          : s.connected ? `Connected${s.account_email ? ` as ${escapeText(s.account_email)}` : ''}` : 'Not connected';
        const span = s.oldest ? ` · ${shortDate(s.oldest)} – ${shortDate(s.newest)}` : '';
        return `
          <div class="settings-row">
            <div><div class="lbl">${escapeText(label)}</div>
              <div class="hint">${s.memories.toLocaleString()} ${s.memories === 1 ? 'memory' : 'memories'}${span} · ${account}</div></div>
            ${s.memories ? `<button type="button" class="btn btn-ghost btn-sm" data-forget="${escapeText(s.source)}" data-count="${s.memories}" data-connected="${s.connected}">Forget</button>` : ''}
          </div>`;
      });
      rows.push(`
        <div class="settings-row">
          <div><div class="lbl">Chat history</div>
            <div class="hint">${body.conversations.toLocaleString()} ${body.conversations === 1 ? 'conversation' : 'conversations'} · ${body.messages.toLocaleString()} ${body.messages === 1 ? 'message' : 'messages'}</div></div>
          ${body.conversations ? '<button type="button" class="btn btn-ghost btn-sm" data-clear-chat>Clear</button>' : ''}
        </div>`);
      const empty = !body.sources.length
        ? '<div class="hint" style="margin-bottom:8px;">No sources yet. <a href="connectors.html">Connect one</a> and its memories will show up here.</div>'
        : '';
      summaryEl.innerHTML = empty + rows.join('');
    }

    summaryEl.addEventListener('click', async (e) => {
      const forget = e.target.closest('[data-forget]');
      const clear = e.target.closest('[data-clear-chat]');
      if(forget){
        const source = forget.dataset.forget;
        const label = SOURCE_LABELS[source] || source;
        const count = Number(forget.dataset.count).toLocaleString();
        const resyncNote = UPLOAD_SOURCES.includes(source)
          ? '\n\nThe uploaded files themselves are deleted too.'
          : forget.dataset.connected === 'true'
            ? `\n\n${label} stays connected, so recent items will be re-indexed on the next sync. Disconnect it on the Connectors page to stop that.`
            : '';
        if(!confirm(`Forget all ${count} ${label} memories? This can't be undone.${resyncNote}`)) return;
        setBusy(forget, true, 'Forgetting…');
        const { ok, body } = await apiFetch(`/account/memories?source=${encodeURIComponent(source)}`, { method: 'DELETE' });
        if(!ok){ setBusy(forget, false); showAlert(privacyAlert, errorText(body, 'Could not forget those memories.'), 'error'); return; }
        showAlert(privacyAlert, `Forgot ${body.deleted.toLocaleString()} ${label} ${body.deleted === 1 ? 'memory' : 'memories'}.`, 'success');
        loadDataSummary();
      }else if(clear){
        if(!confirm("Delete all your conversations? This can't be undone.")) return;
        setBusy(clear, true, 'Clearing…');
        const { ok, body } = await apiFetch('/account/conversations', { method: 'DELETE' });
        if(!ok){ setBusy(clear, false); showAlert(privacyAlert, errorText(body, 'Could not clear chat history.'), 'error'); return; }
        showAlert(privacyAlert, `Deleted ${body.deleted.toLocaleString()} ${body.deleted === 1 ? 'conversation' : 'conversations'}.`, 'success');
        loadDataSummary();
      }
    });

    function shortDate(iso){
      const date = new Date(iso);
      const opts = { month: 'short', day: 'numeric' };
      if(date.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
      return date.toLocaleDateString([], opts);
    }

    /* ----- privacy: export ----- */
    const exportBtn = $('[data-export]');
    exportBtn.addEventListener('click', async () => {
      setBusy(exportBtn, true, 'Preparing…');
      try{
        const res = await fetch(`${API_BASE}/account/export`, { credentials: 'include' });
        if(!res.ok) throw new Error(String(res.status));
        const blob = await res.blob();
        const match = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = match ? match[1] : 'digital-twin-export.json';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        showAlert(privacyAlert, 'Your export has downloaded.', 'success');
      }catch(err){
        showAlert(privacyAlert, 'Could not export your data. Try again.', 'error');
      }
      setBusy(exportBtn, false);
    });

    /* ----- twin behavior ----- */
    const TONE_HINTS = {
      balanced: 'Clear, natural answers. The default.',
      concise: 'Leads with the answer, short sentences, bullet points for lists.',
      warm: 'Conversational and empathetic, and points out patterns across your memories.'
    };
    const twinForm = $('[data-twin-form]');
    const toneSelect = $('#setTone');
    const windowSelect = $('#setMemoryWindow');
    const toneHint = $('[data-tone-hint]');
    const twinAlert = $('[data-twin-alert]');
    const updateToneHint = () => { toneHint.textContent = TONE_HINTS[toneSelect.value] || ''; };
    toneSelect.value = user.response_tone;
    windowSelect.value = user.memory_window_days ? String(user.memory_window_days) : '';
    updateToneHint();
    toneSelect.addEventListener('change', updateToneHint);

    twinForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = twinForm.querySelector('button[type="submit"]');
      setBusy(btn, true, 'Saving…');
      const { ok, body } = await apiFetch('/account/preferences', {
        method: 'PATCH',
        body: JSON.stringify({
          response_tone: toneSelect.value,
          memory_window_days: windowSelect.value ? Number(windowSelect.value) : null
        })
      });
      setBusy(btn, false);
      if(!ok){ showAlert(twinAlert, errorText(body, 'Could not save your preferences.'), 'error'); return; }
      user = body;
      showAlert(twinAlert, 'Saved. Your next chat message will use these settings.', 'success');
    });

    /* ----- danger zone ----- */
    const dangerForm = $('[data-danger-form]');
    const dangerTitle = $('[data-danger-title]');
    const dangerSubmit = $('[data-danger-submit]');
    const dangerPassword = $('#setDangerPassword');
    const dangerAlert = $('[data-danger-alert]');
    let dangerMode = null;

    document.querySelectorAll('[data-danger-open]').forEach(btn => btn.addEventListener('click', () => {
      dangerMode = btn.dataset.dangerOpen;
      dangerTitle.textContent = dangerMode === 'delete'
        ? 'Permanently delete your account and everything in it?'
        : 'Permanently delete your twin: all memories, conversations and connected sources?';
      dangerSubmit.textContent = dangerMode === 'delete' ? 'Delete my account' : 'Delete my twin';
      dangerPassword.value = '';
      showAlert(dangerAlert, '');
      dangerForm.hidden = false;
      dangerPassword.focus();
    }));
    $('[data-danger-cancel]').addEventListener('click', () => { dangerForm.hidden = true; dangerMode = null; });

    dangerForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if(!dangerMode) return;
      if(!dangerPassword.value){ showAlert(dangerAlert, 'Enter your password to confirm.', 'error'); return; }
      setBusy(dangerSubmit, true, 'Deleting…');
      const endpoint = dangerMode === 'delete' ? '/account/delete' : '/account/reset';
      const { ok, body } = await apiFetch(endpoint, { method: 'POST', body: JSON.stringify({ password: dangerPassword.value }) });
      setBusy(dangerSubmit, false);
      if(!ok){ showAlert(dangerAlert, errorText(body, 'Something went wrong.'), 'error'); return; }
      if(dangerMode === 'delete'){
        window.location.href = 'index.html';
        return;
      }
      dangerForm.hidden = true;
      dangerMode = null;
      alert('Your twin has been deleted. Connect a source to start building a new one.');
      window.location.href = 'connectors.html';
    });
  }

  /* ---------- reflections ---------- */
  // Grounded pattern notes from /reflections (generated weekly by the worker, or on demand).
  const REFLECTION_CATEGORIES = {
    work: 'Work', relationships: 'Relationships', schedule: 'Schedule', finance: 'Finance',
    learning: 'Learning', personal: 'Personal', inbox: 'Inbox'
  };
  const REFLECTION_TONES = {
    positive: { label: 'Going well', cls: 'twin' },
    neutral: { label: 'Noticed', cls: 'neutral' },
    watch: { label: 'Worth watching', cls: 'human' }
  };
  const ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  const ICON_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

  function reflectionDate(iso){
    const d = new Date(iso);
    const opts = { month: 'short', day: 'numeric' };
    if(d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString([], opts);
  }

  function reflectionTagsHTML(r){
    const tone = REFLECTION_TONES[r.tone] || REFLECTION_TONES.neutral;
    return `<span class="tag ${tone.cls}">${escapeText(tone.label)}</span>`
      + r.tags.map(t => `<span class="tag">${escapeText(t)}</span>`).join('');
  }

  // Opens (or reuses) the chat thread seeded with this reflection.
  async function discussReflection(id, button){
    if(button){ button.disabled = true; }
    const { ok, body } = await apiFetch(`/reflections/${id}/discuss`, { method: 'POST' });
    if(ok){ window.location.href = `chat.html?c=${body.conversation_id}`; return; }
    if(button){ button.disabled = false; }
    alert((body && body.detail) || 'Could not open a chat for this reflection.');
  }

  function initReflections(){
    const list = document.querySelector('[data-refl-list]');
    if(!list) return;
    const meta = document.querySelector('[data-refl-meta]');
    const alertEl = document.querySelector('[data-refl-alert]');
    const generateBtn = document.querySelector('[data-refl-generate]');
    const undoBar = document.querySelector('[data-refl-undo]');
    let state = null;
    let undoTimer = null;
    let lastDismissed = null;

    const showAlert = (text, kind) => {
      alertEl.textContent = text || '';
      alertEl.classList.remove('error', 'success');
      alertEl.classList.toggle('show', !!text);
      if(text && kind) alertEl.classList.add(kind);
    };

    async function load(){
      const { ok, body } = await apiFetch('/reflections/', { method: 'GET' });
      if(!ok){
        list.innerHTML = '<div class="card empty-state"><p>Couldn\'t load your reflections. Try refreshing.</p></div>';
        return;
      }
      state = body;
      render();
    }

    function render(){
      const s = state;
      const enough = s.memory_count >= s.min_memories;
      generateBtn.hidden = !enough;
      const coolingDown = s.can_generate_at && new Date(s.can_generate_at) > new Date();
      generateBtn.disabled = !!coolingDown;
      generateBtn.title = coolingDown ? `Available again at ${new Date(s.can_generate_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : '';
      generateBtn.textContent = s.last_generated_at ? 'Refresh now' : 'Generate reflections';

      meta.textContent = s.last_generated_at
        ? `Last updated ${reflectionDate(s.last_generated_at)} · next update around ${reflectionDate(s.next_scheduled_at)}. Based on your most recent memories.`
        : '';

      if(!enough){
        list.innerHTML = `
          <div class="card empty-state">
            <div class="thread-badge"><div class="core"></div></div>
            <h3>Not enough to reflect on yet</h3>
            <p>Your twin needs at least ${s.min_memories} memories to spot patterns. You have ${s.memory_count}.</p>
            <a href="connectors.html" class="btn btn-primary btn-sm">Connect a source</a>
          </div>`;
        return;
      }
      if(!s.reflections.length){
        list.innerHTML = s.last_generated_at
          ? `<div class="card empty-state">
               <h3>You're all caught up</h3>
               <p>New reflections arrive around ${escapeText(reflectionDate(s.next_scheduled_at))}. You can also refresh now.</p>
             </div>`
          : `<div class="card empty-state">
               <div class="thread-badge"><div class="core"></div></div>
               <h3>No reflections yet</h3>
               <p>Your twin writes a fresh set every week from your recent emails, events and uploads. Generate the first one now.</p>
             </div>`;
        return;
      }
      list.innerHTML = s.reflections.map(cardHTML).join('');
    }

    function cardHTML(r){
      const span = r.evidence.length
        ? (() => {
            const dates = r.evidence.map(e => new Date(e.occurred_at)).sort((a, b) => a - b);
            const first = reflectionDate(dates[0]), last = reflectionDate(dates[dates.length - 1]);
            return first === last ? first : `${first} – ${last}`;
          })()
        : '';
      const evidence = r.evidence.map(e => `
        <li><span class="t">${escapeText(e.title)}</span><span class="m">${escapeText(SOURCE_LABELS[e.source] || e.source)} · ${escapeText(reflectionDate(e.occurred_at))}</span></li>`).join('');
      return `
        <article class="reflection-card" data-reflection-id="${r.id}">
          <span class="eyebrow">Pattern · ${escapeText(REFLECTION_CATEGORIES[r.category] || r.category)}</span>
          <h3 class="refl-headline">${escapeText(r.headline)}</h3>
          <blockquote>${escapeText(r.body)}</blockquote>
          <div class="reflection-tags">${reflectionTagsHTML(r)}</div>
          <details class="refl-evidence">
            <summary>Based on ${r.evidence.length} ${r.evidence.length === 1 ? 'memory' : 'memories'}${span ? ` · ${escapeText(span)}` : ''}</summary>
            <ul>${evidence}</ul>
          </details>
          <div class="reflection-actions">
            <button type="button" class="btn btn-ghost btn-sm refl-talk" data-refl-discuss>${ICON_CHAT}<span>Talk it through</span></button>
            <button type="button" class="icon-btn" data-refl-dismiss title="Dismiss" aria-label="Dismiss">${ICON_CLOSE}</button>
          </div>
        </article>`;
    }

    list.addEventListener('click', async (e) => {
      const card = e.target.closest('[data-reflection-id]');
      if(!card) return;
      const id = card.dataset.reflectionId;
      const talk = e.target.closest('[data-refl-discuss]');
      if(talk){ discussReflection(id, talk); return; }
      if(!e.target.closest('[data-refl-dismiss]')) return;

      card.classList.add('leaving');
      const { ok } = await apiFetch(`/reflections/${id}/dismiss`, { method: 'POST' });
      if(!ok){ card.classList.remove('leaving'); showAlert('Could not dismiss that reflection.', 'error'); return; }
      state.reflections = state.reflections.filter(r => r.id !== id);
      setTimeout(render, 250);
      lastDismissed = id;
      undoBar.hidden = false;
      clearTimeout(undoTimer);
      undoTimer = setTimeout(() => { undoBar.hidden = true; lastDismissed = null; }, 6000);
    });

    undoBar.querySelector('button').addEventListener('click', async () => {
      if(!lastDismissed) return;
      const id = lastDismissed;
      lastDismissed = null;
      undoBar.hidden = true;
      const { ok } = await apiFetch(`/reflections/${id}/restore`, { method: 'POST' });
      if(ok) load(); else showAlert('Could not restore that reflection.', 'error');
    });

    generateBtn.addEventListener('click', async () => {
      showAlert('');
      generateBtn.disabled = true;
      const label = generateBtn.textContent;
      generateBtn.textContent = 'Generating…';
      list.insertAdjacentHTML('afterbegin', '<div class="card refl-generating" data-refl-generating><span class="search-spinner"></span>Reading your recent memories…</div>');
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      const { ok, body } = await apiFetch('/reflections/generate', { method: 'POST', body: JSON.stringify({ timezone }) });
      document.querySelector('[data-refl-generating]')?.remove();
      generateBtn.textContent = label;
      if(!ok){
        generateBtn.disabled = false;
        showAlert((body && typeof body.detail === 'string' && body.detail) || 'Could not generate reflections.', 'error');
        return;
      }
      state = body;
      render();
      showAlert('Fresh reflections are ready.', 'success');
    });

    load();
  }

  // Dashboard "Latest reflection" card.
  async function initLatestReflection(){
    const box = document.querySelector('[data-latest-reflection]');
    if(!box) return;
    const { ok, body } = await apiFetch('/reflections/', { method: 'GET' });
    if(!ok){ box.innerHTML = '<p class="hint">Couldn\'t load reflections.</p>'; return; }
    const r = body.reflections[0];
    if(!r){
      const enough = body.memory_count >= body.min_memories;
      box.innerHTML = `
        <div class="empty-state" style="padding:30px 10px;">
          <h3>No reflections yet</h3>
          <p>${enough
            ? 'Your twin writes a fresh set of patterns every week. You can generate the first one now.'
            : `Your twin needs at least ${body.min_memories} memories to spot patterns.`}</p>
          <a href="${enough ? 'reflection.html' : 'connectors.html'}" class="btn btn-ghost btn-sm">${enough ? 'Open reflections' : 'Connect a source'}</a>
        </div>`;
      return;
    }
    box.innerHTML = `
      <span class="eyebrow" style="margin-bottom:12px;">Pattern · ${escapeText(REFLECTION_CATEGORIES[r.category] || r.category)}</span>
      <h4 class="refl-headline">${escapeText(r.headline)}</h4>
      <blockquote class="latest-refl-quote">${escapeText(r.body)}</blockquote>
      <div class="reflection-tags">${reflectionTagsHTML(r)}</div>
      <div style="margin-top:20px;">
        <button type="button" class="btn btn-ghost btn-sm" data-latest-discuss>Talk it through</button>
      </div>`;
    box.querySelector('[data-latest-discuss]').addEventListener('click', (e) => discussReflection(r.id, e.currentTarget));
  }

  /* ---------- mobile rail toggle (if hamburger present) ---------- */
  function initMobileNav(){
    const toggle = document.querySelector('[data-nav-toggle]');
    const rail = document.querySelector('.rail, .site-header');
    if(!toggle || !rail) return;
    toggle.addEventListener('click', () => rail.classList.toggle('open'));
  }

  return {
    initAuthForm,
    initAuthGuard,
    initLogout,
    initConnectors,
    initBuildingSequence,
    initChat,
    initDashboard,
    initTimeline,
    initUploads,
    initFilterChips,
    initSearchPage,
    initSettings,
    initReflections,
    initLatestReflection,
    initMobileNav
  };
})();
