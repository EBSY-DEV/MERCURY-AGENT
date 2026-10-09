// Contacts: the table with its public-registry column, and the contact drawer.
//
// The table is /api/prospects, filtered in the browser. Every contact carries a
// `registry` object (mercury/registry/contacts.py) that says what Mercury
// found in a public business filing and what a person has decided about it.
// The drawer shows the filing, lets a person use, dismiss or undo the one name
// Mercury suggests, and previews how the first email opens either way
// (/api/contacts/{id}/greeting-preview). Nothing is ever written onto the
// contact itself: a registry name is evidence, and only a name you choose is used.

const _ct = { q: '', filter: 'all', id: '', preview: null, busy: '', msg: null, token: 0 };

// ── Reading a contact ──

function ctName(p) {
  const last = String(p.last_name || '').toLowerCase() === 'team' ? '' : (p.last_name || '');
  return [p.first_name, last].filter(Boolean).join(' ');
}

function ctHasName(p) {
  const r = p.registry;
  return r ? !!r.has_own_name : !!ctName(p);
}

function ctNeeds(p) {
  return p.import_batch_id
    ? [['first name', p.first_name], ['last name', p.last_name], ['title', p.title]].filter(x => !x[1]).map(x => x[0])
    : [];
}

// The registry writes names in capitals; show them the way a person would.
const CT_SUFFIX = new Set(['LLC', 'LC', 'LLP', 'LP', 'PA', 'PLLC', 'II', 'III']);
function ctCase(name) {
  const text = String(name || '');
  if (text !== text.toUpperCase()) return text;
  return text.split(/(\s+)/).map(w => {
    const bare = w.replace(/[.,]/g, '');
    if (CT_SUFFIX.has(bare)) return w;
    return w.length > 1 && !/^[&]+$/.test(w) ? w.charAt(0) + w.slice(1).toLowerCase() : w;
  }).join('');
}

function ctPersonLine(person) {
  return person ? (person.title ? person.name + ', ' + person.title : person.name) : '';
}

function ctEmailBadge(p) {
  if (!p.email) return '';
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!STATUS[status]) return '';
  const m = statusMeta(status);
  return toneBadge(m.tone, m.label);
}

// The Registry column: a status word and one line of detail.
function ctRegistry(p) {
  const r = p.registry;
  if (!r) return { tone: 'idle', label: 'Not looked up yet', detail: '' };
  const own = r.has_own_name;
  switch (r.status) {
    case 'matched':
      if (r.name_status === 'registry_pending') return { tone: 'waiting', label: 'Review a name', detail: ctPersonLine(r.person) };
      if (r.name_status === 'registry') return { tone: 'good', label: 'Name chosen', detail: ctPersonLine(r.person) };
      if (r.review === 'dismissed' && r.person && !own) return { tone: 'idle', label: 'Name dismissed', detail: ctPersonLine(r.person) };
      if (own) return { tone: 'good', label: 'Matched', detail: r.on_filing ? (r.on_filing.title ? r.on_filing.title + ', on the filing' : 'On the filing') : 'Filing found' };
      return { tone: 'good', label: 'Matched', detail: r.people.length > 1 ? 'Several people on file' : 'Filing found' };
    case 'ambiguous': {
      const n = (r.candidates || []).length;
      return { tone: own ? 'idle' : 'waiting', label: n > 1 ? n + ' possible filings' : 'Possible filings',
               detail: own ? '' : 'Pick one or skip' };
    }
    case 'no_match':
      return { tone: 'idle', label: 'No filing found', detail: r.reason === 'inactive_only' ? 'Only inactive filings' : '' };
    case 'not_eligible':
      return { tone: 'idle', label: 'Not covered',
               detail: (r.covered || []).length ? 'Outside ' + r.covered.join(' and ') : 'No registry here yet' };
    case 'not_looked_up':
      return { tone: 'idle', label: 'Not looked up yet', detail: '' };
    case 'unavailable':
      return { tone: 'idle', label: 'Registry unavailable', detail: 'Will try again' };
    default:
      return { tone: 'idle', label: statusMeta(r.status).label, detail: '' };
  }
}

