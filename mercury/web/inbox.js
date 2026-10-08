// Inbox: every conversation from every sending mailbox, triaged and answered
// in one place. Three panes: the list, the thread with its composer, and the
// contact's context (company, stage, reminder, notes).
//
// Read state, snoozes, notes and reminders are local to Mercury: nothing here
// changes flags in Gmail or on the IMAP server. A reply is an outbox row, so
// it goes through review, revisions and the Sender's gates like any email
// (see mercury/control/inbox.py). Every draft change names the revision it
// was made on; a stale save never loses what was typed.

const IBX_PAGE = 25;
const IBX_SEGMENTS = [
  ['needs', 'Needs you', 'needs_you'], ['unread', 'Unread', 'unread'],
  ['snoozed', 'Snoozed', 'snoozed'], ['all', 'All', ''],
];
const IBX_STAGES = [
  ['initial_outreach', 'Initial outreach', 'paper-plane-tilt'],
  ['engaged', 'Engaged', 'chats-circle'],
  ['qualifying', 'Qualifying', 'list-checks'],
  ['presenting', 'Presenting', 'presentation-chart'],
  ['negotiating', 'Negotiating', 'handshake'],
  ['closing', 'Closing', 'flag-checkered'],
  ['closed_won', 'Closed won', 'trophy'],
  ['closed_lost', 'Closed lost', 'x-circle'],
];
const IBX_INTENT = {
  interested: ['Interested', 'good'], question: ['Question', 'note'],
  objection: ['Objection', 'waiting'], not_interested: ['Not interested', 'bad'],
  unsubscribe: ['Opted out', 'bad'], escalate: ['Escalated', 'bad'],
  ooo: ['Out of office', 'idle'], wrong_person: ['Wrong person', 'idle'],
};
const IBX_FILTERS = [['intent', 'Intent'], ['mailbox', 'Mailbox'], ['stage', 'Stage']];

const _ib = {
  mounted: false, seg: 'needs', q: '', filters: { intent: '', mailbox: '', stage: '' }, offset: 0,
  list: null, sel: null, thread: null, checked: new Set(),
  listSeq: 0, threadSeq: 0, autoRead: null,
  comp: null,          // {conv, draftId, revision, base, text, notice, busy}
  noteEdit: null,      // {id: note id or 'new', text}
  ctxOpen: false,      // the context pane folded into the thread (narrow screens)
  earlierOpen: false, phoneThread: false, menu: null, searchTimer: null,
};

const ibxEl = id => document.getElementById(id);
const ibxArg = s => escHtml(JSON.stringify(String(s ?? '')));     // safe inline-handler string
const ibxPhone = () => window.matchMedia('(max-width: 700px)').matches;
const ibxStage = key => IBX_STAGES.find(s => s[0] === key) || [key, statusMeta(key).label, 'circle-dashed'];
const ibxFirst = t => ((t.prospect && t.prospect.name) || '').split(' ')[0] || 'them';

// ── Time ──
// The database stores naive UTC; everything here is shown in local time.

function ibxClock(d) { return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }); }
function ibxDay(d) {
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric',
    year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' });
}
function ibxWhen(s) { const d = s instanceof Date ? s : parseUTC(s); return d ? ibxDay(d) + ', ' + ibxClock(d) : ''; }
function ibxListTime(s) { const d = parseUTC(s); return !d ? '' : dayDiff(d) === 0 ? ibxClock(d) : ibxDay(d); }
function ibxSoon(s) {
  const d = s instanceof Date ? s : parseUTC(s);
  if (!d) return '';
  const n = dayDiff(d);
  return (n === 0 ? 'today ' : n === 1 ? 'tomorrow ' : n === -1 ? 'yesterday ' : ibxDay(d) + ', ') + ibxClock(d);
}

// Quick times for snooze, reminders and scheduled replies.
function ibxPresets() {
  const now = new Date(), out = [];
  const later = new Date(now);
  later.setMinutes(0, 0, 0);
  later.setHours(later.getHours() + 3);
  if (dayDiff(later) === 0 && later.getHours() <= 20) out.push(['Later today', later]);
  const tomorrow = addDays(startOfDay(now), 1);
  tomorrow.setHours(9);
  out.push(['Tomorrow 9:00', tomorrow]);
  const monday = startOfDay(now);
  monday.setDate(monday.getDate() + (((8 - monday.getDay()) % 7) || 7));
  monday.setHours(9);
  out.push(['Next Monday 9:00', monday]);
  return out;
}
const ibxPresetHint = d => dayDiff(d) === 0 ? ibxClock(d)
  : d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

// ── Requests ──

