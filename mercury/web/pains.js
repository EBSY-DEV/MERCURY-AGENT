/* Pains: the second view of the Signals tab.
   Mercury proposes the problems it can write about, you decide which are true.
   Uses the shared helpers in app.js (api, escHtml, toneBadge, openDrawer,
   promptModal, showToast, ...). The list lives in #signals-view-pains; the
   editor is the shared right-hand drawer. */

// Two icons the shared set does not carry yet (Phosphor regular, same source
// as icons.js). Registered here so this screen does not touch icons.js.
Object.assign(PH, {
  'link-simple': '<path d="M165.66,90.34a8,8,0,0,1,0,11.32l-64,64a8,8,0,0,1-11.32-11.32l64-64A8,8,0,0,1,165.66,90.34ZM215.6,40.4a56,56,0,0,0-79.2,0L106.34,70.45a8,8,0,0,0,11.32,11.32l30.06-30a40,40,0,0,1,56.57,56.56l-30.07,30.06a8,8,0,0,0,11.31,11.32L215.6,119.6a56,56,0,0,0,0-79.2ZM138.34,174.22l-30.06,30.06a40,40,0,1,1-56.56-56.57l30.05-30.05a8,8,0,0,0-11.32-11.32L40.4,136.4a56,56,0,0,0,79.2,79.2l30.06-30.07a8,8,0,0,0-11.32-11.31Z"/>',
  'note': '<path d="M88,96a8,8,0,0,1,8-8h64a8,8,0,0,1,0,16H96A8,8,0,0,1,88,96Zm8,40h64a8,8,0,0,0,0-16H96a8,8,0,0,0,0,16Zm32,16H96a8,8,0,0,0,0,16h32a8,8,0,0,0,0-16ZM224,48V156.69A15.86,15.86,0,0,1,219.31,168L168,219.31A15.86,15.86,0,0,1,156.69,224H48a16,16,0,0,1-16-16V48A16,16,0,0,1,48,32H208A16,16,0,0,1,224,48ZM48,208H152V160a8,8,0,0,1,8-8h48V48H48Zm120-40v28.7L196.69,168Z"/>',
});

const _pn = {
  view: 'signals',      // 'signals' | 'pains', kept for as long as the browser tab is open
  data: null,           // last /api/pains payload
  filter: 'all',        // all | proposed | confirmed | rejected
  market: '',
  sel: '',              // code of the pain open in the drawer
  d: null,              // the drawer's state, see pnOpen()
};

const PN_FIELDS = ['owner_words', 'scene', 'cost', 'market', 'sector', 'offer_key', 'signal_codes', 'evidence'];
const PN_FILTERS = [['all', 'All'], ['proposed', 'Waiting on you'], ['confirmed', 'Confirmed'], ['rejected', 'Rejected']];

try { _pn.view = sessionStorage.getItem('mercury-signals-view') === 'pains' ? 'pains' : 'signals'; } catch { /* ignore */ }

// ── Small helpers ──

function pnDay(s) {
  const d = parseUTC(s);
  return d ? d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) : '';
}

function pnQuote(s) {
  return '“' + String(s || '').trim().replace(/^["“”]+|["“”]+$/g, '') + '”';
}

function pnWords(p) { return p.owner_words || p.label || p.code; }

function pnSentence(s) {
  s = String(s || '').trim();
  return s && !/[.!?]$/.test(s) ? s + '.' : s;
}

function pnPlural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

function pnIsLink(s) {
  return /^(https?:\/\/|www\.)\S/i.test(s) || /^[a-z0-9-]+(\.[a-z0-9-]+)+(\/|\s|,|$)/i.test(s);
}

function pnPain(code) {
  return ((_pn.data && _pn.data.pains) || []).find(p => p.code === code) || null;
}