function ctNeedsReview(p) {
  const r = p.registry;
  return !!r && (r.name_status === 'registry_pending' || (r.status === 'ambiguous' && !r.has_own_name));
}

const CT_FILTERS = [
  ['all', 'All', () => true],
  ['shared', 'Shared inboxes', p => !!(p.registry && p.registry.shared_inbox)],
  ['review', 'Registry to review', ctNeedsReview],
];

function ctMatches(p, q) {
  if (!q) return true;
  const hay = [ctName(p), p.title, p.company, p.email, p.source].join(' ').toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w));
}

// ── The table ──

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const bar = document.getElementById('contacts-bar');
  const data = await api('/api/prospects');
  if (!data) { bar.hidden = true; el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    bar.hidden = true;
    el.innerHTML = emptyState('address-book', 'No contacts yet',
      'Mercury hasn\'t found any prospects. Once it\'s running, the Scout agent searches the web for people matching your ideal customer profile in <b>mercury.yaml</b>.');
    return;
  }
  bar.hidden = false;
  renderContacts();
  if (_drawerCtx && _drawerCtx.type === 'contact') ctRedraw();
}

function ctSearch(value) { _ct.q = value; renderContacts(); }
function ctFilter(key) { _ct.filter = key; renderContacts(); }

function ctRow(p) {
  const info = ctRegistry(p);
  const has = ctHasName(p);
  const needs = ctNeeds(p);
  const shared = !!(p.registry && p.registry.shared_inbox);
  const sub = has ? p.title : (shared ? 'Shared inbox' : p.title);
  const source = p.import_batch_id
    ? '<span title="Import batch ' + escHtml(p.import_batch_id) + '">Import ' + escHtml(p.import_batch_id.slice(0, 6)) +
      ' · row ' + escHtml(String(p.import_row)) + '</span>'
    : escHtml(p.source);
  return '<tr class="ct-row' + (_ct.id === p.id ? ' sel' : '') + '" tabindex="0" data-id="' + escHtml(p.id) + '" ' +
      'onclick="ctOpen(this.dataset.id)" onkeydown="ctRowKey(event, this)">' +
    '<td class="ct-c-name"><div class="ct-l1' + (has ? '' : ' muted') + '">' + (has ? escHtml(ctName(p)) : 'No name yet') + '</div>' +
      (sub ? '<div class="ct-l2">' + escHtml(sub) + '</div>' : '') +
      (needs.length ? '<div class="ct-l2 imp-needs">needs ' + escHtml(needs.join(', ')) + '</div>' : '') + '</td>' +
    '<td class="ct-c-company">' + escHtml(p.company) + '</td>' +
    '<td class="ct-c-email">' + (p.email ? '<div class="mono ct-email">' + escHtml(p.email) + '</div><div class="ct-l2">' + ctEmailBadge(p) + '</div>' : '') + '</td>' +
    '<td class="ct-c-status">' + badge(p.status) + '</td>' +
    '<td class="ct-c-reg">' + toneBadge(info.tone, info.label) +
      (info.detail ? '<div class="ct-l2">' + escHtml(info.detail) + '</div>' : '') + '</td>' +
    '<td class="ct-c-source muted">' + source + '</td></tr>';
}

function renderContacts() {
  const el = document.getElementById('prospects-table');
  const all = _prospects;
  const search = document.getElementById('ct-search');
  search.placeholder = 'Search ' + all.length + ' contact' + (all.length === 1 ? '' : 's');
  document.getElementById('ct-filters').innerHTML = CT_FILTERS.map(([key, label, test]) =>
    '<button type="button" class="' + (_ct.filter === key ? 'on' : '') + '" aria-pressed="' + (_ct.filter === key) +
    '" onclick="ctFilter(\'' + key + '\')">' + label + ' <span class="ct-n">' + all.filter(test).length + '</span></button>').join('');

  const test = (CT_FILTERS.find(f => f[0] === _ct.filter) || CT_FILTERS[0])[2];
  const shown = all.filter(p => test(p) && ctMatches(p, _ct.q));
  const covered = (all.find(p => p.registry && (p.registry.covered || []).length) || { registry: { covered: ['Florida'] } }).registry.covered[0];
  const foot = '<div class="ct-foot">Registry names come from public state filings. ' + escHtml(covered) +
    ' is the first registry Mercury reads; contacts elsewhere show Not covered.</div>';

  if (!shown.length) {
    el.innerHTML = '<div class="table-card ct-table">' +
      '<div class="ct-none">' + (_ct.q ? 'No contacts match “' + escHtml(_ct.q) + '”.' : 'Nothing to review right now.') + '</div>' + foot + '</div>';
    return;
  }
  el.innerHTML = '<div class="table-card ct-table"><table><thead><tr><th>Name</th><th>Company</th><th>Email</th>' +
    '<th>Status</th><th>Registry</th><th>Source</th></tr></thead><tbody>' +
    shown.map(ctRow).join('') + '</tbody></table>' + foot + '</div>';
}