async function ibxSend(method, path, body) {
  try {
    const r = await fetch(path, {
      method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch { /* empty body */ }
    const ok = r.ok && !(data && data.success === false);
    return { ok, status: r.status, data, code: (data && data.code) || '',
             error: ok ? '' : ((data && (data.message || data.error)) || 'Request failed (' + r.status + ').') };
  } catch {
    return { ok: false, status: 0, data: null, code: 'offline', error: 'Can\'t reach the dashboard server.' };
  }
}
const ibxConvPath = id => '/api/inbox/conversations/' + encodeURIComponent(id);

// A toast with a title and a line of detail (bulk results say what happened
// to each conversation).
function ibxToast(title, detail, tone) {
  document.querySelectorAll('.ibx-toast').forEach(t => t.remove());
  const t = document.createElement('div');
  t.className = 'ibx-toast';
  t.setAttribute('role', 'status');
  t.innerHTML = '<span class="badge t-' + (tone || 'good') + '">' +
    icon(TONE_ICON[tone || 'good']) + '</span><div><b>' + escHtml(title) + '</b>' +
    (detail ? '<p>' + escHtml(detail) + '</p>' : '') + '</div>';
  const host = ibxPhone() || !ibxEl('ibx-list-pane') ? document.body : ibxEl('ibx-list-pane');
  host.appendChild(t);
  setTimeout(() => t.remove(), detail ? 6000 : 3200);
}

// ── Entry points (called from app.js) ──

async function loadInbox() {
  ibxMount();
  if (ibxPhone()) ibxPhoneThread(!!(_ib.wantThread && _ib.sel));
  _ib.wantThread = false;
  await ibxLoadList();
  if (_ib.sel) ibxLoadThread(_ib.sel);
  else if (!ibxPhone() && _ib.list && _ib.list.items.length) ibxSelect(_ib.list.items[0].id);
  else ibxRenderThread();
}

// The 15-second refresh. Never re-renders a composition or a note in progress.
function inboxRefresh() {
  if (_ib.menu || promptOpen() || modalOpen()) return;
  ibxLoadList(true);
  if (_ib.sel) ibxLoadThread(_ib.sel, true);
}

function inboxLeave() {
  ibxCloseMenu();
  ibxPhoneThread(false);
}

// Open one conversation from anywhere (Today, a contact drawer).
async function inboxOpen(id) {
  if (!id) { goTab('inbox'); return; }
  _ib.sel = id;
  _ib.seg = 'all';
  _ib.wantThread = true;
  _ib.thread = null;
  _ib.earlierOpen = false;
  _ib.noteEdit = null;
  _ib.autoRead = null;
  if (currentTab !== 'inbox') showTab('inbox');
  else loadInbox();
}

// Today shows each due reminder, and each reply that could not be handled,
// on its own row with a way straight into the inbox.
function inboxTodayItems(items) {
  const out = [];
  for (const it of items) {
    if (it.key === 'reminders' && (it.reminders || []).length) {
      for (const r of it.reminders) {
        const due = parseUTC(r.due_at);
        const who = r.name || r.email || 'this contact';
        out.push({
          key: 'reminder:' + r.id, tone: 'warn',
          stateHtml: icon('bell') + 'Reminder due ' +
            escHtml(due ? ibxSoon(due).replace(/^(today|tomorrow|yesterday) /, '$1, ') : 'now'),
          title: 'Follow up with ' + who + (r.company ? ' at ' + r.company : ''),
          detail: (r.note ? 'Your note: ' + r.note.replace(/[.\s]*$/, '') + '. ' : '') +
            'Nothing is sent until you write and approve a reply.',
          action: 'Open conversation', onclick: 'inboxOpen(' + JSON.stringify(r.conversation_id) + ')',
        });
      }
      const more = (it.conversation_ids || []).length - it.reminders.length;
      if (more > 0) out.push({ ...it, title: more + ' more reminder' + (more === 1 ? ' is' : 's are') + ' due',
        onclick: 'ibxGoSegment("needs")' });
    } else if (it.key === 'inbound-failed' && (it.messages || []).length === 1) {
      const m = it.messages[0];
      out.push({ ...it, title: 'A reply from ' + (m.from_email || 'a contact') + ' could not be processed',
        detail: (m.subject ? '“' + m.subject + '”. ' : '') + 'Mercury kept it but stopped retrying. ' +
          'Read it in the inbox and answer by hand if it needs it.',
        action: m.conversation_id ? 'Open conversation' : 'Find in the inbox',
        onclick: m.conversation_id ? 'inboxOpen(' + JSON.stringify(m.conversation_id) + ')'
          : 'ibxSearchFor(' + JSON.stringify(m.from_email || '') + ')' });
    } else {
      out.push(it.tab === 'inbox' ? { ...it, onclick: 'ibxGoSegment("all")' } : it);
    }
  }
  return out;
}

function ibxGoSegment(seg) { _ib.seg = seg; _ib.offset = 0; goTab('inbox'); }
function ibxSearchFor(q) { _ib.q = q; _ib.seg = 'all'; _ib.offset = 0; _ib.sel = null; goTab('inbox'); }

// ── Mount: the static chrome, built once ──

function ibxMount() {
  if (_ib.mounted) { ibxSyncChrome(); return; }
  _ib.mounted = true;
  ibxEl('ibx').innerHTML =
    '<section class="ibx-list" id="ibx-list-pane" aria-label="Conversations">' +
      '<div class="ibx-list-head">' +
        '<div class="ibx-title-row"><h2>Inbox</h2><span class="num" id="ibx-total"></span></div>' +
        '<label class="search-field ibx-search">' + icon('magnifying-glass') +
          '<input id="ibx-q" type="search" placeholder="Search people, companies, messages" ' +
          'aria-label="Search conversations" autocomplete="off" oninput="ibxSearch(this.value)" ' +
          'onsearch="ibxSearch(this.value)">' +
          '<kbd aria-hidden="true">/</kbd></label>' +
        '<div class="segmented ibx-seg" role="tablist" aria-label="Show" id="ibx-seg"></div>' +
        '<div class="ibx-filters" id="ibx-filters"></div>' +
      '</div>' +
      '<div class="ibx-bulk" id="ibx-bulk" hidden></div>' +
      '<div class="ibx-items" id="ibx-items" role="list"><div class="loading-note">Loading conversations…</div></div>' +
      '<div class="ibx-foot" id="ibx-foot"></div>' +
    '</section>' +
    '<section class="ibx-thread" id="ibx-thread" aria-label="Conversation"></section>' +
    '<aside class="ibx-ctx" id="ibx-ctx" aria-label="Contact"></aside>';
  ibxSyncChrome();
}

function ibxSyncChrome() {
  const q = ibxEl('ibx-q');
  if (q && document.activeElement !== q) q.value = _ib.q;
}

// ── The list ──

function ibxQuery() {
  const p = new URLSearchParams({ limit: IBX_PAGE, offset: _ib.offset });
  if (_ib.q.trim()) p.set('q', _ib.q.trim());
  for (const [k] of IBX_FILTERS) if (_ib.filters[k]) p.set(k, _ib.filters[k]);
  if (_ib.seg === 'needs') p.set('needs_you', 'true');
  if (_ib.seg === 'unread') p.set('read', 'unread');
  if (_ib.seg === 'snoozed') p.set('snoozed', 'only');
  if (_ib.seg === 'all') p.set('snoozed', 'include');
  return p;
}

async function ibxLoadList(quiet) {
  const seq = ++_ib.listSeq;
  const res = await getJSON('/api/inbox/conversations?' + ibxQuery());
  if (seq !== _ib.listSeq) return;
  if (!res.ok) {
    if (!quiet) ibxEl('ibx-items').innerHTML = unavailableState(res, 'chat-circle-text', 'The inbox');
    return;
  }
  _ib.list = res.data;
  const live = new Set(res.data.items.map(i => i.id));
  // A selection only holds rows you can see: changing the view, page or
  // search drops the rest, so a bulk action never touches a hidden row.
  if (!quiet) for (const id of [..._ib.checked]) if (!live.has(id)) _ib.checked.delete(id);
  navCount('nav-inbox', (res.data.segments || {}).needs_you || 0);
  ibxRenderList();
}

function ibxRenderList() {
  const d = _ib.list;
  if (!d) return;
  const seg = d.segments || {};
  ibxEl('ibx-total').textContent = Number(seg.all ?? d.total).toLocaleString('en-US') +
    ((seg.all ?? d.total) === 1 ? ' conversation' : ' conversations');
  ibxEl('ibx-seg').innerHTML = IBX_SEGMENTS.map(([k, label, key]) => {
    const n = key ? Number(seg[key] || 0) : null;
    return '<button role="tab" aria-selected="' + (_ib.seg === k) + '" class="' + (_ib.seg === k ? 'on' : '') +
      '" onclick="ibxSeg(\'' + k + '\')">' + label +
      (n !== null ? '<span class="num' + (k === 'needs' && n ? ' hot' : '') + '">' + n + '</span>' : '') + '</button>';
  }).join('');
  ibxEl('ibx-filters').innerHTML = IBX_FILTERS.map(([k, label]) => {
    const v = _ib.filters[k];
    const shown = !v ? 'Any' : k === 'intent' ? (IBX_INTENT[v] || [statusMeta(v).label])[0]
      : k === 'stage' ? ibxStage(v)[1] : v;
    return '<button class="ibx-filter' + (v ? ' set' : '') + '" aria-haspopup="menu" ' +
      'onclick="ibxFilterMenu(\'' + k + '\', this)"><span>' + label + '</span><b' + (k === 'mailbox' && v ? ' class="mono"' : '') +
      '>' + escHtml(shown) + '</b>' + icon('caret-down') + '</button>';
  }).join('');

  const el = ibxEl('ibx-items');
  if (!d.items.length) {
    const filtered = _ib.q || Object.values(_ib.filters).some(Boolean);
    const [g, title, copy] = filtered ? ['magnifying-glass', 'No conversations match',
      'Try other words, or clear the filters.']
      : { needs: ['check-circle', 'Nothing needs you', 'No escalations, drafts to review or reminders due. Mercury flags them here when they come up.'],
          unread: ['envelope-simple-open', 'All read', 'New replies show up here until you open them.'],
          snoozed: ['moon', 'Nothing snoozed', 'Snooze a conversation to hide it until a time you choose. It comes back early if they write again.'],
          all: ['chat-circle-text', 'No conversations yet', 'When someone replies to Mercury, the thread shows up here with every message from every mailbox.'] }[_ib.seg];
    el.innerHTML = emptyState(g, title, copy) +
      (filtered ? '<div class="ibx-empty-act"><button class="btn btn-secondary btn-sm" onclick="ibxClearFilters()">Clear filters</button></div>' : '');
  } else {
    el.innerHTML = d.items.map(ibxItem).join('');
  }
  const from = d.total ? d.offset + 1 : 0, to = d.offset + d.items.length;
  ibxEl('ibx-foot').innerHTML =
    '<span class="num">' + from + '–' + to + ' of ' + Number(d.total).toLocaleString('en-US') + '</span>' +
    '<span class="ibx-keys" aria-hidden="true"><kbd>J</kbd><kbd>K</kbd> move</span>' +
    '<button class="btn-square ibx-sm" onclick="ibxPage(-1)" title="Previous page" aria-label="Previous page"' +
      (d.offset > 0 ? '' : ' disabled') + '>' + icon('caret-left') + '</button>' +
    '<button class="btn-square ibx-sm" onclick="ibxPage(1)" title="Next page" aria-label="Next page"' +
      (d.next_offset != null ? '' : ' disabled') + '>' + icon('caret-right') + '</button>';
  ibxRenderBulk();
}

function ibxItem(it) {
  const name = it.prospect.name || it.prospect.email || 'Unknown contact';
  const intent = IBX_INTENT[it.intent];
  const cls = ['ibx-item', it.unread ? 'unread' : '', it.id === _ib.sel ? 'sel' : '',
               _ib.checked.has(it.id) ? 'checked' : ''].filter(Boolean).join(' ');
  return '<div class="' + cls + '" role="listitem" tabindex="0" data-id="' + escHtml(it.id) + '" ' +
      'onclick="ibxSelect(' + ibxArg(it.id) + ')" onkeydown="if(event.key===\'Enter\')ibxSelect(' + ibxArg(it.id) + ')">' +
    '<input type="checkbox" class="ibx-cb" aria-label="Select ' + escHtml(name) + '"' +
      (_ib.checked.has(it.id) ? ' checked' : '') +
      ' onclick="event.stopPropagation()" onchange="ibxCheck(' + ibxArg(it.id) + ', this.checked)">' +
    '<div class="ibx-item-main">' +
      '<div class="ibx-item-top"><span class="ibx-name">' + escHtml(name) + '</span>' +
        '<span class="ibx-co">' + escHtml(it.company.name || '') + '</span>' +
        '<span class="ibx-time num">' + escHtml(ibxListTime(it.last_activity_at)) + '</span></div>' +
      (it.subject ? '<div class="ibx-subj">' + escHtml(it.subject) + '</div>' : '') +
      (it.snippet ? '<div class="ibx-snip">' + escHtml(it.snippet) + '</div>' : '') +
      '<div class="ibx-meta">' + (intent ? toneBadge(intent[1], intent[0]) : it.intent ? badge(it.intent) : '') +
        ibxFlag(it) + '</div>' +
    '</div></div>';
}

// One flag per row, the most pressing first.
function ibxFlag(it) {
  const f = (g, text, cls) => '<span class="ibx-flag' + (cls ? ' ' + cls : '') + '">' + icon(g) + escHtml(text) + '</span>';
  if (it.needs_human && it.intent !== 'escalate') return f('warning', 'Escalated to you', 'bad');
  if (it.needs_human) return f('envelope-simple', 'Answer from your own mail');
  const dr = it.draft;
  if (dr && dr.status === 'pending_review') return f('pencil-simple', 'Draft to review');
  if (dr && dr.status === 'blocked') return f('warning', 'Reply blocked', 'bad');
  if (dr && dr.status === 'sending') return f('paper-plane-tilt', 'Reply sending');
  if (dr && dr.status === 'approved') return f('paper-plane-tilt', 'Reply sends ' + ibxSoon(dr.send_at || new Date()));
  if (it.reminder === 'due') return f('bell', 'Reminder due', 'due');
  if (it.reminder === 'scheduled' && it.next_reminder_at) return f('bell', 'Remind ' + ibxWhen(it.next_reminder_at));
  if (it.snoozed && it.snoozed_until) return f('moon', 'Snoozed until ' + ibxWhen(it.snoozed_until));
  return '';
}

function ibxSeg(k) {
  if (_ib.seg === k) return;
  _ib.seg = k; _ib.offset = 0;
  ibxLoadList();
}

function ibxSearch(v) {
  clearTimeout(_ib.searchTimer);
  _ib.searchTimer = setTimeout(() => { _ib.q = v; _ib.offset = 0; ibxLoadList(); }, 220);
}

function ibxClearFilters() {
  _ib.q = ''; _ib.filters = { intent: '', mailbox: '', stage: '' }; _ib.offset = 0;
  ibxSyncChrome();
  ibxLoadList();
}

function ibxPage(dir) {
  const d = _ib.list;
  if (!d) return;
  const next = Math.max(0, d.offset + dir * IBX_PAGE);
  if (next === d.offset || (dir > 0 && d.next_offset == null)) return;
  _ib.offset = next;
  ibxLoadList();
  ibxEl('ibx-items').scrollTop = 0;
}

function ibxFilterMenu(kind, anchor) {
  const facet = ((_ib.list || {}).facets || {})[kind] || [];
  const label = v => kind === 'intent' ? (IBX_INTENT[v] || [statusMeta(v).label])[0]
    : kind === 'stage' ? ibxStage(v)[1] : v;
  const entries = [{ label: 'Any', checked: !_ib.filters[kind], on: () => ibxSetFilter(kind, '') }];
  const values = kind === 'stage'
    ? IBX_STAGES.map(s => facet.find(f => f.value === s[0]) || { value: s[0], count: 0 })
    : facet.filter(f => f.value);
  for (const f of values) {
    entries.push({ label: label(f.value), mono: kind === 'mailbox', hint: String(f.count),
      icon: kind === 'stage' ? ibxStage(f.value)[2] : '', checked: _ib.filters[kind] === f.value,
      on: () => ibxSetFilter(kind, f.value) });
  }
  if (entries.length === 1) entries.push({ text: 'Nothing to filter by yet.' });
  ibxMenu(anchor, entries, { label: IBX_FILTERS.find(f => f[0] === kind)[1] });
}

function ibxSetFilter(kind, value) {
  _ib.filters[kind] = value; _ib.offset = 0;
  ibxLoadList();
}

// ── Bulk ──

function ibxCheck(id, on) {
  if (on) _ib.checked.add(id); else _ib.checked.delete(id);
  const row = document.querySelector('.ibx-item[data-id="' + CSS.escape(id) + '"]');
  if (row) row.classList.toggle('checked', on);
  ibxRenderBulk();
}

function ibxClearChecks() {
  _ib.checked.clear();
  document.querySelectorAll('.ibx-item.checked').forEach(r => {
    r.classList.remove('checked');
    const cb = r.querySelector('.ibx-cb');
    if (cb) cb.checked = false;
  });
  ibxRenderBulk();
}

function ibxRenderBulk() {
  const el = ibxEl('ibx-bulk');
  const n = _ib.checked.size;
  el.hidden = !n;
  if (!n) { el.innerHTML = ''; return; }
  el.innerHTML =
    '<div class="ibx-bulk-row"><label class="ibx-bulk-count"><input type="checkbox" checked ' +
      'onchange="ibxClearChecks()" aria-label="Clear the selection">' + n + ' selected</label>' +
      '<button class="link-btn ibx-link" onclick="ibxClearChecks()">Clear</button></div>' +
    '<div class="ibx-bulk-row"><button class="btn btn-secondary btn-sm" onclick="ibxBulk(\'read\')">' +
      icon('envelope-simple-open') + 'Mark read</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="ibxBulkSnooze(this)">' + icon('moon') + 'Snooze</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="ibxBulk(\'exclude\')">' + icon('prohibit') + 'Exclude</button></div>';
}

function ibxBulkSnooze(anchor) {
  ibxTimeMenu(anchor, 'Snooze until', when => ibxBulk('snooze', when));
}

function ibxNameOf(id) {
  const it = ((_ib.list || {}).items || []).find(i => i.id === id);
  return it ? (it.prospect.name || it.prospect.email || 'One contact') : 'One conversation';
}

async function ibxBulk(action, when) {
  const ids = [..._ib.checked];
  if (!ids.length) return;
  const body = { action, conversation_ids: ids };
  if (action === 'snooze') body.until = when.toISOString();
  if (action === 'exclude') {
    const ok = await confirmModal({
      title: 'Exclude ' + ids.length + (ids.length === 1 ? ' contact?' : ' contacts?'),
      copy: 'Mercury stops every email to these addresses, including anything already queued. ' +
        'You can lift an exclusion later from the Exclusions tab.',
      ok: 'Exclude',
    });
    if (!ok) return;
    body.confirm = true;
  }
  const res = await ibxSend('POST', '/api/inbox/bulk', body);
  if (!res.ok) { showToast('Could not ' + action + ': ' + res.error, 'error'); return; }
  const d = res.data, results = d.results || [];
  const done = results.filter(r => r.ok && r.changed !== false);
  const same = results.filter(r => r.ok && r.changed === false);
  const failed = results.filter(r => !r.ok);
  const verb = { read: 'Marked read', snooze: 'Snoozed', exclude: 'Excluded' }[action];
  const tail = action === 'snooze' ? ' until ' + ibxWhen(when) : '';
  const notes = [];
  for (const r of same) {
    notes.push(ibxNameOf(r.id) + ' was already ' + (action === 'snooze' ? 'snoozed until then' : 'read') +
      ', so nothing changed for them.');
  }
  for (const r of failed) notes.push(ibxNameOf(r.id) + ': ' + r.message);
  ibxToast(verb + ' ' + done.length + ' of ' + results.length + tail,
    notes.join(' '), failed.length ? 'waiting' : 'good');
  ibxClearChecks();
  await ibxLoadList(true);
  if (_ib.sel && ids.includes(_ib.sel)) ibxLoadThread(_ib.sel, true);
}

// ── Selecting and loading a thread ──

async function ibxSelect(id) {
  if (!id) return;
  if (_ib.sel !== id) {
    _ib.sel = id;
    _ib.thread = null;
    _ib.earlierOpen = false;
    _ib.noteEdit = null;
    _ib.autoRead = null;
    document.querySelectorAll('.ibx-item').forEach(r => r.classList.toggle('sel', r.dataset.id === id));
    ibxEl('ibx-thread').dataset.conv = '';
    ibxEl('ibx-thread').innerHTML = '<div class="loading-note ibx-pad">Loading the conversation…</div>';
  }
  if (ibxPhone()) ibxPhoneThread(true);
  await ibxLoadThread(id);
}

async function ibxLoadThread(id, quiet) {
  const seq = ++_ib.threadSeq;
  const res = await getJSON(ibxConvPath(id));
  if (seq !== _ib.threadSeq || _ib.sel !== id) return;
  if (!res.ok) {
    if (!quiet) {
      ibxEl('ibx-thread').innerHTML = res.status === 404
        ? emptyState('chat-circle-text', 'That conversation is gone', 'It may have been removed. Pick another one on the left.')
        : unavailableState(res, 'chat-circle-text', 'This conversation');
      ibxEl('ibx-ctx').innerHTML = '';
    }
    return;
  }
  _ib.thread = res.data;
  ibxSyncComposer(res.data);
  ibxRenderThread(quiet);
  // Opening an unread conversation reads it (once: "Mark unread" sticks).
  if (res.data.local.unread && _ib.autoRead !== id) {
    _ib.autoRead = id;
    const r = await ibxSend('POST', ibxConvPath(id) + '/read');
    if (r.ok && _ib.thread && _ib.thread.conversation.id === id) {
      _ib.thread.local.unread = false;
      ibxRenderHead();
      ibxMarkRow(id, false);
    }
  } else if (!res.data.local.unread) {
    _ib.autoRead = id;
  }
}

function ibxMarkRow(id, unread) {
  const it = ((_ib.list || {}).items || []).find(i => i.id === id);
  if (it) it.unread = unread;
  const row = document.querySelector('.ibx-item[data-id="' + CSS.escape(id) + '"]');
  if (row) row.classList.toggle('unread', unread);
}

// The composer's working copy. A refresh never overwrites typed text: only
// an untouched composer follows the server's newest revision.
function ibxEditable(t) {
  return (t.drafts || []).find(d => ['pending_review', 'approved', 'blocked', 'sending'].includes(d.status)) || null;
}

function ibxSyncComposer(t) {
  const d = ibxEditable(t);
  const c = _ib.comp;
  const server = { draftId: d ? d.id : '', revision: d ? d.revision : 0, base: d ? d.body : '',
                   subject: d ? d.subject : '' };
  if (c && c.conv === t.conversation.id) {
    if (c.text !== c.base) return;             // unsaved: keep it, a save says if it is stale
    Object.assign(c, server, { text: server.base });
    return;
  }
  _ib.comp = { conv: t.conversation.id, ...server, text: server.base, notice: null, busy: '' };
}

function ibxRenderThread(quiet) {
  const t = _ib.thread;
  const pane = ibxEl('ibx-thread');
  if (!t) {
    pane.innerHTML = emptyState('chat-circle-text', 'Pick a conversation',
      'Choose one on the left to read it and answer. <kbd>J</kbd> and <kbd>K</kbd> move through the list.');
    ibxEl('ibx-ctx').innerHTML = '';
    return;
  }
  const fresh = pane.dataset.conv !== t.conversation.id || !ibxEl('ibx-head');
  const scroller = pane.querySelector('.ibx-scroll');
  const atBottom = scroller && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
  const keepTop = scroller ? scroller.scrollTop : 0;
  if (fresh) {
    pane.dataset.conv = t.conversation.id;
    pane.innerHTML =
      '<header class="ibx-head" id="ibx-head"></header>' +
      '<div class="ibx-ctx-fold" id="ibx-ctx-fold"></div>' +
      '<div class="ibx-scroll" id="ibx-scroll"><div class="ibx-msgs" id="ibx-msgs"></div></div>' +
      '<div class="ibx-composer" id="ibx-composer"></div>';
  }
  // Someone typing keeps the composer exactly as it is; anything else
  // (an action, a newer revision) redraws it from the working copy.
  if (!(quiet && document.activeElement === ibxEl('ibx-body'))) ibxRenderComposerKeepFocus();
  ibxRenderHead();
  ibxEl('ibx-msgs').innerHTML = ibxMessages(t);
  ibxRenderCtx();
  const sc = ibxEl('ibx-scroll');
  if (fresh || atBottom) sc.scrollTop = sc.scrollHeight; else sc.scrollTop = keepTop;
}

// ── Thread head and its toolbar ──

function ibxStageBadge(t) {
  const c = t.conversation;
  if (c.stage === 'closed_lost') return toneBadge('idle', t.prospect.status === 'opted_out' || c.intent === 'unsubscribe' ? 'Closed, opted out' : 'Closed lost');
  if (c.stage === 'closed_won') return toneBadge('good', 'Closed won');
  return toneBadge('active', ibxStage(c.stage)[1]);
}

function ibxRenderHead() {
  const t = _ib.thread, el = ibxEl('ibx-head');
  if (!t || !el) return;
  const c = t.conversation, p = t.prospect;
  const intent = IBX_INTENT[c.intent];
  const name = p.name || p.email || 'Unknown contact';
  const sub = [t.company.name, ibxPhone() ? t.company.location : ''].filter(Boolean).join(' · ');
  el.innerHTML =
    '<div class="ibx-head-row">' +
      '<div class="ibx-who"><h3>' + escHtml(name) + '</h3>' +
        (sub ? '<span class="ibx-who-co">' + escHtml(sub) + '</span>' : '') + '</div>' +
      '<div class="ibx-tools">' + ibxToolbar(t) + '</div>' +
    '</div>' +
    '<div class="ibx-head-meta">' + (intent ? toneBadge(intent[1], intent[0]) : '') + ibxStageBadge(t) +
      (t.mailbox ? '<span class="ibx-to">' + icon('envelope-simple') + 'to <span class="mono">' + escHtml(t.mailbox) + '</span></span>' : '') +
    '</div>' +
    (t.local.snoozed ? '<div class="ibx-head-note">' + icon('moon') + 'Snoozed until ' + escHtml(ibxWhen(t.local.snoozed_until)) +
      '. It comes back early if they write again. <button class="link-btn ibx-link" onclick="ibxUnsnooze()">Unsnooze</button></div>' : '');
  const top = document.getElementById('ibx-top');
  if (top) top.innerHTML = ibxToolbar(t, true);
}

function ibxToolbar(t, phone) {
  const open = (t.reminders || []).find(r => !r.done_at);
  const unread = t.local.unread;
  const b = (cls, g, label, on, extra) => '<button class="btn-square ' + cls + (extra || '') + '" title="' + label +
    '" aria-label="' + label + '" aria-haspopup="' + (on.startsWith('ibxMore') || on.includes('Menu') ? 'menu' : 'false') +
    '" onclick="' + on + '">' + icon(g) + '</button>';
  return b('tint-amber', 'bell', open ? 'Reminder set for ' + ibxWhen(open.due_at) : 'Remind me', 'ibxRemindMenu(this)', open ? ' on' : '') +
    b('tint-blue', 'moon', t.local.snoozed ? 'Snoozed until ' + ibxWhen(t.local.snoozed_until) : 'Snooze', 'ibxSnoozeMenu(this)', t.local.snoozed ? ' on' : '') +
    (phone ? '' : b('tint-violet ibx-read-btn', unread ? 'envelope-simple-open' : 'envelope-simple', unread ? 'Mark read' : 'Mark unread', 'ibxToggleRead()')) +
    b('', 'dots-three', 'More', 'ibxMoreMenu(this)');
}

async function ibxToggleRead() {
  const t = _ib.thread;
  if (!t) return;
  const toRead = t.local.unread;
  const id = t.conversation.id;
  _ib.autoRead = id;
  const res = await ibxSend('POST', ibxConvPath(id) + (toRead ? '/read' : '/unread'));
  if (!res.ok) { showToast(res.error, 'error'); return; }
  t.local.unread = !toRead;
  ibxRenderHead();
  ibxMarkRow(id, !toRead);
  showToast(toRead ? 'Marked read.' : 'Marked unread. It stays bold until you open it again.', 'success');
  ibxLoadList(true);
}

function ibxSnoozeMenu(anchor) {
  const t = _ib.thread;
  if (!t) return;
  const extra = t.local.snoozed ? [{ divider: true }, { label: 'Unsnooze', icon: 'x', on: ibxUnsnooze }] : [];
  ibxTimeMenu(anchor, 'Snooze until', async when => {
    const res = await ibxSend('POST', ibxConvPath(t.conversation.id) + '/snooze', { until: when.toISOString() });
    if (!res.ok) { showToast(res.error, 'error'); return; }
    ibxToast('Snoozed until ' + ibxWhen(when), 'It leaves the list until then, and comes back early if they write again.');
    ibxAfterChange();
  }, extra);
}

async function ibxUnsnooze() {
  const t = _ib.thread;
  if (!t) return;
  const res = await ibxSend('DELETE', ibxConvPath(t.conversation.id) + '/snooze');
  if (!res.ok) { showToast(res.error, 'error'); return; }
  showToast('Back in the inbox.', 'success');
  ibxAfterChange();
}

function ibxRemindMenu(anchor) {
  const t = _ib.thread;
  if (!t) return;
  const open = (t.reminders || []).find(r => !r.done_at);
  const extra = open ? [{ divider: true }, { label: 'Mark the reminder done', icon: 'check', on: () => ibxReminderDone(open.id) }] : [];
  ibxTimeMenu(anchor, 'Remind me', when => ibxAddReminder(when), extra);
}

async function ibxAddReminder(when) {
  const t = _ib.thread;
  if (!t) return;
  const note = await promptModal({
    title: 'Remind me about ' + ((t.prospect && t.prospect.name) || 'this conversation'),
    copy: 'On ' + ibxWhen(when) + ' it shows on Today and under Needs you. Nothing is sent. Add a note for yourself if it helps.',
    placeholder: 'Confirm Thursday’s call and send the invite',
    ok: 'Set reminder', icon: 'bell', required: false,
  });
  if (note === null) return;
  const res = await ibxSend('POST', ibxConvPath(t.conversation.id) + '/reminders',
                            { due_at: when.toISOString(), note });
  if (!res.ok) { showToast(res.error, 'error'); return; }
  showToast('Reminder set for ' + ibxWhen(when) + '.', 'success');
  ibxAfterChange();
}

async function ibxReminderDone(id) {
  const res = await ibxSend('POST', '/api/inbox/reminders/' + encodeURIComponent(id) + '/done');
  if (!res.ok) { showToast(res.error, 'error'); return; }
  showToast('Reminder done.', 'success');
  ibxAfterChange();
}

async function ibxReminderDelete(id) {
  const res = await ibxSend('DELETE', '/api/inbox/reminders/' + encodeURIComponent(id));
  if (!res.ok) { showToast(res.error, 'error'); return; }
  ibxAfterChange();
}

function ibxMoreMenu(anchor) {
  const t = _ib.thread;
  if (!t) return;
  const entries = [];
  if (ibxPhone()) {
    entries.push({ label: t.local.unread ? 'Mark read' : 'Mark unread',
      icon: t.local.unread ? 'envelope-simple-open' : 'envelope-simple', on: ibxToggleRead });
  }
  const c = _ib.comp, d = ibxEditable(t);
  if (ibxPhone() && t.compose.allowed && c && c.text !== c.base) entries.push({ label: 'Save draft', icon: 'pencil-simple', on: ibxSave });
  if (d && d.status !== 'sending' && t.compose.allowed) entries.push({ label: 'Discard draft', icon: 'trash', on: ibxDiscard });
  if (t.prospect.id) entries.push({ label: 'Open contact', icon: 'address-book', on: () => ibxOpenContact(t.prospect.id) });
  if (t.company.id) entries.push({ label: 'Open company', icon: 'buildings', on: () => ibxOpenCompany(t.company.id) });
  if (t.prospect.email && !(t.restrictions || {}).exclusion) {
    entries.push({ divider: true });
    entries.push({ label: 'Exclude this contact', icon: 'prohibit', danger: true, on: ibxExcludeOne });
  }
  ibxMenu(anchor, entries, { align: 'right' });
}

async function ibxExcludeOne() {
  const t = _ib.thread;
  const ok = await confirmModal({
    title: 'Exclude ' + (t.prospect.name || t.prospect.email) + '?',
    copy: 'Mercury stops every email to ' + t.prospect.email + ', including anything already queued. ' +
      'You can lift it later from the Exclusions tab.',
    ok: 'Exclude',
  });
  if (!ok) return;
  const res = await ibxSend('POST', '/api/inbox/bulk',
                            { action: 'exclude', conversation_ids: [t.conversation.id], confirm: true });
  const r = res.ok ? (res.data.results || [])[0] : null;
  if (!r || !r.ok) { showToast('Could not exclude: ' + (r ? r.message : res.error), 'error'); return; }
  ibxToast('Excluded ' + t.prospect.email, r.blocked_queued ? r.blocked_queued + ' queued email' +
    (r.blocked_queued === 1 ? ' was' : 's were') + ' blocked.' : '');
  ibxAfterChange();
}

async function ibxOpenContact(pid) {
  if (!_pipe.loaded) await loadPipeline();
  if (pipeFind(pid)) openProspectDrawer(pid);
  else showToast('That contact is not on the Pipeline board.', 'error');
}

async function ibxOpenCompany(cid) {
  showTab('companies');
  await loadCompanies();
  const i = _companies.findIndex(c => c.id === cid);
  if (i >= 0) showCompanyContacts(i);
}

async function ibxAfterChange() {
  await Promise.all([ibxLoadList(true), _ib.sel ? ibxLoadThread(_ib.sel, true) : null]);
}

// ── Messages ──

function ibxMessages(t) {
  const msgs = t.messages || [];
  const events = (t.events || []).map(e => ({ ...e, direction: 'event' }));
  let all = msgs.concat(events).sort((a, b) => (parseUTC(a.at) || 0) - (parseUTC(b.at) || 0));
  let html = '';
  if (t.partial_history) {
    html += '<div class="ibx-sys">' + icon('info') + '<span>Some of this conversation was recorded before Mercury ' +
      'stored mail, so those messages have no mailbox or times to the minute.</span></div>';
  }
  // On a phone the thread opens on the newest message: earlier emails of
  // ours fold into one line.
  if (ibxPhone() && !_ib.earlierOpen) {
    const lastIn = all.map(m => m.direction).lastIndexOf('inbound');
    const earlier = lastIn > 0 ? all.slice(0, lastIn).filter(m => m.direction === 'outbound') : [];
    if (earlier.length >= 2 && all.slice(0, lastIn).every(m => m.direction !== 'inbound')) {
      html += '<button class="link-btn ibx-earlier" onclick="ibxShowEarlier()">' + icon('dots-three') +
        earlier.length + ' earlier emails from you</button>';
      all = all.slice(lastIn);
    }
  }
  if (!all.length) {
    html += '<div class="ibx-sys">' + icon('info') + '<span>No messages stored for this conversation yet.</span></div>';
  }
  html += all.map(m => m.direction === 'event' ? ibxEvent(m, t) : ibxMessage(m, t)).join('');
  if ((t.unsent || []).length) {
    html += '<div class="ibx-unsent-head">' + icon('prohibit') + 'Not sent</div>' +
      t.unsent.slice().reverse().map(ibxUnsent).join('');
  }
  return html;
}

function ibxShowEarlier() { _ib.earlierOpen = true; ibxEl('ibx-msgs').innerHTML = ibxMessages(_ib.thread); }

// Quoted history below "On ... wrote:" or ">" lines folds away.
function ibxBody(text) {
  const lines = String(text || '').replace(/\s+$/, '').split('\n');
  const cut = lines.findIndex((l, i) => i > 0 && (/^\s*>/.test(l) || /^On .{4,200} wrote:\s*$/.test(l.trim())));
  if (cut < 1) return '<div class="ibx-text">' + escHtml(lines.join('\n')) + '</div>';
  return '<div class="ibx-text">' + escHtml(lines.slice(0, cut).join('\n').replace(/\s+$/, '')) + '</div>' +
    '<details class="ibx-quoted"><summary>Show quoted text</summary><div class="ibx-text">' +
    escHtml(lines.slice(cut).join('\n')) + '</div></details>';
}

function ibxMessage(m, t) {
  const when = ibxWhen(m.at);
  if (m.direction === 'outbound') {
    const what = m.kind === 'sequence' && m.step ? 'Step ' + m.step : m.kind === 'reply' ? 'Reply' : '';
    const status = m.delivery === 'sent' ? toneBadge('good', 'Sent') : toneBadge('idle', 'Recorded');
    return '<article class="ibx-msg out">' +
      '<div class="ibx-msg-head">' + icon('paper-plane-tilt') + '<b>You, via Mercury</b>' +
        '<span class="ibx-msg-when">' + escHtml([what, when].filter(Boolean).join(' · ')) + '</span>' +
        '<span class="ibx-msg-badge">' + status + '</span></div>' +
      (m.mailbox && m.mailbox !== (_ib.thread || {}).mailbox ? '<div class="ibx-msg-from">from <span class="mono">' + escHtml(m.mailbox) + '</span></div>' : '') +
      ibxBody(m.body) + '</article>';
  }
  const intent = IBX_INTENT[m.intent];
  const auto = m.kind && m.kind !== 'message';
  const failed = m.ingestion && m.ingestion.status === 'failed';
  const name = (t.prospect && t.prospect.name) || m.from_email || 'They';
  return '<article class="ibx-msg in">' +
    '<div class="ibx-msg-head">' + icon('arrow-bend-up-left') + '<b>' + escHtml(name) + '</b>' +
      '<span class="ibx-msg-when">' + escHtml(when) + '</span>' +
      '<span class="ibx-msg-badge">' + (auto ? toneBadge('idle', m.kind === 'bounce' ? 'Bounce' : 'Automatic reply')
        : intent ? toneBadge(intent[1], intent[0]) : '') + '</span></div>' +
    ibxBody(m.body) +
    ((m.also_received_in || []).length ? '<div class="ibx-msg-foot">Also delivered to <span class="mono">' +
      m.also_received_in.map(escHtml).join(', ') + '</span></div>' : '') +
    (failed ? '<div class="ibx-msg-foot bad">' + icon('warning-circle') + 'Mercury could not process this message' +
      (m.ingestion.error ? ': ' + escHtml(m.ingestion.error) : '') + '. Answer it yourself if it needs one.</div>' : '') +
    '</article>';
}

function ibxEvent(e, t) {
  const when = '<span class="ibx-sys-when">' + escHtml(ibxWhen(e.at)) + '</span>';
  if (e.kind === 'exclusion') {
    const who = e.source === 'opt_out' || e.source === 'bounce' ? 'Mercury' : 'You';
    return '<div class="ibx-sys">' + icon('prohibit') + '<span>' + when + ' ' + who + ' added <span class="mono">' +
      escHtml(e.value || t.prospect.email) + '</span> to Exclusions' +
      (e.source === 'opt_out' ? ' after they asked to be taken off the list' : '') + '. Queued emails to them are blocked.</span></div>';
  }
  if (e.kind === 'pause') {
    return '<div class="ibx-sys">' + icon('pause-circle') + '<span>' + when + ' Mercury paused their sequence' +
      (e.resume_at ? ' until ' + escHtml(ibxWhen(e.resume_at)) : '') + '. Replies still go.</span></div>';
  }
  if (e.kind === 'stage') {
    return '<div class="ibx-sys">' + icon(ibxStage(e.to)[2]) + '<span>' + when + ' You moved this to ' +
      escHtml(ibxStage(e.to)[1]) + '.</span></div>';
  }
  return '';
}

function ibxUnsent(d) {
  const why = d.error ? String(d.error).replace(/_/g, ' ') : '';
  return '<article class="ibx-msg unsent">' +
    '<div class="ibx-msg-head">' + icon('paper-plane-tilt') + '<b>Your reply</b>' +
      '<span class="ibx-msg-when">' + escHtml(ibxWhen(d.updated_at || d.created_at)) + '</span>' +
      '<span class="ibx-msg-badge">' + badge(d.status) + '</span></div>' +
    ibxBody(d.body) +
    (why ? '<div class="ibx-msg-foot">' + escHtml(why.charAt(0).toUpperCase() + why.slice(1)) + '. It was never sent.</div>'
      : '<div class="ibx-msg-foot">It was never sent.</div>') +
    '</article>';
}

// ── Composer ──

function ibxWords(s) { return (String(s).trim().match(/\S+/g) || []).length; }

function ibxRenderComposer() {
  const t = _ib.thread, el = ibxEl('ibx-composer'), c = _ib.comp;
  if (!t || !el) return;
  const comp = t.compose;
  const d = ibxEditable(t);
  el.classList.toggle('blocked', !comp.allowed);
  if (!comp.allowed) { el.innerHTML = ibxBlocked(t); return; }
  const phone = ibxPhone();
  const sending = d && d.status === 'sending';
  const approved = d && d.approved && c.text === c.base;
  let state;
  if (!d) state = '<span class="badge t-idle">' + icon('pencil-simple') + 'New reply</span>';
  else if (sending) state = toneBadge('active', 'Sending now');
  else if (d.status === 'blocked') state = toneBadge('bad', 'Blocked, not sending');
  else if (approved) state = toneBadge('good', 'Approved, sends ' + ibxSoon(d.send_at || new Date()));
  else state = toneBadge('waiting', (Number(d.manually_edited) ? 'Your draft' : 'Mercury’s draft') + ', waiting for your review');
  const from = (d && (d.from_mailbox || d.mailbox)) || comp.mailbox;
  const answering = ibxAnswering(t, d);
  const policy = d && d.policy && d.policy.code !== 'ok' ? d.policy : null;
  const paused = (t.restrictions || {}).sending || {};
  el.innerHTML =
    '<div class="ibx-comp-head">' + state +
      (from ? '<span class="ibx-from">Reply from <span class="mono">' + escHtml(from) + '</span></span>' : '') + '</div>' +
    (c.notice ? ibxNotice(c.notice) : '') +
    '<div class="ibx-editor' + (c.busy ? ' busy' : '') + '">' +
      '<label class="sr-only" for="ibx-body">Reply to ' + escHtml(ibxFirst(t)) + '</label>' +
      '<textarea id="ibx-body" rows="' + (phone ? 6 : 7) + '" placeholder="Write a reply to ' + escHtml(ibxFirst(t)) + '…"' +
        (sending || c.busy ? ' readonly' : '') + ' oninput="ibxInput(this.value)">' + escHtml(c.text) + '</textarea>' +
      '<div class="ibx-editor-foot"><span>' + escHtml(answering) + '</span>' +
        '<span class="num" id="ibx-words">' + ibxWords(c.text) + ' words</span></div>' +
      (c.busy ? '<div class="ibx-busy">' + icon('sparkle') + escHtml(c.busy) + '</div>' : '') +
    '</div>' +
    (policy ? '<div class="ibx-comp-note bad">' + icon('warning-circle') + '<span>' + escHtml(policy.reason) +
      (policy.action ? ' ' + escHtml(policy.action) : '') + '</span></div>' : '') +
    (paused.paused ? '<div class="ibx-comp-note">' + icon('pause-circle') + '<span>Sending is paused, so approved replies wait. ' +
      escHtml(paused.reason || '') + '</span></div>' : '') +
    '<div class="ibx-comp-actions">' +
      '<button class="btn btn-primary ibx-approve" onclick="ibxApprove()"' + (sending || approved || c.busy ? ' disabled' : '') + '>' +
        (approved ? icon('check') + 'Approved' : 'Approve and send') + (phone || approved ? '' : '<kbd>A</kbd>') + '</button>' +
      '<button class="btn btn-secondary ibx-act" onclick="ibxScheduleMenu(this)" title="Schedule" aria-label="Schedule"' + (sending || c.busy ? ' disabled' : '') + '>' +
        icon('clock') + '<span>Schedule</span></button>' +
      '<button class="btn btn-secondary ibx-act" onclick="ibxRegenerate()" title="' + (d ? 'Regenerate' : 'Mercury writes a draft for your review') +
        '" aria-label="' + (d ? 'Regenerate' : 'Draft for me') + '"' + (sending || c.busy ? ' disabled' : '') + '>' +
        icon('sparkle') + '<span>' + (d ? 'Regenerate' : 'Draft for me') + '</span></button>' +
      '<span class="ibx-spacer"></span>' +
      '<button class="btn btn-secondary ibx-save" onclick="ibxSave()"' + (sending || c.busy || c.text === c.base ? ' disabled' : '') + '>Save draft</button>' +
    '</div>';
}

function ibxAnswering(t, d) {
  const id = d && d.answers_inbound_id;
  const msg = id ? (t.messages || []).find(m => m.id === id) : null;
  const target = msg ? { at: msg.at, subject: msg.subject } : t.compose.reply_to
    ? { at: t.compose.reply_to.received_at, subject: t.compose.reply_to.subject } : null;
  if (!target) return (d && d.subject) || t.compose.subject || '';
  const day = parseUTC(target.at);
  return 'In reply to ' + ibxFirst(t) + '’s message' + (day ? ' of ' + ibxDay(day) : '') +
    (target.subject ? ' · ' + target.subject : '');
}

function ibxBlocked(t) {
  const comp = t.compose;
  const copy = {
    escalated: ['warning', 'Escalated to you', 'Mercury does not write to escalated threads. Answer from your own mail client.'],
    opted_out: ['prohibit', 'You can’t reply to this conversation', comp.reason],
    excluded: ['prohibit', 'You can’t reply to this conversation', comp.reason],
    invalid_address: ['warning-circle', 'Their address bounced', comp.reason],
    no_contact: ['info', 'No address to write to', comp.reason],
  }[comp.code] || ['prohibit', 'You can’t reply here', comp.reason];
  const action = comp.code === 'opted_out' || comp.code === 'excluded'
    ? '<button class="btn btn-secondary" onclick="goTab(\'exclusions\')">Open Exclusions</button>'
    : comp.code === 'escalated' && t.prospect.email
      ? '<button class="btn btn-secondary" onclick="ibxCopy(' + ibxArg(t.prospect.email) + ')">' + icon('copy') + 'Copy address</button>'
      : '';
  const reason = comp.code === 'escalated' && comp.reason && comp.reason !== copy[2] ? comp.reason : '';
  return '<div class="ibx-blocked">' + icon(copy[0]) +
    '<div><b>' + escHtml(copy[1]) + '</b><p>' + escHtml(copy[2]) + (reason ? ' ' + escHtml(reason) : '') + '</p></div>' +
    action + '</div>';
}

async function ibxCopy(text) {
  try { await navigator.clipboard.writeText(text); showToast('Copied ' + text + '.', 'success'); }
  catch { showToast('Could not copy. The address is ' + text + '.', 'error'); }
}

function ibxInput(v) {
  const c = _ib.comp;
  if (!c) return;
  const wasClean = c.text === c.base;
  c.text = v;
  const w = ibxEl('ibx-words');
  if (w) w.textContent = ibxWords(v) + ' words';
  if (wasClean !== (c.text === c.base)) {
    const save = document.querySelector('#ibx-composer .ibx-save');
    if (save) save.disabled = c.text === c.base;
    const approve = document.querySelector('#ibx-composer .ibx-approve');
    if (approve && approve.textContent.startsWith('Approved')) ibxRenderComposerKeepFocus();
  }
}

function ibxRenderComposerKeepFocus() {
  const ta = ibxEl('ibx-body');
  const pos = ta ? [ta.selectionStart, ta.selectionEnd, ta.scrollTop] : null;
  const focused = ta && document.activeElement === ta;
  ibxRenderComposer();
  const next = ibxEl('ibx-body');
  if (next && focused) {
    next.focus({ preventScroll: true });
    if (pos) { next.setSelectionRange(pos[0], pos[1]); next.scrollTop = pos[2]; }
  }
}

function ibxNotice(n) {
  const actions = n.kind === 'stale'
    ? '<button class="btn btn-secondary btn-sm" onclick="ibxReloadDraft()">Reload draft</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="ibxNewFromText()">Start a new draft from this text</button>'
    : n.kind === 'gone'
      ? '<button class="btn btn-secondary btn-sm" onclick="ibxReloadDraft()">Reload</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="ibxNewFromText()">Start a new draft from this text</button>'
      : '';
  return '<div class="ibx-notice">' + icon('warning-circle') + '<div><p>' + escHtml(n.message) + '</p>' +
    (actions ? '<div class="ibx-notice-actions">' + actions + '</div>' : '') + '</div></div>';
}

// A refused draft change keeps the typed text and explains what happened.
function ibxRefused(res) {
  const c = _ib.comp;
  if (res.code === 'stale_revision' || res.code === 'draft_exists') {
    c.notice = { kind: 'stale', message: 'This draft changed somewhere else after you opened it (Mercury rewrote it, or another tab saved it). Your text is still here.' };
  } else if (res.code === 'not_editable' || res.status === 404) {
    c.notice = { kind: 'gone', message: 'This draft can no longer be changed: it was sent, rejected or cancelled. Your text is still here.' };
  } else if (res.code === 'compose_refused') {
    ibxLoadThread(_ib.sel, true);
    showToast(res.error, 'error');
    return;
  } else {
    c.notice = { kind: 'error', message: res.error };
  }
  ibxRenderComposerKeepFocus();
}

async function ibxReloadDraft() {
  const c = _ib.comp;
  c.text = c.base;   // drop the local copy on purpose
  c.notice = null;
  _ib.comp = null;
  await ibxLoadThread(_ib.sel);
}

// Put the typed text on the newest draft, or a fresh one when none is left.
async function ibxNewFromText() {
  const c = _ib.comp, id = _ib.sel, text = c.text;
  const res = await getJSON(ibxConvPath(id));
  if (!res.ok) { showToast('Could not reload the conversation.', 'error'); return; }
  const d = ibxEditable(res.data);
  let r;
  if (d && d.status !== 'sending') {
    r = await ibxSend('PUT', ibxConvPath(id) + '/drafts/' + encodeURIComponent(d.id),
                      { subject: d.subject, body: text, revision: d.revision });
  } else {
    r = await ibxSend('POST', ibxConvPath(id) + '/drafts', { body: text });
  }
  if (!r.ok) { ibxRefused(r); return; }
  _ib.comp = null;
  showToast('Saved as a new draft. It waits for your review.', 'success');
  await ibxAfterChange();
}

// Save the working copy (creating the draft if there is none). Resolves to
// the draft's id and new revision, or null when it was refused.
async function ibxSaveNow() {
  const t = _ib.thread, c = _ib.comp;
  if (!c.text.trim()) { c.notice = { kind: 'error', message: 'Write something first.' }; ibxRenderComposerKeepFocus(); return null; }
  const id = t.conversation.id;
  let r;
  if (c.draftId) {
    if (c.text === c.base) return { id: c.draftId, revision: c.revision };
    r = await ibxSend('PUT', ibxConvPath(id) + '/drafts/' + encodeURIComponent(c.draftId),
                      { subject: c.subject, body: c.text, revision: c.revision });
  } else {
    r = await ibxSend('POST', ibxConvPath(id) + '/drafts', { body: c.text });
  }
  if (!r.ok) { ibxRefused(r); return null; }
  Object.assign(c, { draftId: r.data.id, revision: r.data.revision, base: r.data.body, text: r.data.body,
                     subject: r.data.subject, notice: null });
  return { id: r.data.id, revision: r.data.revision };
}

async function ibxSave() {
  const saved = await ibxSaveNow();
  if (!saved) return;
  showToast('Draft saved. It waits for your review.', 'success');
  ibxAfterChange();
}

async function ibxApprove() {
  const t = _ib.thread, c = _ib.comp;
  if (!t || !c || c.busy || !t.compose.allowed) return;
  const saved = await ibxSaveNow();
  if (!saved) return;
  const r = await ibxSend('POST', ibxConvPath(t.conversation.id) + '/drafts/' + encodeURIComponent(saved.id) + '/approve',
                          { revision: saved.revision });
  if (!r.ok) { ibxRefused(r); return; }
  ibxToast('Approved. Mercury sends it on its next cycle.', 'It goes from ' + (r.data.from_mailbox || r.data.mailbox || t.compose.mailbox) +
    ' after the usual checks.');
  ibxAfterChange();
}

function ibxScheduleMenu(anchor) {
  ibxTimeMenu(anchor, 'Send at', async when => {
    const t = _ib.thread;
    const saved = await ibxSaveNow();
    if (!saved) return;
    const r = await ibxSend('POST', ibxConvPath(t.conversation.id) + '/drafts/' + encodeURIComponent(saved.id) + '/schedule',
                            { send_at: when.toISOString(), revision: saved.revision });
    if (!r.ok) { ibxRefused(r); return; }
    ibxToast('Scheduled for ' + ibxWhen(when), 'Approved for that time. Editing it sends it back to review.');
    ibxAfterChange();
  }, [], 'future');
}

async function ibxRegenerate() {
  const t = _ib.thread, c = _ib.comp;
  if (!t || !c || c.busy) return;
  const d = ibxEditable(t);
  if (c.text !== c.base && d) {
    const ok = await confirmModal({ title: 'Replace your edits?',
      copy: 'Regenerating writes a new draft from scratch. The edits you have not saved are lost.', ok: 'Regenerate' });
    if (!ok) return;
  }
  const instruction = await promptModal({
    title: (d ? 'Regenerate ' : 'Write ') + ibxFirst(t) + '’s reply',
    copy: d ? 'Tell Mercury what to change. The new draft replaces this one and waits for your review again.'
      : 'Mercury writes a reply from the conversation. Add an instruction if you want something specific. It waits for your review.',
    placeholder: 'Shorter, warmer, ask about something else...',
    suggestions: ['Shorter', 'Warmer', 'One question only'],
    ok: d ? 'Regenerate' : 'Write the reply', icon: 'sparkle', required: false,
  });
  if (instruction === null) return;
  const id = t.conversation.id;
  c.busy = d ? 'Mercury is rewriting the reply…' : 'Mercury is writing a reply…';
  c.notice = null;
  ibxRenderComposer();
  const r = d
    ? await ibxSend('POST', ibxConvPath(id) + '/drafts/' + encodeURIComponent(d.id) + '/regenerate',
                    { instruction, revision: d.revision })
    : await ibxSend('POST', ibxConvPath(id) + '/drafts', { generate: true, instruction });
  c.busy = '';
  if (_ib.comp !== c || _ib.sel !== id) return;
  if (!r.ok) { ibxRefused(r); return; }
  _ib.comp = null;
  showToast('New draft ready for your review.', 'success');
  ibxAfterChange();
}

async function ibxDiscard() {
  const t = _ib.thread, d = t && ibxEditable(t);
  if (!d) return;
  const ok = await confirmModal({ title: 'Discard this draft?',
    copy: 'It is rejected and never sent. You can write a new one afterwards.', ok: 'Discard' });
  if (!ok) return;
  const r = await ibxSend('POST', ibxConvPath(t.conversation.id) + '/drafts/' + encodeURIComponent(d.id) + '/discard',
                          { revision: d.revision });
  if (!r.ok) { ibxRefused(r); return; }
  _ib.comp = null;
  showToast('Draft discarded.', 'success');
  ibxAfterChange();
}

// ── Context: who they are, the company, the stage, a reminder, notes ──

function ibxRenderCtx() {
  const t = _ib.thread;
  if (!t) return;
  if (_ib.noteEdit && document.activeElement && document.activeElement.closest &&
      document.activeElement.closest('.ibx-note-form')) return;   // typing a note
  const html = ibxCtx(t);
  ibxEl('ibx-ctx').innerHTML = html;
  const notes = (t.notes || []).length;
  const fold = ibxEl('ibx-ctx-fold');
  if (fold) {
    fold.innerHTML = '<button class="ibx-fold-btn" aria-expanded="' + _ib.ctxOpen + '" onclick="ibxToggleCtx()">' +
      icon('identification-card') + '<span>Contact, notes and reminder</span>' +
      '<span class="num">' + notes + (notes === 1 ? ' note' : ' notes') + '</span>' + icon(_ib.ctxOpen ? 'caret-up' : 'caret-down') +
      '</button>' + (_ib.ctxOpen ? '<div class="ibx-fold-body">' + html + '</div>' : '');
  }
}

function ibxToggleCtx() { _ib.ctxOpen = !_ib.ctxOpen; ibxRenderCtx(); }

function ibxCtx(t) {
  const p = t.prospect, co = t.company, c = t.conversation;
  const ex = (t.restrictions || {}).exclusion;
  const verify = ex ? toneBadge('bad', 'Excluded' + (ex.created_at ? ' ' + ibxDay(parseUTC(ex.created_at)) : ''))
    : p.email_status ? badge(p.email_status) : '';
  const row = (g, text, mono) => text ? '<div class="ibx-fact">' + icon(g) + '<span' + (mono ? ' class="mono"' : '') + '>' + escHtml(text) + '</span></div>' : '';
  const signals = (co.signals || []).map(s => s.code + (s.value !== '' && s.value !== null && s.value !== undefined ? ' ' + s.value : '')).join(', ');
  const st = ibxStage(c.stage);
  const open = (t.reminders || []).filter(r => !r.done_at).sort((a, b) => (parseUTC(a.due_at) || 0) - (parseUTC(b.due_at) || 0));
  const next = open[0];
  const role = [p.title, co.name].filter(Boolean).join(' at ');
  return '<div class="ibx-ctx-sec ibx-id">' +
      '<h3>' + escHtml(p.name || p.email || 'Unknown contact') + '</h3>' +
      (role ? '<p>' + escHtml(role) + '</p>' : '') +
      (p.email ? '<div class="ibx-email"><span class="mono">' + escHtml(p.email) + '</span>' + verify + '</div>' : '') +
    '</div>' +
    '<div class="ibx-ctx-sec"><div class="ibx-ctx-label"><span>Company</span>' +
      (co.id ? '<button class="link-btn ibx-link" onclick="ibxOpenCompany(' + ibxArg(co.id) + ')">Open</button>' : '') + '</div>' +
      (co.name && !co.id ? row('buildings', co.name) : '') +
      row('map-pin', co.location) + row('globe-simple', co.domain, true) + row('tag', co.offer_key, true) +
      row('funnel', signals, true) +
      (!co.location && !co.domain && !co.offer_key && !signals && !co.name ? '<div class="ibx-fact muted">Nothing known yet.</div>' : '') +
    '</div>' +
    '<div class="ibx-ctx-sec"><div class="ibx-ctx-label"><span>Pipeline</span></div>' +
      '<button class="ibx-stage" aria-haspopup="menu" onclick="ibxStageMenu(this)">' + icon(st[2]) +
        '<span>' + escHtml(st[1]) + '</span>' + icon('caret-down') + '</button>' +
      '<div class="ibx-since">' + escHtml(ibxSince(t)) + '</div>' +
    '</div>' +
    '<div class="ibx-ctx-sec"><div class="ibx-ctx-label"><span>Reminder</span>' +
      '<button class="link-btn ibx-link" onclick="ibxRemindMenu(this)">' + (next ? 'Add' : 'Set') + '</button></div>' +
      (next ? open.map(r => {
        const due = parseUTC(r.due_at), isDue = due && due <= new Date();
        return '<div class="ibx-reminder' + (isDue ? ' due' : '') + '">' + icon('bell') +
          '<div><b>' + escHtml(ibxWhen(due)) + '</b>' + (isDue ? ' ' + toneBadge('waiting', 'Due') : '') +
          (r.note ? '<p>' + escHtml(r.note) + '</p>' : '') + '</div>' +
          '<button class="btn btn-secondary btn-sm" onclick="ibxReminderDone(' + ibxArg(r.id) + ')">Done</button>' +
          '<button class="ibx-icon-btn" onclick="ibxReminderDelete(' + ibxArg(r.id) + ')" title="Delete reminder" aria-label="Delete reminder">' + icon('x') + '</button>' +
        '</div>';
      }).join('') : '<div class="ibx-fact muted">' + icon('bell') + '<span>' + (c.status === 'closed' ? 'None' : 'No reminder yet') + '</span></div>') +
    '</div>' +
    '<div class="ibx-ctx-sec"><div class="ibx-ctx-label"><span>Notes</span></div>' +
      (t.notes || []).map(ibxNote).join('') +
      (_ib.noteEdit && _ib.noteEdit.id === 'new' ? ibxNoteForm('new', _ib.noteEdit.text)
        : p.id ? '<button class="ibx-add-note" onclick="ibxNoteStart(\'new\')">' + icon('plus') + 'Add a note</button>' : '') +
    '</div>' +
    '<p class="ibx-local">Read state, notes and reminders live in Mercury only. They don’t change anything in the mailbox.</p>';
}

function ibxSince(t) {
  const s = t.conversation.stage_since || {};
  const d = parseUTC(s.at);
  const day = d ? ibxDay(d) : '';
  if (!day) return '';
  return { replied: 'Since ' + day + ', when ' + ibxFirst(t) + ' replied', set: 'Since ' + day + ', set by you',
    opted_out: 'Closed ' + day + ', after the opt-out', closed: 'Closed ' + day,
    started: 'Since ' + day }[s.reason] || 'Since ' + day;
}

function ibxStageMenu(anchor) {
  const t = _ib.thread;
  if (!t) return;
  const cur = t.conversation.stage;
  const entries = [];
  IBX_STAGES.forEach(([key, label, g]) => {
    if (key === 'closed_won') entries.push({ divider: true });
    entries.push({ label, icon: g, checked: key === cur, on: () => ibxSetStage(key) });
  });
  ibxMenu(anchor, entries, { width: anchor.getBoundingClientRect().width, stage: true });
}

async function ibxSetStage(key) {
  const t = _ib.thread;
  if (!t || key === t.conversation.stage) return;
  const r = await ibxSend('POST', ibxConvPath(t.conversation.id) + '/stage', { stage: key });
  if (!r.ok) { showToast('Could not change the stage: ' + r.error, 'error'); return; }
  showToast('Moved to ' + ibxStage(key)[1] + '.', 'success');
  ibxAfterChange();
}

function ibxNote(n) {
  if (_ib.noteEdit && _ib.noteEdit.id === n.id) return ibxNoteForm(n.id, _ib.noteEdit.text);
  const who = /^(mercury|agent|handler)/i.test(n.created_by || '') ? 'Mercury' : 'You';
  return '<div class="ibx-note"><p>' + escHtml(n.body) + '</p>' +
    '<div class="ibx-note-foot"><span>' + who + ' · ' + escHtml(ibxDay(parseUTC(n.created_at) || new Date())) + '</span>' +
      '<span class="ibx-note-acts"><button class="ibx-icon-btn" onclick="ibxNoteStart(' + ibxArg(n.id) + ')" title="Edit note" aria-label="Edit note">' +
      icon('pencil-simple') + '</button><button class="ibx-icon-btn" onclick="ibxNoteDelete(' + ibxArg(n.id) + ')" title="Delete note" aria-label="Delete note">' +
      icon('trash') + '</button></span></div></div>';
}

function ibxNoteForm(id, text) {
  return '<div class="ibx-note-form"><textarea class="form-input" rows="3" maxlength="2000" placeholder="What should you remember about them?" ' +
    'oninput="_ib.noteEdit.text = this.value" onkeydown="if((event.metaKey||event.ctrlKey)&&event.key===\'Enter\')ibxNoteSave()">' +
    escHtml(text) + '</textarea><div class="ibx-note-form-acts">' +
    '<button class="btn btn-secondary btn-sm" onclick="ibxNoteCancel()">Cancel</button>' +
    '<button class="btn btn-primary btn-sm" onclick="ibxNoteSave()">' + (id === 'new' ? 'Add note' : 'Save') + '</button></div></div>';
}

function ibxNoteStart(id) {
  const t = _ib.thread;
  const n = id === 'new' ? null : (t.notes || []).find(x => x.id === id);
  _ib.noteEdit = { id, text: n ? n.body : '' };
  ibxRenderCtx();
  const ta = document.querySelector((ibxPhone() || window.innerWidth <= 1100 ? '#ibx-ctx-fold' : '#ibx-ctx') + ' .ibx-note-form textarea');
  if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
}

function ibxNoteCancel() { _ib.noteEdit = null; ibxRenderCtx(); }

async function ibxNoteSave() {
  const t = _ib.thread, e = _ib.noteEdit;
  if (!t || !e) return;
  if (!e.text.trim()) { showToast('A note needs some text.', 'error'); return; }
  const r = e.id === 'new'
    ? await ibxSend('POST', '/api/inbox/contacts/' + encodeURIComponent(t.prospect.id) + '/notes', { body: e.text })
    : await ibxSend('PATCH', '/api/inbox/notes/' + encodeURIComponent(e.id), { body: e.text });
  if (!r.ok) { showToast('Could not save the note: ' + r.error, 'error'); return; }
  _ib.noteEdit = null;
  if (document.activeElement) document.activeElement.blur();
  await ibxLoadThread(t.conversation.id, true);
}

async function ibxNoteDelete(id) {
  const ok = await confirmModal({ title: 'Delete this note?', copy: 'It is removed from this contact.', ok: 'Delete' });
  if (!ok) return;
  const r = await ibxSend('DELETE', '/api/inbox/notes/' + encodeURIComponent(id));
  if (!r.ok) { showToast('Could not delete the note: ' + r.error, 'error'); return; }
  await ibxLoadThread(_ib.sel, true);
}

// ── Menus: one at a time, anchored to the button that opened it ──

function ibxMenu(anchor, entries, opts) {
  opts = opts || {};
  ibxCloseMenu();
  const m = document.createElement('div');
  m.className = 'menu ibx-menu' + (opts.stage ? ' ibx-stage-menu' : '');
  m.setAttribute('role', 'menu');
  const acts = [];
  m.innerHTML = (opts.label ? '<div class="menu-label">' + escHtml(opts.label) + '</div>' : '') + entries.map(e => {
    if (e.divider) return '<div class="ibx-menu-div" role="separator"></div>';
    if (e.text) return '<div class="menu-empty">' + escHtml(e.text) + '</div>';
    if (e.html) return e.html;
    acts.push(e.on);
    return '<button role="menuitem" data-i="' + (acts.length - 1) + '" class="' + (e.checked ? 'on' : '') + (e.danger ? ' danger' : '') + '"' +
      (e.disabled ? ' disabled' : '') + '><span class="ibx-mi">' + (e.icon ? icon(e.icon) : '') +
      '<span' + (e.mono ? ' class="mono"' : '') + '>' + escHtml(e.label) + '</span></span>' +
      (e.hint ? '<small class="num">' + escHtml(e.hint) + '</small>' : '') + (e.checked ? icon('check', 'ibx-check') : '') + '</button>';
  }).join('');
  m.addEventListener('click', ev => {
    const b = ev.target.closest('button[data-i]');
    if (!b) return;
    const fn = acts[+b.dataset.i];
    ibxCloseMenu(true);
    if (fn) fn();
  });
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  if (opts.width) m.style.width = opts.width + 'px';
  const w = m.offsetWidth, h = m.offsetHeight;
  let left = opts.align === 'right' || r.left + w > window.innerWidth - 8 ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  let top = r.bottom + 6;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
  m.style.left = left + 'px';
  m.style.top = top + 'px';
  anchor.setAttribute('aria-expanded', 'true');
  _ib.menu = { el: m, anchor };
  const first = m.querySelector('button:not([disabled]), input');
  if (first) first.focus({ preventScroll: true });
  return m;
}

function ibxCloseMenu(restoreFocus) {
  const menu = _ib.menu;
  if (!menu) return;
  _ib.menu = null;
  menu.el.remove();
  if (menu.anchor && document.contains(menu.anchor)) {
    menu.anchor.setAttribute('aria-expanded', 'false');
    if (restoreFocus) menu.anchor.focus({ preventScroll: true });
  }
}

// Presets plus "Pick a time…", which swaps the menu for a date field.
function ibxTimeMenu(anchor, label, pick, extra, mode) {
  const entries = ibxPresets().map(([name, d]) => ({ label: name, hint: ibxPresetHint(d), on: () => pick(d) }));
  entries.push({ label: 'Pick a time…', icon: 'calendar-blank', on: () => ibxPickTime(anchor, label, pick) });
  ibxMenu(anchor, entries.concat(extra || []), { label, align: 'auto' });
}

function ibxPickTime(anchor, label, pick) {
  const start = addDays(startOfDay(new Date()), 1);
  start.setHours(9);
  const m = ibxMenu(anchor, [{ html:
    '<form class="ibx-pick" onsubmit="event.preventDefault()">' +
      '<label class="form-label" for="ibx-pick-at">' + escHtml(label) + '</label>' +
      '<input class="form-input" type="datetime-local" id="ibx-pick-at" value="' + localInputValue(start) +
        '" min="' + localInputValue(new Date()) + '">' +
      '<div class="ibx-pick-acts"><button type="button" class="btn btn-secondary btn-sm" data-act="cancel">Cancel</button>' +
      '<button type="submit" class="btn btn-primary btn-sm" data-act="ok">Set</button></div></form>' }]);
  m.querySelector('form').addEventListener('submit', () => {
    const v = m.querySelector('input').value;
    const d = v ? new Date(v) : null;
    if (!d || isNaN(d) || d <= new Date()) { showToast('Pick a time in the future.', 'error'); return; }
    ibxCloseMenu(true);
    pick(d);
  });
  m.querySelector('[data-act="cancel"]').addEventListener('click', () => ibxCloseMenu(true));
}

document.addEventListener('mousedown', e => {
  if (_ib.menu && !_ib.menu.el.contains(e.target) && !_ib.menu.anchor.contains(e.target)) ibxCloseMenu();
});
window.addEventListener('resize', () => {
  if (_ib.menu) ibxCloseMenu();
  if (currentTab === 'inbox' && !ibxPhone() && _ib.phoneThread) ibxPhoneThread(false);
});

// ── Phone: the list, then the thread with a back button in the top bar ──

function ibxPhoneThread(on) {
  _ib.phoneThread = !!on;
  document.body.classList.toggle('ibx-phone-thread', _ib.phoneThread);
  const crumb = document.getElementById('crumb');
  const bar = document.querySelector('.topbar');
  let top = document.getElementById('ibx-top');
  if (on && currentTab === 'inbox') {
    if (crumb) crumb.innerHTML = '<button class="ibx-back" onclick="ibxBack()">' + icon('caret-left') + 'Inbox</button>';
    if (!top && bar) { top = document.createElement('div'); top.id = 'ibx-top'; top.className = 'ibx-top'; bar.appendChild(top); }
    if (_ib.thread && top) top.innerHTML = ibxToolbar(_ib.thread, true);
  } else {
    if (top) top.remove();
    if (crumb && currentTab === 'inbox') crumb.innerHTML = icon('chat-circle-text') + '<span>Inbox</span>';
  }
}

function ibxBack() {
  ibxPhoneThread(false);
  ibxLoadList(true);
}

// ── Keyboard: J/K move, / searches, A approves, Escape closes a menu ──

function ibxMove(dir) {
  const items = ((_ib.list || {}).items || []);
  if (!items.length) return;
  const i = items.findIndex(x => x.id === _ib.sel);
  const next = items[Math.max(0, Math.min(items.length - 1, i < 0 ? 0 : i + dir))];
  if (!next || next.id === _ib.sel) {
    if (i === items.length - 1 && dir > 0 && _ib.list.next_offset != null) ibxPage(1);
    return;
  }
  ibxSelect(next.id);
  const row = document.querySelector('.ibx-item[data-id="' + CSS.escape(next.id) + '"]');
  if (row) row.scrollIntoView({ block: 'nearest' });
}

document.addEventListener('keydown', e => {
  if (currentTab !== 'inbox' || promptOpen() || modalOpen() || drawerOpen()) return;
  if (e.key === 'Escape' && _ib.menu) { e.preventDefault(); ibxCloseMenu(true); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (e.target && e.target.tagName) || '';
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable);
  if (typing) {
    // Escape clears the search first, then leaves the field.
    if (e.key === 'Escape' && e.target.id === 'ibx-q') {
      e.preventDefault();
      if (e.target.value) { e.target.value = ''; ibxSearch(''); } else e.target.blur();
    }
    return;
  }
  if (_ib.menu) return;
  const k = e.key.toLowerCase();
  if (e.key === '/') { e.preventDefault(); const q = ibxEl('ibx-q'); if (q) { q.focus(); q.select(); } }
  else if (k === 'j') { e.preventDefault(); ibxMove(1); }
  else if (k === 'k') { e.preventDefault(); ibxMove(-1); }
  else if (k === 'a' && _ib.thread && _ib.thread.compose.allowed) {
    const btn = document.querySelector('#ibx-composer .ibx-approve');
    if (btn && !btn.disabled) { e.preventDefault(); ibxApprove(); }
  }
});