// POST/GET that keeps the API's error code, which api() throws away.
async function pnCall(path, body) {
  try {
    const r = await fetch(path, body === undefined ? undefined : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch { /* not JSON */ }
    data = data || {};
    return {ok: r.ok && data.success !== false, status: r.status, data: data};
  } catch {
    return {ok: false, status: 0, data: {}, offline: true};
  }
}

function pnErrorText(r) {
  if (r.offline) return 'Could not reach Mercury. Check that the dashboard is still running and try again.';
  const d = r.data || {};
  if (Array.isArray(d.detail) && d.detail.length) {
    const first = d.detail[0];
    const field = (first.loc || []).filter(x => x !== 'body').join(' ');
    return 'Check the fields and try again' + (field ? ' (' + field + ': ' + first.msg + ')' : '') + '.';
  }
  return d.message || 'Something went wrong. Try again.';
}

// ── The Signals page: a view switch over the two lists ──

async function loadSignalsPage() {
  pnRenderSwitch();
  pnApplyView();
  await Promise.all([loadSignals(), loadPains()]);
  pnRenderSwitch();
}

function pnSignalCount() {
  const groups = (_signals && _signals.groups) || [];
  return groups.reduce((n, g) => n + g.signals.length, 0);
}

function pnRenderSwitch() {
  const el = document.getElementById('signals-views');
  if (!el) return;
  const pains = _pn.data && _pn.data.summary ? _pn.data.summary.total : null;
  const btn = (key, label, n) => '<button type="button" data-view="' + key + '" aria-pressed="' + (_pn.view === key) +
    '" class="' + (_pn.view === key ? 'on' : '') + '">' + label +
    (n === null ? '' : '<span class="pn-n">' + n + '</span>') + '</button>';
  el.innerHTML = '<div class="segmented pn-views" role="group" aria-label="What to review">' +
    btn('signals', 'Signals', _signals ? pnSignalCount() : null) + btn('pains', 'Pains', pains) + '</div>';
}

function pnApplyView() {
  document.getElementById('signals-view-signals').hidden = _pn.view !== 'signals';
  document.getElementById('signals-view-pains').hidden = _pn.view !== 'pains';
}

function pnSetView(view) {
  if (view === _pn.view) return;
  _pn.view = view;
  try { sessionStorage.setItem('mercury-signals-view', view); } catch { /* ignore */ }
  closeDrawer();
  pnRenderSwitch();
  pnApplyView();
}

// ── Loading and the list ──

async function loadPains() {
  const el = document.getElementById('signals-view-pains');
  const r = await pnCall('/api/pains');
  if (!r.ok || !r.data.pains) {
    _pn.data = null;
    el.innerHTML = offlineState();
    return;
  }
  _pn.data = r.data;
  pnRender();
}

function pnMarkets() {
  const names = ((_pn.data && _pn.data.markets) || []).slice();
  const seen = new Set(names.map(n => n.toLowerCase()));
  for (const p of (_pn.data && _pn.data.pains) || []) {
    if (p.market && !seen.has(p.market.toLowerCase())) { seen.add(p.market.toLowerCase()); names.push(p.market); }
  }
  return names;
}

// Pains in the chosen market, plus the ones that apply to any market.
function pnInMarket() {
  const m = _pn.market.toLowerCase();
  return ((_pn.data && _pn.data.pains) || []).filter(p => !m || !p.market || p.market.toLowerCase() === m);
}

function pnRender() {
  const el = document.getElementById('signals-view-pains');
  if (!_pn.data) return;
  const s = _pn.data.summary || {};
  const scoped = pnInMarket();
  const count = key => key === 'all' ? scoped.length : scoped.filter(p => p.status === key).length;
  const rows = scoped.filter(p => _pn.filter === 'all' || p.status === _pn.filter)
    .sort((a, b) => (a.status === 'rejected') - (b.status === 'rejected'));

  const kpi = (cls, n, label) => '<div class="sig-stat ' + cls + '"><div class="n">' + (n || 0) +
    '</div><div class="k">' + label + '</div></div>';
  const markets = pnMarkets();

  el.innerHTML =
    '<div class="sig-summary">' + kpi('confirmed', s.confirmed, 'Confirmed') +
      kpi('proposed', s.proposed, 'Waiting on you') + kpi('rejected', s.rejected, 'Rejected') + '</div>' +
    '<div class="toolbar pn-toolbar">' +
      '<div class="segmented pn-filter" role="group" aria-label="Show pains">' +
        PN_FILTERS.map(([key, label]) => '<button type="button" data-filter="' + key + '" aria-pressed="' +
          (_pn.filter === key) + '" class="' + (_pn.filter === key ? 'on' : '') + '">' + label +
          '<span class="pn-n">' + count(key) + '</span></button>').join('') + '</div>' +
      '<label class="pn-select"><span>Market:</span><select data-market aria-label="Market">' +
        '<option value="">All</option>' + markets.map(m => '<option value="' + escAttr(m) + '"' +
          (m.toLowerCase() === _pn.market.toLowerCase() ? ' selected' : '') + '>' + escHtml(m) + '</option>').join('') +
      '</select>' + icon('caret-down') + '</label>' +
      '<span class="pn-spacer"></span>' +
      '<button type="button" class="btn btn-secondary" data-act="add">' + icon('plus') + 'Add a pain</button>' +
    '</div>' +
    '<section class="panel pn-list">' +
      (rows.length ? rows.map(pnRow).join('') : pnEmpty(s.total || 0)) +
      '<div class="panel-foot">Each email uses one confirmed pain that fits the business. Rejected pains are listed to ' +
        'the Writer as never use, and training will not propose them again.</div>' +
    '</section>';
}

function pnEmpty(total) {
  return total
    ? emptyState('funnel', 'No pains match', 'Try another status or market.')
    : emptyState('sparkle', 'No pains yet', 'Training proposes the problems it finds on your product pages. You can also add one by hand.');
}

function pnSource(p) {
  return p.source === 'manual' ? 'Added by you' : 'Proposed by training';
}

function pnRow(p) {
  const evidence = (p.evidence || []).length;
  const scene = [pnSentence(p.scene), p.cost ? 'Cost: ' + pnSentence(p.cost) : ''].filter(Boolean).join(' ');
  const where = [p.market || 'Any market', p.sector].filter(Boolean).map(escHtml).join(' · ');
  const meta = ['<span>' + where + '</span>'];
  if (p.offer_key) meta.push('<span>answered by <span class="mono">' + escHtml(p.offer_key) + '</span></span>');
  if ((p.signal_codes || []).length) meta.push('<span>when <span class="mono">' + escHtml(p.signal_codes.join(', ')) + '</span></span>');
  if (evidence) meta.push('<button type="button" class="link-btn pn-src" data-act="evidence" data-code="' + escAttr(p.code) + '">' +
    pnPlural(evidence, 'source', 'sources') + '</button>');

  let side;
  if (p.status === 'proposed') {
    side = '<div class="pn-btns"><button type="button" class="btn btn-primary btn-sm" data-act="confirm" data-code="' + escAttr(p.code) + '">Confirm</button>' +
      '<button type="button" class="btn btn-secondary btn-sm" data-act="reject" data-code="' + escAttr(p.code) + '">Reject</button></div>' +
      toneBadge('waiting', pnSource(p) + ', ' + pnDay(p.created_at));
  } else if (p.status === 'confirmed') {
    const st = p.stats || {};
    side = toneBadge('good', 'Confirmed') +
      '<span class="pn-stats mono">' + st.sends + ' sent · ' + pnPlural(st.replies, 'reply', 'replies') + '</span>' +
      '<span class="pn-note">Confirmed by you, ' + escHtml(pnDay(p.status_at)) + '</span>';
  } else {
    side = toneBadge('bad', 'Rejected') +
      '<span class="pn-note">Rejected by you, ' + escHtml(pnDay(p.status_at)) + (p.status_note ? ': ' + escHtml(p.status_note) : '') + '</span>' +
      '<button type="button" class="link-btn pn-restore" data-act="restore" data-code="' + escAttr(p.code) + '">Restore</button>';
  }

  return '<div class="pn-row ' + escAttr(p.status) + (p.code === _pn.sel ? ' sel' : '') + '" data-code="' + escAttr(p.code) + '">' +
    '<div class="pn-main">' +
      '<div class="pn-title"><button type="button" class="pn-open" data-act="open" data-code="' + escAttr(p.code) + '">' +
        escHtml(pnQuote(pnWords(p))) + '</button><span class="pn-code">' + escHtml(p.code) + '</span></div>' +
      (scene ? '<div class="pn-scene">' + escHtml(scene) + '</div>' : '') +
      '<div class="pn-meta">' + meta.join('<span class="pn-dot" aria-hidden="true">·</span>') + '</div>' +
    '</div>' +
    '<div class="pn-side">' + side + '</div>' +
  '</div>';
}

// ── Deciding ──

async function pnDecide(code, status, note, quiet) {
  const p = pnPain(code);
  const revisions = p ? {[code]: p.revision} : {};
  const r = await pnCall('/api/pains/status', {code: code, status: status, note: note || '', revisions: revisions});
  if (!r.ok) { showToast(pnErrorText(r), 'error'); return false; }
  if ((r.data.stale || []).includes(code)) {
    showToast('That pain changed while you were looking at it. The list is up to date now.', 'error');
    await loadPains();
    return false;
  }
  if (!quiet) showToast({
    confirmed: 'Confirmed. The Writer may use this pain now.',
    rejected: 'Rejected. The Writer will never use it.',
    proposed: 'Restored. It is waiting on you again.',
  }[status], 'success');
  await loadPains();
  return true;
}

// Rejecting asks for an optional reason, which the list shows next to the pain.
function pnAskReject(code) {
  const p = pnPain(code);
  return promptModal({
    title: 'Reject this pain?',
    copy: (p ? pnQuote(pnWords(p)) + ' ' : '') + 'The Writer will be told never to use it, and training will not propose it again. ' +
      'A note helps you remember why.',
    placeholder: 'Too generic, not true for our customers...',
    ok: 'Reject', required: false,
  });
}

async function pnReject(code) {
  const note = await pnAskReject(code);
  if (note === null) return false;
  return pnDecide(code, 'rejected', note);
}

// ── Click handling for the list ──

document.addEventListener('click', e => {
  const sw = e.target.closest('#signals-views [data-view]');
  if (sw) { pnSetView(sw.dataset.view); return; }

  const root = document.getElementById('signals-view-pains');
  if (!root || !root.contains(e.target)) return;

  const filter = e.target.closest('[data-filter]');
  if (filter) { _pn.filter = filter.dataset.filter; pnRender(); return; }

  const act = e.target.closest('[data-act]');
  if (act) {
    const code = act.dataset.code;
    switch (act.dataset.act) {
      case 'add': pnOpenNew(); break;
      case 'open': pnOpen(code); break;
      case 'evidence': pnOpen(code, true); break;
      case 'confirm': pnDecide(code, 'confirmed'); break;
      case 'reject': pnReject(code); break;
      case 'restore': pnDecide(code, 'proposed'); break;
    }
    return;
  }
  const row = e.target.closest('.pn-row');
  if (row && !e.target.closest('a, button, select')) pnOpen(row.dataset.code);
});

document.addEventListener('change', e => {
  if (e.target.matches('#signals-view-pains [data-market]')) { _pn.market = e.target.value; pnRender(); }
});

// ── The drawer: read, edit, decide ──

function pnFieldsOf(p) {
  return {
    owner_words: pnWords(p) === p.code ? '' : pnWords(p), scene: p.scene || '', cost: p.cost || '',
    market: p.market || '', sector: p.sector || '', offer_key: p.offer_key || '',
    signal_codes: (p.signal_codes || []).slice(), evidence: (p.evidence || []).slice(),
  };
}

function pnClone(f) { return JSON.parse(JSON.stringify(f)); }

function pnOpen(code, toEvidence) {
  const p = pnPain(code);
  if (!p) return;
  _pn.sel = code;
  _pn.d = {mode: 'edit', pain: p, orig: pnFieldsOf(p), draft: pnClone(pnFieldsOf(p)), notice: null, evOpen: false, busy: false};
  pnDrawerRender(true);
  pnMarkSelected();
  if (toEvidence) {
    const ev = document.getElementById('pn-evidence');
    if (ev) ev.scrollIntoView({block: 'center'});
  }
}

function pnOpenNew() {
  const blank = {owner_words: '', scene: '', cost: '', market: _pn.market, sector: '', offer_key: '', signal_codes: [], evidence: []};
  _pn.sel = '';
  _pn.d = {mode: 'new', pain: null, orig: pnClone(blank), draft: pnClone(blank), notice: null, evOpen: false, busy: false};
  pnDrawerRender(true);
  pnMarkSelected();
  const first = document.getElementById('pn-f-owner_words');
  if (first) first.focus();
}

function pnMarkSelected() {
  document.querySelectorAll('#signals-view-pains .pn-row').forEach(r => r.classList.toggle('sel', r.dataset.code === _pn.sel));
}

function pnOwned() { return !!_pn.d && drawerOpen() && _drawerCtx && _drawerCtx.type === 'pain'; }

// What the person changed, as the fields to send.
function pnDiff() {
  const d = _pn.d, out = {};
  for (const k of PN_FIELDS) {
    const a = d.draft[k], b = d.orig[k];
    if (Array.isArray(a)) {
      if (JSON.stringify(a) !== JSON.stringify(b)) out[k] = a;
    } else {
      // The API lower-cases a market and an offer key, so only a real change counts.
      const fold = k === 'market' || k === 'offer_key';
      const x = String(a).trim(), y = String(b).trim();
      if (fold ? x.toLowerCase() !== y.toLowerCase() : x !== y) out[k] = x;
    }
  }
  return out;
}

function pnDrawerTitleParts() {
  const d = _pn.d;
  if (d.mode === 'new') return ['New pain', 'Add a pain', 'Write it the way the owner would say it. You can confirm it now or leave it waiting.'];
  const p = d.pain, m = statusMeta(p.status);
  let sub;
  if (p.status === 'proposed') sub = pnSource(p) + ' on ' + pnDay(p.created_at) + '. Nothing writes from it until you confirm.';
  else if (p.status === 'confirmed') sub = 'Confirmed by you on ' + pnDay(p.status_at) + '. The Writer may use it.';
  else sub = 'Rejected by you on ' + pnDay(p.status_at) + (p.status_note ? ': ' + p.status_note : '') + '. The Writer is told never to use it.';
  return [m.label + ' · ' + p.code, pnQuote(d.draft.owner_words || pnWords(p)), sub];
}

function pnDrawerRender(first) {
  const [kicker, title, sub] = pnDrawerTitleParts();
  const html = pnDrawerBody();
  if (first) { openDrawer(kicker, title, escHtml(sub), html); _drawerCtx = {type: 'pain'}; }
  else updateDrawer(kicker, title, escHtml(sub), html);
}

function pnNotice() {
  const n = _pn.d.notice;
  if (!n) return '';
  if (n.kind === 'stale') {
    return '<div class="pn-notice" role="alert">' + toneBadge('waiting', 'Changed elsewhere') +
      '<span>This pain was changed after you opened it. Your edits are still here and nothing was saved.</span>' +
      '<button type="button" class="link-btn pn-reload" data-pn="reload">Reload the saved version</button></div>';
  }
  return '<div class="pn-notice" role="alert">' + toneBadge('bad', n.title || 'Not saved') + '<span>' + escHtml(n.text) + '</span>' +
    (n.matched ? '<button type="button" class="link-btn pn-reload" data-pn="open-matched" data-code="' + escAttr(n.matched) + '">Open ' + escHtml(n.matched) + '</button>' : '') +
    '</div>';
}

function pnInput(id, label, value, opts) {
  opts = opts || {};
  const attrs = ' class="form-input" id="pn-f-' + id + '" data-f="' + id + '" maxlength="' + opts.max + '" autocomplete="off"';
  const control = opts.rows
    ? '<textarea' + attrs + ' rows="' + opts.rows + '">' + escHtml(value) + '</textarea>'
    : '<input' + attrs + ' value="' + escAttr(value) + '">';
  return '<div class="form-group"><label class="form-label" for="pn-f-' + id + '">' + label + '</label>' + control + '</div>';
}

function pnSelect(id, label, value, options, anyLabel) {
  const known = options.some(o => o.toLowerCase() === value.toLowerCase());
  const all = known || !value ? options : [value].concat(options);
  return '<div class="form-group"><label class="form-label" for="pn-f-' + id + '">' + label + '</label>' +
    '<select class="form-input" id="pn-f-' + id + '" data-f="' + id + '"><option value="">' + anyLabel + '</option>' +
    all.map(o => '<option value="' + escAttr(o) + '"' + (o.toLowerCase() === value.toLowerCase() ? ' selected' : '') + '>' + escHtml(o) + '</option>').join('') +
    '</select></div>';
}

function pnDrawerBody() {
  const d = _pn.d, f = d.draft;
  const confirmedSignals = ((_pn.data && _pn.data.signals) || []).filter(s => s.status === 'confirmed').map(s => s.code)
    .filter(c => !f.signal_codes.includes(c));
  const offers = (_pn.data && _pn.data.offers) || [];

  const signals = '<div class="pn-signals" id="pn-f-signal_codes">' +
    f.signal_codes.map(c => '<span class="pn-sig"><span class="mono">' + escHtml(c) + '</span>' +
      '<button type="button" class="pn-x" data-pn="signal-off" data-code="' + escAttr(c) + '" aria-label="Remove ' + escAttr(c) + '">' + icon('x') + '</button></span>').join('') +
    (confirmedSignals.length
      ? '<select class="pn-sig-add" data-pn="signal-add" aria-label="Add a confirmed signal"><option value="">Add a confirmed signal</option>' +
        confirmedSignals.map(c => '<option value="' + escAttr(c) + '">' + escHtml(c) + '</option>').join('') + '</select>'
      : '<span class="pn-sig-none">' + (f.signal_codes.length ? 'All confirmed signals are added' : 'No confirmed signals yet') + '</span>') +
    '</div>';

  const evidence = f.evidence.map((e, i) => '<div class="pn-ev">' + icon(pnIsLink(e) ? 'link-simple' : 'note') +
    (/^https?:\/\/\S+$/i.test(e) ? '<a href="' + escAttr(e) + '" target="_blank" rel="noopener noreferrer">' + escHtml(e) + '</a>' : '<span>' + escHtml(e) + '</span>') +
    '<button type="button" class="pn-x" data-pn="evidence-off" data-i="' + i + '" aria-label="Remove this evidence">' + icon('x') + '</button></div>').join('') +
    (d.evOpen
      ? '<div class="pn-ev-add"><label class="sr-only" for="pn-ev-new">Evidence</label>' +
        '<input class="form-input" id="pn-ev-new" maxlength="300" placeholder="A link, or a note about where you saw it" autocomplete="off">' +
        '<button type="button" class="btn btn-secondary btn-sm" data-pn="evidence-add">Add</button>' +
        '<button type="button" class="btn btn-secondary btn-sm" data-pn="evidence-cancel">Cancel</button></div>'
      : (f.evidence.length < 10 ? '<button type="button" class="link-btn pn-add-ev" data-pn="evidence-open">Add evidence</button>' : ''));

  let foot;
  const dirty = Object.keys(pnDiff()).length > 0;
  const busy = d.busy ? ' disabled' : '';
  if (d.mode === 'new') {
    foot = '<button type="button" class="btn btn-primary" data-pn="create-confirm"' + busy + '>Add and confirm</button>' +
      '<button type="button" class="btn btn-secondary" data-pn="create"' + busy + '>Add for later</button>';
  } else {
    const st = d.pain.status;
    foot = (st === 'proposed' ? '<button type="button" class="btn btn-primary" data-pn="confirm"' + busy + '>Confirm</button>' : '') +
      (st === 'rejected'
        ? '<button type="button" class="btn btn-secondary" data-pn="restore"' + busy + '>Restore</button>'
        : '<button type="button" class="btn btn-secondary" data-pn="reject"' + busy + '>Reject</button>') +
      '<span class="pn-spacer"></span>' +
      '<button type="button" class="btn btn-secondary" data-pn="save" id="pn-save"' + (dirty && !d.busy ? '' : ' disabled') + '>Save edits</button>';
  }

  return '<div class="pn-wrap">' + pnNotice() +
    '<section class="drawer-section pn-sec"><h4>In the owner’s words</h4>' +
      pnInput('owner_words', 'What they would say', f.owner_words, {max: 600}) +
      pnInput('scene', 'The scene, in one or two lines', f.scene, {max: 400, rows: 2}) +
      pnInput('cost', 'What it costs them', f.cost, {max: 300}) +
    '</section>' +
    '<section class="drawer-section pn-sec"><h4>Where it applies</h4>' +
      '<div class="pn-two">' + pnSelect('market', 'Market', f.market, pnMarkets(), 'Any market') +
        pnInput('sector', 'Trade', f.sector, {max: 80}) + '</div>' +
      '<div class="form-group"><span class="form-label">Use it when the business has</span>' + signals + '</div>' +
      pnSelect('offer_key', 'Answered by offer', f.offer_key, offers, 'Any offer') +
    '</section>' +
    '<section class="drawer-section pn-sec" id="pn-evidence"><h4>Evidence</h4>' + evidence + '</section>' +
    '<div class="pn-foot">' + foot + '</div>' +
  '</div>';
}

// ── Drawer events ──

function pnSyncSave() {
  const b = document.getElementById('pn-save');
  if (b && _pn.d) b.disabled = _pn.d.busy || !Object.keys(pnDiff()).length;
}

function pnRetitle() {
  // The title follows the words being typed.
  if (!_pn.d) return;
  const [, title] = pnDrawerTitleParts();
  document.getElementById('drawer-title').textContent = title;
}

document.addEventListener('input', e => {
  if (!pnOwned() || !e.target.dataset || !e.target.dataset.f) return;
  _pn.d.draft[e.target.dataset.f] = e.target.value;
  pnSyncSave();
  if (e.target.dataset.f === 'owner_words') pnRetitle();
});

document.addEventListener('change', e => {
  if (!pnOwned()) return;
  const t = e.target;
  if (t.dataset && t.dataset.f && t.tagName === 'SELECT') {
    _pn.d.draft[t.dataset.f] = t.value;
    pnSyncSave();
  } else if (t.dataset && t.dataset.pn === 'signal-add' && t.value) {
    _pn.d.draft.signal_codes.push(t.value);
    pnDrawerRender(false);
  }
});

document.addEventListener('keydown', e => {
  if (!pnOwned() || e.target.id !== 'pn-ev-new') return;
  if (e.key === 'Enter') { e.preventDefault(); pnAddEvidence(); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); _pn.d.evOpen = false; pnDrawerRender(false); }
}, true);