function ctRowKey(e, row) {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ctOpen(row.dataset.id); }
}

// ── The drawer ──

const ctById = id => _prospects.find(p => p.id === id);

async function ctFetchPreview(id) {
  const data = await api('/api/contacts/' + encodeURIComponent(id) + '/greeting-preview');
  return data && !data.detail ? data : null;
}

async function ctOpen(id) {
  const p = ctById(id);
  if (!p) return;
  const token = ++_ct.token;
  _ct.id = id; _ct.msg = null; _ct.busy = ''; _ct.preview = await ctFetchPreview(id);
  if (token !== _ct.token) return;
  _drawerCtx = { type: 'contact', id };
  ctMarkRow();
  const [kicker, title, sub, body] = ctDrawerContent(p);
  openDrawer(kicker, title, sub, body);
}

function ctDrawerClosed() {
  _ct.id = ''; _ct.token++;
  ctMarkRow();
}

function ctMarkRow() {
  document.querySelectorAll('#prospects-table .ct-row').forEach(r => r.classList.toggle('sel', r.dataset.id === _ct.id));
}

function ctRedraw() {
  const p = ctById(_ct.id);
  if (!p) { closeDrawer(); return; }
  const [kicker, title, sub, body] = ctDrawerContent(p);
  updateDrawer(kicker, title, sub, body);
}