function pnAddEvidence() {
  const input = document.getElementById('pn-ev-new');
  const text = input ? input.value.trim() : '';
  const d = _pn.d;
  if (text && !d.draft.evidence.some(x => x.toLowerCase() === text.toLowerCase())) d.draft.evidence.push(text);
  d.evOpen = false;
  pnDrawerRender(false);
}

document.addEventListener('click', e => {
  if (!pnOwned()) return;
  const b = e.target.closest('#drawer-body [data-pn]');
  if (!b || b.tagName === 'SELECT') return;
  const d = _pn.d;
  switch (b.dataset.pn) {
    case 'signal-off': d.draft.signal_codes = d.draft.signal_codes.filter(c => c !== b.dataset.code); pnDrawerRender(false); break;
    case 'evidence-off': d.draft.evidence.splice(+b.dataset.i, 1); pnDrawerRender(false); break;
    case 'evidence-open': d.evOpen = true; pnDrawerRender(false); { const i = document.getElementById('pn-ev-new'); if (i) i.focus(); } break;
    case 'evidence-cancel': d.evOpen = false; pnDrawerRender(false); break;
    case 'evidence-add': pnAddEvidence(); break;
    case 'reload': pnReloadSaved(); break;
    case 'open-matched': pnOpen(b.dataset.code); break;
    case 'save': pnSave(); break;
    case 'confirm': pnConfirmFromDrawer(); break;
    case 'reject': pnRejectFromDrawer(); break;
    case 'restore': pnRestoreFromDrawer(); break;
    case 'create': pnCreate(false); break;
    case 'create-confirm': pnCreate(true); break;
  }
});

function pnBusy(on) {
  _pn.d.busy = on;
  pnDrawerRender(false);
}

// Show an API refusal in the drawer, keeping everything typed.
function pnFail(r) {
  const code = (r.data || {}).code;
  const d = _pn.d;
  if (code === 'stale_revision') d.notice = {kind: 'stale'};
  else if (code === 'matches_rejected') d.notice = {kind: 'error', title: 'Already rejected', text: pnErrorText(r), matched: r.data.matched};
  else if (code === 'duplicate') d.notice = {kind: 'error', title: 'Already in the library', text: 'A pain with these words is already in the library.', matched: r.data.matched};
  else d.notice = {kind: 'error', title: 'Not saved', text: pnErrorText(r)};
  d.busy = false;
  pnDrawerRender(false);
}

async function pnReloadSaved() {
  const d = _pn.d;
  const r = await pnCall('/api/pains/' + encodeURIComponent(d.pain.code));
  if (!r.ok) { pnFail(r); return; }
  d.pain = r.data; d.orig = pnFieldsOf(r.data); d.draft = pnClone(d.orig); d.notice = null;
  pnDrawerRender(false);
  loadPains();
}

// Save what changed. Returns true when the pain is saved (or nothing changed).
async function pnSave(quiet) {
  const d = _pn.d, changed = pnDiff();
  if (!Object.keys(changed).length) return true;
  d.notice = null;
  pnBusy(true);
  const r = await pnCall('/api/pains/' + encodeURIComponent(d.pain.code) + '/save',
    Object.assign({expected_revision: d.pain.revision}, changed));
  if (!r.ok) { pnFail(r); return false; }
  d.pain = r.data.pain; d.orig = pnFieldsOf(d.pain); d.draft = pnClone(d.orig); d.busy = false;
  pnDrawerRender(false);
  if (!quiet) showToast('Saved.', 'success');
  loadPains();
  return true;
}