function ctDate(ts) {
  if (!ts) return '';
  const d = new Date(String(ts).includes('T') ? ts : String(ts).replace(' ', 'T') + 'Z');
  return isNaN(d) ? '' : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function ctHost(url) {
  try { return new URL(url).hostname.replace(/^www\./, '').split('.').slice(-2).join('.'); } catch { return 'the registry'; }
}

function ctDrawerContent(p) {
  const r = p.registry || {};
  const has = ctHasName(p);
  const shared = !!r.shared_inbox;
  let sub;
  if (has) sub = escHtml(p.title || '');
  else if (r.name_status === 'registry') sub = 'Shared inbox. You chose ' + escHtml(ctPersonLine(r.person) || 'a name') + ' from the public filing.';
  else sub = shared ? 'Shared inbox. No person’s name on the website.' : 'No person’s name on the website.';
  return ['Contact · ' + (p.company || 'No company'), has ? ctName(p) : (p.email || 'No email'), sub,
    '<div class="ct-drawer">' + ctRegistrySection(p) + ctOpensSection(p) + ctContactSection(p) + '</div>'];
}

// "Public registry": the filing, its people, and what a person can do about them.
function ctRegistrySection(p) {
  const r = p.registry;
  if (!r) return '';
  return '<section class="drawer-section"><h4>Public registry</h4>' + ctFilingCard(p, r) + '</section>';
}

function ctMsg() {
  if (!_ct.msg) return '';
  return '<div class="ct-msg" role="status"><span class="badge t-' + _ct.msg.tone + '">' +
    icon(TONE_ICON[_ct.msg.tone]) + '<span>' + _ct.msg.html + '</span></span></div>';
}

function ctRefreshBtn(r, label) {
  if (!r.provider) return '';
  const busy = _ct.busy === 'refresh';
  return '<button type="button" class="ct-link" onclick="ctRefresh()"' + (_ct.busy ? ' disabled' : '') + '>' +
    (busy ? 'Looking up…' : label) + '</button>';
}

function ctFilingCard(p, r) {
  const looked = ctDate(r.looked_up_at);
  const foot = (link) => '<div class="ct-card-foot"><span class="ct-foot-l">' + link + '</span><span class="ct-foot-r">' +
    (looked ? '<span>Looked up ' + escHtml(looked) + '</span>' : '') + ctRefreshBtn(r, 'Refresh') + '</span></div>';
  const where = [r.jurisdiction_name, r.entity_type].filter(Boolean).join(' ');

  if (r.status === 'matched') {
    const meta = [where || 'Public filing', 'Active', r.city].filter(Boolean).join(' · ');
    const match = r.reason === 'name_and_city' ? 'Matched on name and city' : 'Matched';
    const link = r.source_url
      ? '<a href="' + escHtml(r.source_url) + '" target="_blank" rel="noopener">' + icon('link-simple') + 'View the filing on ' + escHtml(ctHost(r.source_url)) + '</a>'
      : '';
    const people = (r.people || []).map(person => ctPerson(p, r, person)).join('');
    const none = (r.people || []).length ? '' : '<div class="ct-state">The filing lists no individual people.</div>';
    const lead = !r.person && (r.people || []).length > 1 && !r.has_own_name && r.shared_inbox
      ? '<p class="ct-note">Several people are listed and none stands out as the lead, so Mercury does not suggest one.</p>' : '';
    return '<div class="ct-card"><div class="ct-card-head"><div class="ct-ent">' + escHtml(ctCase(r.entity_name) || 'Filing') + '</div>' +
      '<div class="ct-meta">' + escHtml(meta) + '</div>' +
      '<div class="ct-match" title="Confidence ' + escHtml(String(r.confidence || '')) + '">' + toneBadge('good', match) + '</div></div>' +
      people + none + foot(link) + '</div>' + ctMsg() + lead +
      '<p class="ct-note">A filing names who is legally responsible, not who reads this inbox or decides on purchases. Mercury only uses a name you choose here.</p>';
  }

  let head, copy = '', list = '';
  switch (r.status) {
    case 'ambiguous': {
      const n = (r.candidates || []).length;
      head = n > 1 ? n + ' filings could be this business' : 'More than one filing could be this business';
      copy = r.reason === 'city_unknown'
        ? 'This company has no city on record, and a name alone is not enough to tell the filings apart.'
        : 'Mercury could not tell which filing is this company, so it uses none of them. Check them here, then look again later if the company changes.';
      list = (r.candidates || []).map(c => '<div class="ct-cand"><div><div class="ct-pn">' + escHtml(ctCase(c.name)) + '</div>' +
        '<div class="ct-pr">' + (c.document_number ? '<span class="mono">' + escHtml(c.document_number) + '</span>' : '') +
        (c.status ? ' · ' + escHtml(c.status) : '') + '</div></div>' +
        (c.url ? '<a class="ct-cand-link" href="' + escHtml(c.url) + '" target="_blank" rel="noopener">' + icon('link-simple') + 'View</a>' : '') + '</div>').join('');
      break;
    }
    case 'no_match':
      head = 'No filing found';
      copy = r.reason === 'inactive_only' ? 'Only inactive filings carry this name, and Mercury never reads those.'
        : r.reason === 'city_mismatch' ? 'Filings with this name exist, but none is in this company’s city.'
        : 'No active filing carries this business’s name.';
      break;
    case 'not_eligible':
      head = 'Not covered';
      copy = ((r.covered || []).length ? 'Mercury reads the ' + r.covered.join(' and ') + ' registry for now, and this company is outside it.'
        : 'No registry covers this place yet.') + ' Nothing is looked up here.';
      break;
    case 'unavailable':
      head = 'Registry unavailable';
      copy = 'The registry did not answer the last time Mercury asked' + (r.reason ? ' (' + r.reason + ')' : '') + '. It tries again after a day.';
      break;
    default:
      head = 'Not looked up yet';
      copy = 'Mercury reads the registry on its own once the Contact found signal is turned on. You can look this company up now.';
  }
  const canLook = r.provider && r.status !== 'not_eligible';
  const foot2 = canLook
    ? '<div class="ct-card-foot"><span class="ct-foot-l">' + escHtml(r.provider_label || '') + '</span><span class="ct-foot-r">' +
      (looked ? '<span>Looked up ' + escHtml(looked) + '</span>' : '') + ctRefreshBtn(r, r.status === 'not_looked_up' ? 'Look up now' : 'Refresh') + '</span></div>' : '';
  return '<div class="ct-card"><div class="ct-card-head"><div class="ct-ent">' + escHtml(head) + '</div>' +
    '<p class="ct-state">' + escHtml(copy) + '</p></div>' + list + foot2 + '</div>' + ctMsg();
}

function ctPerson(p, r, person) {
  const raw = person.raw_title ? ' <span class="mono ct-code">' + escHtml(person.raw_title) + '</span>' : '';
  const decidable = r.shared_inbox && !r.has_own_name;
  let act = '';
  if (r.has_own_name && r.on_filing && r.on_filing.name === person.name) {
    act = toneBadge('good', 'This contact');
  } else if (decidable && person.suggested) {
    const off = _ct.busy ? ' disabled' : '';
    if (r.name_status === 'registry_pending') {
      act = '<button type="button" class="btn btn-primary btn-sm" onclick="ctReview(\'accepted\')"' + off + '>Use this name</button>' +
        '<button type="button" class="btn btn-secondary btn-sm" onclick="ctReview(\'dismissed\')"' + off + '>Dismiss</button>';
    } else if (r.name_status === 'registry') {
      act = toneBadge('good', 'Using this name') +
        '<button type="button" class="btn btn-secondary btn-sm" onclick="ctReview(\'clear\')"' + off + '>Undo</button>';
    } else if (r.review === 'dismissed') {
      act = toneBadge('idle', 'Dismissed') +
        '<button type="button" class="btn btn-secondary btn-sm" onclick="ctReview(\'clear\')"' + off + '>Undo</button>';
    }
  } else if (decidable && r.person) {
    act = '<button type="button" class="btn btn-secondary btn-sm" disabled aria-disabled="true" ' +
      'title="Mercury can only use the one name it suggests for this inbox.">Use this name</button>';
  }
  return '<div class="ct-person"><span class="ct-avatar">' + icon('user') + '</span>' +
    '<div class="ct-pwho"><div class="ct-pn">' + escHtml(person.name) + '</div>' +
    '<div class="ct-pr">' + escHtml(person.title || 'Listed on the filing') + raw + '</div></div>' +
    (act ? '<div class="ct-pact">' + act + '</div>' : '') + '</div>';
}

// "How the first email opens": what each outcome would say, the current one marked.
function ctOpensSection(p) {
  const pv = _ct.preview;
  if (!pv) return '';
  const named = pv.mode === 'name';
  const boxes = [];
  if (pv.with_name) {
    boxes.push({ on: named, label: ctHasName(p) ? 'With their name' : 'With the name you choose', text: pv.with_name });
  }
  if (pv.shared_inbox || !named) {
    boxes.push({ on: !named, label: 'Without a name',
      text: pv.without_name || 'No greeting line. The email starts with its first sentence.' });
  }
  if (!boxes.length) return '';
  return '<section class="drawer-section"><h4>How the first email opens</h4>' +
    boxes.map(b => '<div class="ct-open' + (b.on ? ' on' : '') + '"><div class="ct-open-h">' +
      icon(b.on ? 'radio-button' : 'circle') + '<span>' + escHtml(b.label) + '</span>' +
      (b.on ? '<span class="sr-only"> (used now)</span>' : '') + '</div>' +
      '<div class="ct-open-t">' + escHtml(b.text) + '</div></div>').join('') +
    (pv.without_name ? '<p class="ct-note">The role in the second line comes from the campaign brief, so it changes with the offer.</p>' : '') +
    '</section>';
}

function ctContactSection(p) {
  const r = p.registry || {};
  const rows = [
    ['Email', p.email ? '<span class="mono">' + escHtml(p.email) + '</span> ' + ctEmailBadge(p) : '<span class="muted">None</span>'],
    ['Company', escHtml(p.company || '') + (r.company_location ? ' · ' + escHtml(r.company_location) : '')],
    ['Status', badge(p.status)],
  ];
  if (p.phone) rows.push(['Phone', '<span class="mono">' + escHtml(p.phone) + '</span>' +
    (p.phone_verified ? ' <span class="verified" title="verified">' + icon('check-circle') + '</span>' : '')]);
  rows.push(['Source', p.import_batch_id
    ? 'Import ' + escHtml(p.import_batch_id.slice(0, 6)) + ' · row ' + escHtml(String(p.import_row)) : escHtml(p.source || '')]);
  if (p.created_at) rows.push(['Added', formatDate(p.created_at)]);
  return '<section class="drawer-section"><h4>Contact</h4>' + facts(rows) +
    '<div class="drawer-actions"><button type="button" class="btn btn-secondary btn-sm" onclick="ctFeedback()">' +
    icon('chat-text') + 'Feedback</button></div></section>';
}

// ── Actions ──

async function ctSend(method, path, body) {
  try {
    const r = await fetch(path, { method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    let data = null;
    try { data = await r.json(); } catch { /* not JSON */ }
    if (r.ok) return { ok: true, data };
    const d = data && data.detail;
    return { ok: false, status: r.status,
      error: d && typeof d === 'object' && !Array.isArray(d) ? d : { code: 'http', message: 'Request failed (' + r.status + ').' } };
  } catch {
    return { ok: false, status: 0, error: { code: 'offline', message: 'Can’t reach the dashboard server.' } };
  }
}

async function ctAfter(id) {
  await loadProspects();
  _ct.preview = await ctFetchPreview(id);
  if (_ct.id === id) ctRedraw();
}

async function ctReview(decision) {
  const p = ctById(_ct.id);
  if (!p || _ct.busy) return;
  const id = p.id, who = p.registry && p.registry.person ? p.registry.person.name : 'this name';
  _ct.busy = 'review'; _ct.msg = null; ctRedraw();
  const res = await ctSend('POST', '/api/contacts/' + encodeURIComponent(id) + '/registry-name', { decision });
  _ct.busy = '';
  if (res.ok) {
    showToast(decision === 'accepted' ? 'Mercury will greet this inbox as ' + who.split(' ')[0] + '.'
      : decision === 'dismissed' ? 'Dismissed. Mercury will not use that name.' : 'Decision undone.', 'success');
  } else {
    _ct.msg = { tone: 'bad', html: escHtml(res.error.message || 'That did not save. Try again.') };
  }
  await ctAfter(id);
}

async function ctRefresh() {
  const p = ctById(_ct.id);
  if (!p || _ct.busy) return;
  const id = p.id;
  _ct.busy = 'refresh'; _ct.msg = null; ctRedraw();
  const res = await ctSend('POST', '/api/companies/' + encodeURIComponent(p.company_id) + '/registry/refresh');
  _ct.busy = '';
  if (!res.ok) {
    const e = res.error;
    _ct.msg = e.code === 'signal_not_confirmed'
      ? { tone: 'waiting', html: 'Looking up filings needs the Contact found signal, and it is not on yet. Turn it on in <a href="#" onclick="ctGoSignals(event)">Signals</a>.' }
      : res.status === 503 || e.code === 'offline'
        ? { tone: 'waiting', html: 'Mercury couldn’t reach the registry just now. Try again in a moment.' }
        : { tone: 'bad', html: escHtml(e.message || 'The lookup failed. Try again.') };
  } else if (res.data && res.data.status === 'unavailable') {
    _ct.msg = { tone: 'waiting', html: 'The registry did not answer, so nothing changed. Mercury will try again later.' };
  } else {
    showToast('Looked up again.', 'success');
  }
  await ctAfter(id);
}

function ctGoSignals(e) {
  e.preventDefault();
  showTab('signals', document.querySelector('.sidebar [data-tab="signals"]'));
}

function ctFeedback() {
  const p = ctById(_ct.id);
  if (p) submitFeedback('contact', p.id, 'Feedback on this contact:');
}