async function pnCreate(confirm) {
  const d = _pn.d, f = d.draft;
  const words = f.owner_words.trim();
  if (!words) {
    d.notice = {kind: 'error', title: 'Not added', text: 'Write what the owner would say first.'};
    pnDrawerRender(false);
    const i = document.getElementById('pn-f-owner_words');
    if (i) i.focus();
    return;
  }
  d.notice = null;
  pnBusy(true);
  const r = await pnCall('/api/pains', {
    label: words.slice(0, 200), owner_words: words, scene: f.scene.trim(), cost: f.cost.trim(),
    market: f.market, sector: f.sector.trim(), offer_key: f.offer_key, signal_codes: f.signal_codes,
    evidence: f.evidence, confirm: confirm,
  });
  if (!r.ok) { pnFail(r); return; }
  showToast(confirm ? 'Added and confirmed. The Writer may use it now.' : 'Added. It is waiting on you.', 'success');
  closeDrawer();
  _pn.filter = 'all';
  await loadPains();
}

async function pnConfirmFromDrawer() {
  const code = _pn.d.pain.code;
  if (!(await pnSave(true))) return;
  pnBusy(true);
  await pnDecideFromDrawer(code, 'confirmed', '');
}

async function pnRejectFromDrawer() {
  const code = _pn.d.pain.code;
  const note = await pnAskReject(code);
  if (note === null) return;
  pnBusy(true);
  await pnDecideFromDrawer(code, 'rejected', note);
}

async function pnRestoreFromDrawer() {
  pnBusy(true);
  await pnDecideFromDrawer(_pn.d.pain.code, 'proposed', '');
}

// Decide from the drawer: a stale revision is shown here, not as a toast.
async function pnDecideFromDrawer(code, status, note) {
  const d = _pn.d;
  const r = await pnCall('/api/pains/status', {code: code, status: status, note: note, revisions: {[code]: d.pain.revision}});
  if (!r.ok) { pnFail(r); return false; }
  if ((r.data.stale || []).includes(code)) { pnFail({data: {code: 'stale_revision'}}); return false; }
  showToast({
    confirmed: 'Confirmed. The Writer may use this pain now.',
    rejected: 'Rejected. The Writer will never use it.',
    proposed: 'Restored. It is waiting on you again.',
  }[status], 'success');
  closeDrawer();
  await loadPains();
  return true;
}

// Clear the highlighted row when the drawer closes by any route.
new MutationObserver(() => {
  if (!drawerOpen() && _pn.sel) { _pn.sel = ''; _pn.d = null; pnMarkSelected(); }
}).observe(document.getElementById('drawer'), {attributes: true, attributeFilter: ['class']});
