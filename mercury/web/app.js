let currentTab = 'today';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.innerHTML = icon({light: 'sun', dark: 'moon'}[mode] || 'circle-half') +
    '<span class="lbl">Appearance</span><span class="side-meta">' + mode + '</span>';
}

function cycleTheme() {
  const now = localStorage.getItem('mercury-theme') || 'auto';
  const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
  try { localStorage.setItem('mercury-theme', next); } catch { /* private mode */ }
  applyTheme(next);
}

(function initTheme() {
  let saved = 'auto';
  try { saved = localStorage.getItem('mercury-theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
})();

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// ── One status vocabulary ──
//
// Mercury's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Five of those mean "waiting on you" and the old dashboard styled every one
// differently. Everything now resolves through this map, so a status reads the
// same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  // prospects
  new:            ['New', 'active'],
  contacted:      ['Contacted', 'active'],
  replied:        ['Replied', 'good'],
  interested:     ['Interested', 'good'],
  meeting:        ['Meeting booked', 'note'],
  not_interested: ['Not interested', 'bad'],
  bounced:        ['Bounced', 'bad'],
  // campaigns
  draft:          ['Draft', 'waiting'],
  active:         ['Active', 'good'],
  completed:      ['Completed', 'idle'],
  paused:         ['Paused', 'bad'],
  // conversations
  open:           ['Open', 'active'],
  closed:         ['Closed', 'idle'],
  closed_won:     ['Won', 'good'],
  closed_lost:    ['Lost', 'bad'],
  objection:      ['Objection', 'waiting'],
  // outbox
  pending_review: ['Waiting on you', 'waiting'],
  approved:       ['Approved', 'good'],
  scheduled:      ['Scheduled', 'active'],
  sent:           ['Sent', 'good'],
  failed:         ['Failed', 'bad'],
  rejected:       ['Rejected', 'idle'],
  cancelled:      ['Cancelled', 'idle'],
  // signals
  proposed:       ['Waiting on you', 'waiting'],
  confirmed:      ['Confirmed', 'good'],
  // email deliverability
  verified:       ['Verified', 'good'],
  risky:          ['Catch-all', 'waiting'],
  guess:          ['Unverified', 'idle'],
  invalid:        ['Invalid', 'bad'],
  // runs
  running:        ['Running', 'active'],
  stale:          ['Stale', 'bad'],
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const hit = STATUS[key];
  if (hit) return { label: hit[0], tone: hit[1] };
  const label = String(status || 'unknown').replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

const TONE_ICON = {
  good: 'check-circle', waiting: 'clock', bad: 'warning-circle',
  active: 'arrow-circle-right', idle: 'circle-dashed', note: 'info',
};

function toneBadge(tone, label) {
  return '<span class="badge t-' + tone + '">' + icon(TONE_ICON[tone] || 'circle-dashed') +
    escHtml(label) + '</span>';
}

function badge(status) {
  const m = statusMeta(status);
  return toneBadge(m.tone, m.label);
}

function formatDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

function emptyState(iconName, title, copy) {
  return '<div class="empty"><div class="glyph">' + icon(iconName) + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('warning', 'Dashboard can\'t reach the server',
    'The dashboard process may have stopped. Restart it with <b>mercury dashboard</b> and refresh this page.');
}

async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs ──

const TAB_META = {
  today: ['Today', 'squares-four'], signals: ['Signals', 'funnel'], discover: ['Discover', 'compass'],
  companies: ['Companies', 'buildings'], prospects: ['Contacts', 'address-book'],
  pipeline: ['Pipeline', 'kanban'], calendar: ['Calendar', 'calendar-blank'],
  campaigns: ['Campaigns', 'megaphone'], outbox: ['Outbox', 'tray'], warmup: ['Warm-up', 'fire'],
  conversations: ['Conversations', 'chat-circle-text'], activity: ['Activity', 'pulse'],
  usage: ['Usage', 'gauge'], settings: ['Settings', 'gear-six'], controls: ['Controls', 'power'],
  help: ['Help', 'question'],
};

function showTab(id, btn) {
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.sidebar [data-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === id));
  document.getElementById(id).classList.add('active');
  const meta = TAB_META[id] || [id, 'squares-four'];
  const crumb = document.getElementById('crumb');
  if (crumb) crumb.innerHTML = icon(meta[1]) + '<span>' + escHtml(meta[0]) + '</span>';
  document.title = meta[0] + ' · Mercury by EBSY';
  toggleSidebar(false);
  closeDrawer();
  closeMoveMenu();
  window.scrollTo(0, 0);
  loadCurrentTab();
}

function toggleSidebar(open) {
  const on = open === undefined ? !document.body.classList.contains('side-open') : open;
  document.body.classList.toggle('side-open', on);
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'today': loadToday(); loadSetupStatus(); loadRuns(); loadTodayActivity(); loadTrend(); loadHeatmap(); break;
    case 'help': break;
    case 'warmup': loadWarmup(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'pipeline': loadPipeline(); break;
    case 'calendar': loadCalendar(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); break;
    case 'controls': loadMercuryStatus(); loadLogs(); break;
  }
}

// ── Today: what needs a human ──

async function loadToday() {
  const data = await api('/api/today');
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  renderFigures(data.stats || {});

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div><span class="qstate">' + icon('check-circle') + 'All clear</span>' +
      '<div class="qtitle">Nothing needs you</div>' +
      '<div class="qdetail">Mercury has everything it needs. Anything that requires a ' +
      'decision — an email to approve, a signal to confirm — shows up here.</div>' +
      '</div></div>';
    return;
  }

  el.innerHTML = '<div class="queue">' + items.map(it =>
    '<div class="queue-item ' + escHtml(it.tone || 'warn') + '">' +
      '<div class="qtext">' +
        '<span class="qstate">' + ({bad: icon('warning') + 'Blocked', good: icon('arrow-right') + 'Next step'}[it.tone] ||
          icon('clock') + 'Needs you') + '</span>' +
        '<div class="qtitle">' + escHtml(it.title) + '</div>' +
        '<div class="qdetail">' + escHtml(it.detail) + '</div>' +
      '</div>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(\'' + escHtml(it.tab) + '\')">' +
        escHtml(it.action) + '</button>' +
    '</div>'
  ).join('') + '</div>';
}

function renderFigures(stats) {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const n = v => Number(v || 0);
  const fmt = v => n(v).toLocaleString('en-US');
  const kpi = (label, value, foot, tab) =>
    '<button class="kpi" onclick="goTab(\'' + tab + '\')">' +
      '<span class="kpi-label">' + label + '</span>' +
      '<span class="kpi-value' + (n(value) ? '' : ' zero') + '">' + fmt(value) + '</span>' +
      '<span class="kpi-foot">' + foot + '</span>' +
    '</button>';
  const unread = n(stats.unprofiled), sched = n(stats.outbox_approved), sig = n(stats.signals_confirmed);
  el.innerHTML =
    kpi('Companies', stats.companies,
        unread ? '<b>' + fmt(unread) + '</b> not yet read'
               : (n(stats.companies) ? 'All profiled' : 'Run a discovery to start'), 'companies') +
    kpi('Contacts', stats.prospects,
        '<b>' + sig + '</b> signal' + (sig === 1 ? '' : 's') + ' collected', 'prospects') +
    kpi('Awaiting approval', stats.outbox_pending,
        sched ? '<b>' + fmt(sched) + '</b> approved and scheduled' : 'Nothing scheduled', 'outbox') +
    kpi('Live conversations', stats.open_conversations,
        'Replies are classified automatically', 'conversations');
  renderFunnel(stats);
}

function renderFunnel(stats) {
  const el = document.getElementById('today-funnel');
  if (!el) return;
  const n = v => Math.max(0, Number(v || 0));
  const companies = n(stats.companies);
  const steps = [
    ['Found', companies, 'businesses discovered'],
    ['Profiled', Math.max(0, companies - n(stats.unprofiled)), 'websites read'],
    ['Contacts', n(stats.prospects), 'decision-makers found'],
    ['In outbox', n(stats.outbox_pending) + n(stats.outbox_approved), 'emails drafted'],
    ['Talking', n(stats.open_conversations), 'live conversations'],
  ];
  const max = Math.max(1, ...steps.map(s => s[1]));
  if (!steps.some(s => s[1])) {
    el.innerHTML = '<div class="funnel-empty">' + icon('compass') +
      '<div><b>No businesses yet.</b> Run a discovery source and the pipeline fills in here, ' +
      'stage by stage.</div><button class="btn btn-secondary btn-sm" onclick="goTab(\'discover\')">Find businesses</button></div>';
    return;
  }
  el.innerHTML = '<div class="funnel">' + steps.map(([label, v, sub]) =>
    '<div class="funnel-row">' +
      '<div class="funnel-k"><b>' + label + '</b><small>' + sub + '</small></div>' +
      '<div class="funnel-bar"><span style="width:' + (v ? Math.max(2, v / max * 100) : 0) + '%"></span></div>' +
      '<div class="funnel-v' + (v ? '' : ' zero') + '">' + v.toLocaleString('en-US') + '</div>' +
    '</div>').join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const data = await api('/api/activity');
  if (!Array.isArray(data) || !data.length) {
    el.innerHTML = '<div class="funnel-empty">' + icon('clock-counter-clockwise') +
      '<div><b>Nothing yet.</b> Every action Mercury takes shows up here.</div></div>';
    return;
  }
  el.innerHTML = '<div class="activity-feed">' + data.slice(0, 8).map(a =>
    '<div class="activity-item">' +
      '<span class="time">' + formatDate(a.created_at) + '</span>' +
      '<span class="agent">' + escHtml(a.agent) + '</span>' +
      '<span class="action">' + escHtml(String(a.action_type).replace(/_/g, ' ')) + '</span>' +
    '</div>').join('') + '</div>';
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? n : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id) {
  showTab(id);
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';

  // A rail is 300px wide — a seven-column table does not belong here.
  el.innerHTML = '<div class="figures">' + runs.slice(0, 6).map(r =>
    '<div class="figure" style="align-items:flex-start">' +
      '<span class="k">' + escHtml(r.stage) +
        '<br><span class="muted" style="font-size:11px">' +
        formatDate(r.started_at) + '</span></span>' +
      '<span style="text-align:right;flex:none">' + badge(r.status) +
        '<br><span class="muted" style="font-family:var(--mono);font-size:11px">' +
        (r.records || 0) + ' rec &middot; ' +
        (r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : 'free') + '</span></span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (lives on Today, and disappears once it's done) ──

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }

  const pct = data.percent || 0;
  // Finished setup disappears completely rather than greeting you forever.
  if (pct === 100) { el.innerHTML = ''; return; }

  const renderCheck = (c, optional) =>
    '<div class="check-item">' +
      (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
              : '<span class="check-icon pending"></span>') +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
          (optional ? '<span class="optional-tag">optional</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
      '</div></div>';

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);

  el.innerHTML =
    '<section class="panel">' +
      '<div class="panel-head"><div><h3>Setup</h3><p>' + pct + '% done. This card disappears when it hits 100.</p></div></div>' +
      '<div class="panel-body">' +
      '<div class="progress-bar" style="margin-bottom:10px">' +
        '<div class="progress-fill yellow" style="width:' + pct + '%"></div></div>' +
      required.map(c => renderCheck(c, false)).join('') +
      (optional.length
        ? '<details style="margin-top:10px"><summary class="muted" ' +
          'style="font-size:12px">' + optional.length + ' optional</summary>' +
          '<div style="margin-top:6px">' +
          optional.map(c => renderCheck(c, true)).join('') + '</div></details>'
        : '') +
    '</div></section>';
}

// ── Signals: Mercury proposes, you confirm ──

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  sumEl.innerHTML = '<div class="sig-summary">' +
    ['confirmed', 'proposed', 'rejected'].map(k =>
      '<div class="sig-stat ' + k + '"><div class="n">' + (s[k] || 0) + '</div>' +
      '<div class="k">' + (k === 'proposed' ? 'awaiting you' : k) + '</div></div>'
    ).join('') +
    '</div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    const codes = g.signals.map(x => x.code);
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
        '<h3>' + escHtml(g.label) + '</h3><p>' + escHtml(g.blurb) + '</p>' +
      '</div>' +
      (undecided
        ? '<div style="display:flex;gap:6px;flex-shrink:0">' +
            '<button class="btn btn-primary btn-sm" onclick=\'setSignals(' +
              JSON.stringify(codes) + ", \"confirmed\")'>Confirm all " + undecided + '</button>' +
            '<button class="btn btn-secondary btn-sm" onclick=\'setSignals(' +
              JSON.stringify(codes) + ", \"rejected\")'>Skip all</button>" +
          '</div>'
        : '') +
      '</div>' +
      '<div class="card" style="padding:4px 0">' + g.signals.map(sigRow).join('') + '</div>' +
    '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  const seen = sig.companies
    ? '<span class="cost-chip">seen on ' + sig.companies +
      (sig.companies === 1 ? ' company' : ' companies') + ' so far</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">only recorded above ' +
      Math.round(sig.confidence_floor * 100) + '% confidence</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([\'' + sig.code +
        '\'],\'rejected\')">Turn off</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([\'' + sig.code +
          '\'],\'confirmed\')">Turn on</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([\'' + sig.code +
          '\'],\'confirmed\')">Confirm</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals([\'' + sig.code +
          '\'],\'rejected\')">Skip</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
      '<div class="sig-label">' + escHtml(sig.label || sig.code) +
        '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
      '<div class="sig-desc">' + escHtml(sig.description) + '</div>' +
      '<div class="sig-meta">' +
        '<span class="cost-chip ' + costCls + '">' + escHtml(sig.cost_note || 'cost unknown') + '</span>' +
        floor + seen + badge(sig.status) +
      '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
  '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({codes: codes, status: status}),
  });
  if (data && data.success) {
    showToast(
      status === 'confirmed'
        ? 'Confirmed ' + data.changed + ' signal' + (data.changed === 1 ? '' : 's') +
          ' — Mercury will collect ' + (data.changed === 1 ? 'it' : 'them') + ' from now on.'
        : data.changed + ' signal' + (data.changed === 1 ? '' : 's') + ' turned off.',
      'success');
  } else {
    showToast('Could not update that signal.', 'error');
  }
  loadSignals();
}

// ── Discover: pick a source, price it, then run it ──

let _provider = null;
let _discoverPoll = null;

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }

  _provider = _provider || data.selected || data.default;
  el.innerHTML = '<div class="prov-grid">' + data.providers.map(p => {
    const ready = p.configured
      ? toneBadge('good', 'Ready')
      : toneBadge('waiting', 'Needs a key');
    return '<div class="prov-card ' + (p.key === _provider ? 'selected' : '') +
      '" onclick="pickProvider(\'' + p.key + '\')">' +
      '<div class="prov-head"><h3>' + escHtml(p.label) + '</h3>' + ready + '</div>' +
      '<div class="blurb">' + escHtml(p.blurb) + '</div>' +
      '<div class="row"><span class="k">Cost</span><span class="v">' +
        escHtml(p.cost_note) + '</span></div>' +
      '<div class="row"><span class="k">Free</span><span class="v">' +
        escHtml(p.free_tier) + '</span></div>' +
      (p.needs_key
        ? '<div class="row"><span class="k">Setup</span><span class="v">' +
          escHtml(p.env_keys.join(', ')) + ' in .env &middot; ' +
          '<a href="' + escHtml(p.signup_url) + '" target="_blank" rel="noopener">get a key</a>' +
          '</span></div>'
        : '') +
      (p.caveat ? '<div class="caveat">' + escHtml(p.caveat) + '</div>' : '') +
    '</div>';
  }).join('') + '</div>';

  const running = data.running;
  document.getElementById('disc-stop').style.display = running ? '' : 'none';
  if (running && !_discoverPoll) {
    _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else if (!running && _discoverPoll) {
    clearInterval(_discoverPoll);
    _discoverPoll = null;
    // A finished run leaves the button stuck on "Running…" otherwise.
    const btn = document.getElementById('disc-run');
    btn.disabled = true;
    btn.textContent = 'Estimate first';
    showToast('Discovery finished.', 'success');
  }
  renderDiscoverResult(data.last_report, running);
}

function pickProvider(key) {
  _provider = key;
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = 'Estimate first';
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

function discoverBody() {
  const raw = document.getElementById('disc-cities').value.trim();
  return {
    provider: _provider,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: raw ? raw.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    depth: Number(document.getElementById('disc-depth').value) || 30,
    limit: Number(document.getElementById('disc-limit').value) || 100,
    max_spend: Number(document.getElementById('disc-cap').value) || 1,
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  el.innerHTML = '<p class="muted" style="font-size:13px">Pricing it…</p>';
  const data = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (!data || data.error) {
    el.innerHTML = '<div class="test-result error">' +
      escHtml((data && data.error) || 'Could not estimate.') + '</div>';
    return;
  }

  const cap = discoverBody().max_spend;
  const over = data.estimated_cost > cap;
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
      (data.free ? 'Free' : '$' + data.estimated_cost.toFixed(4)) + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">' +
      data.query_count + ' quer' + (data.query_count === 1 ? 'y' : 'ies') +
      (data.free ? '' : ' &middot; cap is $' + cap.toFixed(2)) + '</div>' +
    (over ? '<div class="test-result error" style="margin-top:12px">' +
      'Over your cap. Raise the cap or narrow the search.</div>' : '') +
    '<div class="query-list">' +
      data.queries.slice(0, 40).map(escHtml).join('<br>') +
      (data.queries.length > 40 ? '<br>… and ' + (data.queries.length - 40) + ' more' : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  btn.disabled = over;
  btn.textContent = over ? 'Over cap'
    : (data.free ? 'Run — free' : 'Run — spend up to $' + data.estimated_cost.toFixed(2));
}

async function runDiscovery() {
  const btn = document.getElementById('disc-run');
  btn.disabled = true;
  btn.textContent = 'Running…';
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (data && data.success) {
    showToast('Discovery started — ' + data.queries + ' queries.', 'success');
  } else {
    showToast((data && data.message) || 'Could not start.', 'error');
    btn.disabled = false;
  }
  loadDiscoverProviders();
}

async function stopDiscovery() {
  const data = await api('/api/discover/stop', {method: 'POST'});
  showToast(data && data.success ? 'Stopping after the current query.'
                                 : 'Could not stop.', data ? 'success' : 'error');
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (running) {
    el.innerHTML = '<div class="card"><h2>Running…</h2>' +
      '<p class="muted" style="font-size:13px">Mercury is working through the ' +
      'queries. Results land in Companies, and the run log is on Today.</p></div>';
    return;
  }
  if (!report) { el.innerHTML = ''; return; }

  const stat = (label, value, tone) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value"' + (tone ? ' style="color:var(--' + tone + ')"' : '') + '>' +
    value + '</div></div>';

  el.innerHTML = '<div class="subhead">Last run</div>' +
    '<div class="stats-grid">' +
      stat('New companies', report.new_companies || 0, 'accent') +
      stat('Already known', report.known_companies || 0) +
      stat('Observations', report.observations || 0) +
      stat('Filtered as junk', report.junk || 0) +
      stat('Actual cost', report.actual_cost ? '$' + report.actual_cost.toFixed(4) : 'free') +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      'Stopped early: ' + escHtml(report.stopped) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>Problems</h2>' +
        (report.errors || []).slice(0, 8).map(e =>
          '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
}

// ── Cohort builder: a prospect list is a query ──

let _cohort = { require: new Set(), exclude: new Set() };

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Confirm some signals above and they become the building blocks here.</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + title +
      ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      hint + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
        (_cohort[key].has(s.code) ? 'checked ' : '') +
        'onchange="toggleCohort(\'' + key + '\',\'' + s.code + '\',this.checked)">' +
        '<span>' + escHtml(s.label || s.code) + '</span>' +
        '<span class="n">' + (s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  el.innerHTML = col('Must have', 'require', '&mdash; every company in the cohort carries all of these') +
    '<div style="height:18px"></div>' +
    col('Must not have', 'exclude', '&mdash; disqualifiers');
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  if (!require.length) { el.innerHTML = ''; return; }

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({require: require, exclude: exclude}),
  });
  if (!data) { el.innerHTML = ''; return; }

  let html = '<div style="border-top:1px solid var(--border);margin-top:20px;padding-top:20px">' +
    '<div class="cohort-size">' + data.size + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">compan' +
      (data.size === 1 ? 'y matches' : 'ies match') + ' this cohort right now</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>Company</th><th>Domain</th><th>Industry</th><th>Location</th>' +
      '</tr></thead><tbody>' +
      data.companies.slice(0, 50).map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';
    if (data.size > 50) {
      html += '<p class="muted" style="font-size:12px;margin-top:10px">Showing the first 50.</p>';
    }
  } else if (data.size === 0) {
    html += '<p class="muted" style="font-size:13px;margin-top:12px">' +
      'No company carries all of those yet. Either loosen the cohort, or run ' +
      'prospecting to collect more.</p>';
  }
  el.innerHTML = html + '</div>';
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;

  // Active provider indicator
  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? 'Saved — enter new value to change' : base;
  };
  const tag = (id, ok, okLabel) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? okLabel : 'not set';
  };

  // Non-secret values repopulate
  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'Enter client secret');
  savedPh('smtp-pass', data.smtp_password_set, 'Enter password / app password');
  savedPh('instantly-key', data.instantly_api_key_set, 'Enter your Instantly API key');
  savedPh('reoon-key', data.reoon_api_key_set, '600 free/mo — reoon.com/email-verifier');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, '100 free/mo — best for M365/Workspace catch-alls');
  savedPh('hunter-key', data.hunter_api_key_set, '50 free/mo + email-pattern lookup');
  savedPh('linkedin-password', data.linkedin_password_set, 'Enter password');
  savedPh('cf-api-token', data.cloudflare_api_token_set, 'Your Cloudflare API Token');

  // Gmail auth status chip
  const g = document.getElementById('gmail-status');
  if (g) {
    if (data.gmail_authorized) { g.className = 'email-tag verified'; g.textContent = 'authorized'; }
    else if (data.gmail_client_secret_set) { g.className = 'email-tag risky'; g.textContent = 'run: mercury gmail auth'; }
    else { g.className = 'email-tag guess'; g.textContent = 'not set up'; }
  }
  tag('reoon-status', data.reoon_api_key_set, 'set');
  tag('zerobounce-status', data.zerobounce_api_key_set, 'set');
  tag('hunter-status', data.hunter_api_key_set, 'set');
}

function saveGmail() {
  const payload = {GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim()};
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, 'Gmail OAuth saved. Now run "mercury gmail auth" in your terminal.').then(loadSettings);
}

function saveSmtp() {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  saveEnv(payload, 'SMTP settings saved.').then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast('Enter at least one key first.', 'error'); return; }
  saveEnv(payload, 'Verification keys saved.').then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || 'Save failed — is the dashboard still running?', 'error');
}

function saveInstantly() {
  saveEnv({INSTANTLY_API_KEY: document.getElementById('instantly-key').value.trim()}, 'Instantly API key saved.');
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, 'LinkedIn credentials saved.');
}

function saveCloudflare() {
  saveEnv({
    CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim(),
    CLOUDFLARE_API_TOKEN: document.getElementById('cf-api-token').value.trim()
  }, 'Cloudflare credentials saved.');
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">Testing&hellip;</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">Could not reach the dashboard server.</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(data.message) + '</div>';
}

// ── Controls ──

async function loadMercuryStatus() {
  const data = await api('/api/mercury/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = 'Offline';
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? 'Mercury is running' : 'Mercury is stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; started ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = 'Mercury wakes every few minutes, does what needs doing, and sleeps.';
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startMercury() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/mercury/start', {method: 'POST'});
  if (data && data.success) showToast('Mercury started.', 'success');
  else showToast((data && data.message) || 'Failed to start.', 'error');
  btn.disabled = false;
  loadMercuryStatus();
}

async function stopMercury() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/mercury/stop', {method: 'POST'});
  if (data && data.success) showToast('Mercury stopped.', 'success');
  else showToast((data && data.message) || 'Failed to stop.', 'error');
  btn.disabled = false;
  loadMercuryStatus();
}

async function loadLogs() {
  const data = await api('/api/mercury/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = 'No logs yet. Start Mercury to see activity.';
  }
}

// ── Pipeline data ──

async function loadStats() {
  const grid = document.getElementById('stats-grid');
  const data = await api('/api/stats');
  if (!data) { grid.innerHTML = offlineState(); return; }
  if (data.error) {
    grid.innerHTML = emptyState('chart-line-up', 'No pipeline data yet',
      'Start Mercury from the <b>Controls</b> tab and it will begin prospecting, writing, and sending on its own.');
    return;
  }
  const p = data.prospects || {}, c = data.campaigns || {}, v = data.conversations || {};
  const chips = (map) => {
    const entries = Object.entries(map || {});
    if (!entries.length) return '<span class="chip muted">none yet</span>';
    return entries.map(([k, n]) =>
      '<span class="chip">' + escHtml(k) + ' <b>' + escHtml(String(n)) + '</b></span>'
    ).join('');
  };
  const card = (label, value, breakdown) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    '<div class="breakdown">' + breakdown + '</div></div>';

  grid.innerHTML =
    card('Prospects', p.total || 0, chips(p.by_status)) +
    card('Campaigns', c.total || 0, chips(c.by_status)) +
    card('Conversations', v.total || 0, chips(v.by_status)) +
    card('Actions Logged', data.actions_total || 0,
      '<span class="chip">Claude calls today <b>' + escHtml(String(data.claude_calls_today || 0)) + '</b></span>');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}


function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!STATUS[status]) return '';
  const m = statusMeta(status);
  return ' <span style="margin-left:8px">' + toneBadge(m.tone, m.label) + '</span>';
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  // Quota gauges — the same numbers `/usage` shows in Claude Code.
  const quotaEl = document.getElementById('usage-quota');
  if (data.quota && Object.keys(data.quota).length) {
    const labels = {five_hour: '5-hour window', seven_day: 'Weekly'};
    let qHtml = '<h2>Claude Subscription Quota</h2>';
    for (const [key, w] of Object.entries(data.quota)) {
      const pct = Math.min(100, Math.max(0, w.utilization || 0));
      const color = pct >= 80 ? 'yellow' : 'green';
      const resets = w.resets_at ? 'resets ' + formatDate(w.resets_at) : '';
      qHtml += '<div class="progress-wrap">' +
        '<div class="progress-label">' +
          '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
          '<span class="pct">' + pct.toFixed(0) + '%</span>' +
        '</div>' +
        '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
      '</div>';
    }
    quotaEl.innerHTML = qHtml;
    quotaEl.style.display = 'block';
  } else {
    quotaEl.style.display = 'none';
  }

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars: on a subscription plan usage isn't billed per token.
  const t = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + (p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">calls</span>' +
        '<span class="chip">out <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">in <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">cached <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card('Today', t.today) + card('Last 7 Days', t.week) + card('Last 30 Days', t.month);

  // Daily bars — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = data.by_day || [];
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    let dHtml = '<h2>Daily Output Tokens (30 days)</h2>';
    for (const d of days.slice(-30)) {
      const pct = Math.max(2, (d.output_tokens || 0) / maxOut * 100);
      dHtml += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;font-size:12px">' +
        '<span class="muted" style="width:78px;flex-shrink:0;font-family:var(--mono)">' + escHtml(d.day || '') + '</span>' +
        '<div style="flex:1;background:var(--panel-raised);border-radius:99px;height:10px;overflow:hidden">' +
          '<div style="width:' + pct + '%;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent-deep),var(--accent))"></div>' +
        '</div>' +
        '<span style="width:130px;text-align:right;font-variant-numeric:tabular-nums">' + fmtTokens(d.output_tokens) + ' out' +
          ' <span class="muted">&middot; ' + (d.calls || 0) + ' calls</span></span>' +
      '</div>';
    }
    dailyEl.innerHTML = dHtml;
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables — calls + tokens, no cost column
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="card"><h2>' + title + '</h2><div class="table-card"><table><thead><tr>' +
      '<th>' + keyName + '</th><th>Calls</th><th>Input</th><th>Output</th><th>Cache read</th>' +
      '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td>' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td>' + (r.calls || 0) + '</td>' +
        '<td class="muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td>' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="muted">' + fmtTokens(r.cache_read_tokens) + '</td></tr>';
    }
    return h + '</tbody></table></div></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState('gauge', 'No usage recorded yet',
      'Once Mercury starts making Claude calls, every one is logged here with exact calls and tokens by agent and task. Run <b>mercury usage --reconcile</b> to backfill from Claude Code transcripts.');
  } else {
    tablesEl.innerHTML =
      table('By Agent (30 days)', data.by_agent, 'Agent') +
      table('By Task (30 days)', data.by_task, 'Task') +
      table('By Model (30 days)', data.by_model, 'Model');
  }
}

// ── Outbox: a decisions desk, not a wall of drafts ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

async function loadOutbox() {
  const data = await api('/api/outbox');
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }

  const pending = data.pending || [];
  _desk.items = pending;
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', pending.length);

  actions.innerHTML =
    (pending.length && !data.paused
      ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()">Approve all ' +
        pending.length + '</button>' : '') +
    (data.paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' +
        'Resume sending</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">' +
        'Pause all sending</button>');

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  banner.innerHTML = data.paused
    ? '<div class="card" style="border-color:var(--s-bad-line);margin-bottom:16px">' +
        '<h2 class="icon-title" style="color:var(--s-bad)">' + icon('pause-circle') + 'Sending is paused</h2>' +
        '<p style="color:var(--text-2);font-size:13px">' + escHtml(data.paused) +
        '. Approved mail stays queued until you resume.</p></div>'
    : '';

  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState('tray', 'Nothing to review',
      'Every draft Mercury writes lands here first. Approve one and it sends on schedule.') +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="card"><h2>' + title + '</h2><div class="table-card"><table><thead><tr>' +
      cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div></div>';
  };

  list.innerHTML =
    table('Approved &amp; scheduled', data.approved, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sends', r => formatDate(r.send_at), true],
    ]) +
    table('Recently sent', data.sent, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sent', r => formatDate(r.sent_at), true],
    ]) +
    table('Didn\'t send', data.failed, [
      ['To', r => r.to_email], ['Status', r => badge(r.status), false, true],
      ['Reason', r => r.error, true], ['Updated', r => formatDate(r.updated_at), true],
    ]);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) =>
    '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<div class="to">' + escHtml(it.to_email) + '</div>' +
      '<div class="sub">' + escHtml(it.subject || '(no subject)') + '</div>' +
    '</div>').join('');

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">To <b>' + escHtml(cur.to_email) + '</b> &middot; step ' +
        cur.step + ' (' + escHtml(cur.kind) + ') &middot; sends ' +
        formatDate(cur.send_at) + '</div>' +
      '<div class="subject">' + escHtml(cur.subject || '(no subject)') + '</div>' +
      '<div class="body">' + escHtml(cur.body) + '</div>' +
      '<div class="desk-actions">' +
        '<button class="btn btn-primary" onclick="outboxAct(\'' + cur.id + '\',\'approve\')">' +
          'Approve <kbd>A</kbd></button>' +
        '<button class="btn btn-secondary" onclick="outboxAct(\'' + cur.id + '\',\'reject\')">' +
          'Reject <kbd>R</kbd></button>' +
        '<span class="muted" style="font-size:12px;margin-left:auto">' +
          (i + 1) + ' of ' + items.length + '</span>' +
      '</div>' +
    '</div></div>';
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard review. Ignored while typing into a field, so Settings still works.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
});

async function outboxAct(id, action) {
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'approve' ? 'Approved — will send on schedule.' : 'Rejected.', 'success');
  else showToast('Action failed.', 'error');
  loadOutbox();
}

async function outboxApproveAll() {
  const data = await api('/api/outbox/approve-all', {method: 'POST'});
  if (data && data.success) showToast('Approved ' + data.approved + ' email(s).', 'success');
  else showToast('Approve-all failed.', 'error');
  loadOutbox();
}

async function sendingToggle(action) {
  const data = await api('/api/sending/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'pause' ? 'Sending paused.' : 'Sending resumed.', 'success');
  else showToast('Failed.', 'error');
  loadOutbox();
}

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState('buildings', 'No companies yet',
      'Mercury\'s Scout agent hasn\'t researched any companies. Finish <b>Setup</b>, then start Mercury from the <b>Controls</b> tab.');
    return;
  }
  let html = '<div class="table-card"><table><thead><tr><th>Company</th><th>Domain</th><th>Industry</th><th>Size</th><th>Location</th><th>Contacts</th><th>Source</th><th>Added</th></tr></thead><tbody>';
  data.forEach((c, i) => {
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const nameLink = website
      ? '<a href="' + escHtml(website) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + escHtml(c.name) + '</a>'
      : escHtml(c.name);
    html += '<tr style="cursor:pointer" onclick="showCompanyContacts(' + i + ')">' +
      '<td>' + nameLink + '</td><td class="muted">' + escHtml(c.domain) + '</td><td>' + escHtml(c.industry) + '</td>' +
      '<td>' + escHtml(c.company_size) + '</td><td>' + escHtml(c.location) + '</td>' +
      '<td>' + (c.contact_count || 0) + '</td><td class="muted">' + escHtml(c.source) + '</td>' +
      '<td class="muted">' + formatDate(c.created_at) + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies/' + encodeURIComponent(company.id) + '/contacts');
  let html = '<div class="card"><h2>' + escHtml(company.name) + ' — Contacts</h2>' +
    '<button class="btn btn-secondary btn-sm" onclick="loadCompanies()" style="margin-bottom:16px">&larr; Back to Companies</button>';
  if (!data || !data.length) {
    html += '<p style="color:var(--text-3);font-size:13px">No contacts found at this company yet.</p></div>';
  } else {
    html += '<div class="table-card"><table><thead><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const emailIcon = emailTag(p);
      const phoneIcon = p.phone_verified ? ' <span class="verified" title="verified">' + icon('check-circle') + '</span>' : '';
      html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</td>' +
        '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.email) + emailIcon + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (p.linkedin_url ? '<a href="' + escHtml(p.linkedin_url) + '" target="_blank" rel="noopener">Profile</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + escHtml(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast('Feedback saved. Mercury will take it into account.', 'success');
  else showToast((data && data.message) || 'Could not save feedback.', 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, 'Feedback on this contact:');
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, 'Leave feedback on this campaign:');
}

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/prospects');
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    el.innerHTML = emptyState('address-book', 'No contacts yet',
      'Mercury hasn\'t found any prospects. Once it\'s running, the Scout agent searches the web for people matching your ideal customer profile in <b>mercury.yaml</b>.');
    return;
  }
  let html = '<div class="table-card"><table><thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Status</th><th>Source</th><th>Added</th><th></th></tr></thead><tbody>';
  data.forEach((p, i) => {
    const emailV = p.email ? (escHtml(p.email) + emailTag(p)) : '';
    const phoneV = p.phone ? (escHtml(p.phone) + (p.phone_verified ? ' <span class="verified" title="verified">' + icon('check-circle') + '</span>' : '')) : '';
    html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</td>' +
      '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.company) + '</td>' +
      '<td>' + emailV + '</td><td>' + phoneV + '</td><td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + escHtml(p.source) + '</td><td class="muted">' + formatDate(p.created_at) + '</td>' +
      '<td><button class="btn btn-secondary btn-sm" onclick="fbProspect(' + i + ')">Feedback</button></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState('envelope-simple', 'No campaigns yet',
      'The Writer agent hasn\'t drafted any sequences. It kicks in automatically once Mercury has scored prospects to write for.');
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    for (const step of (c.sequence || [])) {
      stepsHtml += '<div class="email-step"><div class="step-num">Email ' + escHtml(String(step.step || '?')) +
        (step.delay_days ? ' &middot; send after ' + escHtml(String(step.delay_days)) + ' days' : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div></div>';
    }
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><h3>' + escHtml(c.name || 'Untitled Campaign') + '</h3>' +
      '<div class="meta">' + badge(c.status) + '<span>' + escHtml(c.channel || 'email') + '</span>' +
      '<span>' + pc + ' prospect' + (pc !== 1 ? 's' : '') + '</span><span>' + formatDate(c.created_at) + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="fbCampaign(' + i + ')">Feedback</button></div>' +
      (stepsHtml || '<p style="color:var(--text-3);font-size:13px">No email steps in this campaign.</p>') + '</div>';
  });
  el.innerHTML = html;
}

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState('chat-circle-text', 'No conversations yet',
      'No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.');
    return;
  }
  let html = '';
  for (const c of data) {
    let threadHtml = '';
    for (const msg of (c.thread || [])) {
      const cls = msg.sender === 'mercury' ? 'sent' : 'received';
      threadHtml += '<div class="thread-msg ' + cls + '"><div class="sender">' + escHtml(msg.sender) +
        ' &middot; ' + formatDate(msg.timestamp) + '</div>' + escHtml(msg.content) + '</div>';
    }
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || 'Unknown';
    html += '<div class="convo-card"><h3>' + escHtml(name) +
      (c.company ? ' <span style="color:var(--text-3);font-weight:500">&mdash; ' + escHtml(c.company) + '</span>' : '') + '</h3>' +
      '<div class="meta">' + badge(c.status) + (c.intent ? badge(c.intent) : '') +
      '<span>' + escHtml(c.prospect_email || '') + '</span><span>' + formatDate(c.updated_at) + '</span></div>' +
      (threadHtml || '<p style="color:var(--text-3);font-size:13px">No messages in this thread yet.</p>') + '</div>';
  }
  el.innerHTML = html;
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState('clock-counter-clockwise', 'No activity yet',
      'Mercury hasn\'t taken any actions. Every prospect found, email written, and reply handled will appear here the moment it happens.');
    return;
  }
  let html = '<div class="activity-feed">';
  for (const a of data) {
    html += '<div class="activity-item"><span class="time">' + formatDate(a.created_at) + '</span>' +
      '<span class="agent">' + escHtml(a.agent) + '</span>' +
      '<span class="action">' + escHtml(a.action_type) + '</span></div>';
  }
  el.innerHTML = html + '</div>';
}

// ── Writes that keep their error body ──
//
// api() collapses every non-2xx into null, which is right for reads but throws
// away the reason a write was refused ("prospect not found", "that column is
// managed by Mercury"). Writes from Pipeline and Calendar go through here so
// the toast can say why.

async function postJSON(path, body) {
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch { /* empty or non-JSON body */ }
    const ok = r.ok && !(data && data.success === false);
    let error = '';
    if (!ok) {
      const why = data && (data.error || data.message || data.detail);
      error = typeof why === 'string' ? why
        : r.status === 404 ? 'not found on this server (restart mercury dashboard?)'
        : 'request failed (' + r.status + ')';
    }
    return { ok, status: r.status, data, error };
  } catch {
    return { ok: false, status: 0, data: null, error: 'can\'t reach the dashboard server' };
  }
}

// A read that can tell "the server is down" from "this server predates the view".
async function getJSON(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) return { ok: false, status: r.status, data: null };
    return { ok: true, status: r.status, data: await r.json() };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

function unavailableState(res, iconName, what) {
  if (res && res.status === 404) {
    return emptyState(iconName, what + ' isn\'t available on this server yet',
      'The running dashboard predates this view. Restart it with <b>mercury dashboard</b> and refresh the page.');
  }
  if (res && res.status >= 500) {
    return emptyState('warning', what + ' failed to load',
      'The server hit an error building this view. Check <b>data/mercury.log</b>, then refresh.');
  }
  return offlineState();
}

// ── Local time ──
//
// The database stores naive UTC ("2026-10-06T14:03:22"). Every date on these
// two views is shown in the browser's local time, so parse as UTC first.

const pad2 = n => String(n).padStart(2, '0');

function parseUTC(s) {
  if (!s) return null;
  let t = String(s).trim().replace(' ', 'T');
  if (/^\d{4}-\d\d-\d\d$/.test(t)) t += 'T00:00:00';
  if (!/(Z|[+-]\d\d:?\d\d)$/i.test(t)) t += 'Z';
  const d = new Date(t);
  return isNaN(d) ? null : d;
}

function ymd(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function firstOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function hhmm(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
function dayDiff(d) { return Math.round((startOfDay(d) - startOfDay(new Date())) / 864e5); }
function localInputValue(d) { return ymd(d) + 'T' + hhmm(d); }

function fullWhen(d) {
  if (!d) return '';
  return d.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric',
    year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric'}) + ', ' + hhmm(d);
}

// "today 14:00", "tomorrow 09:30", "Thu 10:15", "3d ago", "Oct 12"
function relWhen(d) {
  if (!d) return '';
  const days = dayDiff(d);
  if (days === 0) return 'today ' + hhmm(d);
  if (days === 1) return 'tomorrow ' + hhmm(d);
  if (days === -1) return 'yesterday';
  if (days > 1 && days < 7) return d.toLocaleDateString('en-US', {weekday: 'short'}) + ' ' + hhmm(d);
  if (days < -1 && days > -7) return -days + 'd ago';
  return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
}

function fmtScore(s) {
  const n = Number(s);
  if (!isFinite(n)) return String(s);
  return String(Math.round(n * 10) / 10);
}

// ── Drawer: one right-hand panel shared by Pipeline and Calendar ──

let _drawerCtx = null;      // {type:'prospect', id} | {type:'event', id, from}
let _drawerPrevFocus = null;

function drawerOpen() {
  const d = document.getElementById('drawer');
  return !!d && d.classList.contains('open');
}

function openDrawer(kicker, title, subHtml, bodyHtml) {
  const d = document.getElementById('drawer');
  document.getElementById('drawer-kicker').textContent = kicker || '';
  document.getElementById('drawer-title').textContent = title || '';
  document.getElementById('drawer-sub').innerHTML = subHtml || '';
  const body = document.getElementById('drawer-body');
  body.innerHTML = bodyHtml || '';
  body.scrollTop = 0;
  if (!drawerOpen()) _drawerPrevFocus = document.activeElement;
  d.classList.add('open');
  d.setAttribute('aria-hidden', 'false');
  document.getElementById('drawer-scrim').classList.add('open');
  const close = d.querySelector('.drawer-head .btn-square');
  if (close) close.focus({preventScroll: true});
}

function closeDrawer() {
  if (!drawerOpen()) return;
  const d = document.getElementById('drawer');
  d.classList.remove('open');
  d.setAttribute('aria-hidden', 'true');
  document.getElementById('drawer-scrim').classList.remove('open');
  _drawerCtx = null;
  if (_drawerPrevFocus && document.contains(_drawerPrevFocus)) _drawerPrevFocus.focus({preventScroll: true});
  _drawerPrevFocus = null;
}

function facts(rows) {
  return '<dl class="facts">' + rows.map(([k, v]) =>
    '<div><dt>' + k + '</dt><dd>' + v + '</dd></div>').join('') + '</dl>';
}

// ── Confirm dialog (instead of window.confirm) ──

let _modalResolve = null, _modalPrevFocus = null;

function modalOpen() {
  const m = document.getElementById('modal');
  return !!m && m.classList.contains('open');
}

function confirmModal(opts) {
  if (_modalResolve) _modalResolve(false);
  const m = document.getElementById('modal');
  document.getElementById('modal-title').textContent = opts.title || 'Are you sure?';
  document.getElementById('modal-copy').textContent = opts.copy || '';
  const ok = document.getElementById('modal-ok');
  ok.textContent = opts.ok || 'Confirm';
  _modalPrevFocus = document.activeElement;
  m.classList.add('open');
  m.setAttribute('aria-hidden', 'false');
  setTimeout(() => ok.focus(), 0);
  return new Promise(resolve => { _modalResolve = resolve; });
}

function closeModal(result) {
  const m = document.getElementById('modal');
  if (!m.classList.contains('open')) return;
  m.classList.remove('open');
  m.setAttribute('aria-hidden', 'true');
  const resolve = _modalResolve;
  _modalResolve = null;
  if (_modalPrevFocus && document.contains(_modalPrevFocus)) _modalPrevFocus.focus({preventScroll: true});
  if (resolve) resolve(!!result);
}

// ── Pipeline: a board of every contact ──
//
// The first three columns (New, Queued, Contacted) are Mercury's bookkeeping —
// it moves cards there itself as it writes and sends, so they're locked. The
// rest record what a human learned: a reply, a meeting, a win, a loss. Moving
// a card into Meeting / Won / Lost stops any queued email, which is why those
// three ask first.

const PIPE_STOPS = new Set(['meeting', 'won', 'lost']);
let _pipe = { columns: [], q: '', drag: null, loaded: false, menuFor: null, menuAnchor: null };

function pipeBusy() {
  const menu = document.getElementById('move-menu');
  return !!_pipe.drag || drawerOpen() || modalOpen() || (menu && !menu.hidden);
}

function pipeFind(id) {
  for (const col of _pipe.columns) {
    const index = col.items.findIndex(it => String(it.id) === String(id));
    if (index >= 0) return { col, item: col.items[index], index };
  }
  return null;
}

function pipeCount(col) { return col.count ?? col.items.length; }

async function loadPipeline() {
  const el = document.getElementById('pipe-board');
  if (!_pipe.loaded) el.innerHTML = '<p class="loading-note">Loading the pipeline…</p>';
  const res = await getJSON('/api/pipeline');
  if (!res.ok || !res.data || !Array.isArray(res.data.columns)) {
    _pipe.loaded = false;
    _pipe.columns = [];
    el.innerHTML = unavailableState(res, 'kanban', 'The pipeline');
    document.getElementById('pipe-summary').textContent = '';
    return;
  }
  if (_pipe.drag) return;   // a drag began while we were fetching; don't yank the board
  _pipe.columns = res.data.columns.map(c => ({ ...c, items: Array.isArray(c.items) ? c.items : [] }));
  _pipe.loaded = true;
  renderBoard();
}

function pipeSearch(q) {
  _pipe.q = q || '';
  if (_pipe.loaded) renderBoard();
}

function pipeMatch(it, q) {
  if (!q) return true;
  return [it.name, it.company, it.email].some(v => String(v || '').toLowerCase().includes(q));
}

function renderBoard() {
  const el = document.getElementById('pipe-board');
  const q = _pipe.q.trim().toLowerCase();
  const total = _pipe.columns.reduce((n, c) => n + pipeCount(c), 0);
  let shown = 0;

  el.innerHTML = '<div class="pipe-board">' + _pipe.columns.map(c => {
    const items = c.items.filter(it => pipeMatch(it, q));
    shown += items.length;
    const n = q ? items.length : pipeCount(c);
    const hidden = !q && pipeCount(c) > c.items.length
      ? '<div class="pipe-more">' + (pipeCount(c) - c.items.length) + ' more not shown</div>' : '';
    return '<section class="pipe-col' + (c.locked ? ' locked' : '') + '" data-col="' + escHtml(c.key) + '">' +
      '<header class="pipe-col-head">' +
        '<div class="pipe-col-title">' +
          '<span class="pipe-col-name">' + escHtml(c.label) + '</span>' +
          '<span class="pipe-count">' + n + '</span>' +
          (c.locked ? '<span class="pipe-lock" title="' + escHtml(c.hint || 'Mercury moves these cards itself') +
            '" aria-label="Locked: ' + escHtml(c.hint || 'Mercury moves these cards itself') + '">' +
            icon('lock-simple') + '</span>' : '') +
        '</div>' +
        (c.hint ? '<p class="pipe-col-hint">' + escHtml(c.hint) + '</p>' : '') +
      '</header>' +
      '<div class="pipe-col-body">' +
        (items.length ? items.map(pipeCard).join('')
          : '<div class="pipe-empty">' + (q ? 'No matches' : (c.locked ? 'Nothing here' : 'Nothing here · drop a card')) + '</div>') +
        hidden +
      '</div>' +
    '</section>';
  }).join('') + '</div>';

  const talking = _pipe.columns.filter(c => c.key === 'replied' || c.key === 'meeting')
    .reduce((n, c) => n + pipeCount(c), 0);
  document.getElementById('pipe-summary').innerHTML = q
    ? '<b>' + shown + '</b> of ' + total + ' contacts match'
    : total
      ? '<b>' + total.toLocaleString('en-US') + '</b> contact' + (total === 1 ? '' : 's') +
        ' &middot; <b>' + talking + '</b> in conversation'
      : 'No contacts yet &mdash; they appear here once Scout finds people.';
}

function pipeCard(it) {
  const next = parseUTC(it.next_send_at);
  const sent = Number(it.sent_count || 0);
  const id = escHtml(String(it.id));
  const name = it.name || it.email || 'Unknown';
  const sub = [it.title, it.company].filter(Boolean).map(escHtml).join(' &middot; ');
  const meta = [];
  if (it.email_status) meta.push(badge(it.email_status));
  if (next) {
    meta.push('<span class="pc-fact" title="Next email: ' + escHtml(fullWhen(next)) + '">' + icon('clock') +
      'Next: ' + escHtml(next < new Date() ? 'due now' : relWhen(next)) + '</span>');
  }
  if (sent) meta.push('<span class="pc-fact">' + icon('paper-plane-tilt') + sent + ' sent</span>');
  const hasScore = it.score !== null && it.score !== undefined && it.score !== '';

  return '<article class="pipe-card" draggable="true" tabindex="0" data-id="' + id + '" ' +
      'aria-label="' + escHtml(name) + (it.company ? ', ' + escHtml(it.company) : '') + '. Enter to open, M to move.">' +
    '<div class="pc-top">' +
      '<span class="pc-name">' + escHtml(name) + '</span>' +
      (hasScore ? '<span class="pc-score" title="Fit score">' + escHtml(fmtScore(it.score)) + '</span>' : '') +
      '<button class="pc-move" data-move="' + id + '" title="Move to…" aria-haspopup="menu" ' +
        'aria-label="Move ' + escHtml(name) + ' to another column">' + icon('dots-three') + '</button>' +
    '</div>' +
    (sub ? '<div class="pc-sub">' + sub + '</div>' : '') +
    (meta.length ? '<div class="pc-meta">' + meta.join('') + '</div>' : '') +
  '</article>';
}

function pipeCanDrop(key) {
  const col = _pipe.columns.find(c => c.key === key);
  return !!col && !col.locked && !(_pipe.drag && _pipe.drag.from === key);
}

function pipeDragEnd() {
  _pipe.drag = null;
  const board = document.getElementById('pipe-board');
  board.querySelectorAll('.is-dragging, .dragging, .drop-ok, .drop-hover').forEach(n =>
    n.classList.remove('is-dragging', 'dragging', 'drop-ok', 'drop-hover'));
}

(function initPipelineBoard() {
  const board = document.getElementById('pipe-board');
  if (!board) return;

  board.addEventListener('click', e => {
    const mv = e.target.closest('[data-move]');
    if (mv) { e.stopPropagation(); openMoveMenu(mv.dataset.move, mv); return; }
    const card = e.target.closest('.pipe-card');
    if (card) openProspectDrawer(card.dataset.id);
  });

  board.addEventListener('keydown', e => {
    const card = e.target.closest('.pipe-card');
    if (!card || e.target !== card) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openProspectDrawer(card.dataset.id); }
    else if (e.key === 'm' || e.key === 'M') { e.preventDefault(); openMoveMenu(card.dataset.id, card.querySelector('[data-move]')); }
  });

  board.addEventListener('dragstart', e => {
    const card = e.target.closest && e.target.closest('.pipe-card');
    if (!card) return;
    const found = pipeFind(card.dataset.id);
    if (!found) { e.preventDefault(); return; }
    _pipe.drag = { id: card.dataset.id, from: found.col.key };
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', card.dataset.id); } catch { /* old browsers */ }
    closeMoveMenu();
    // Next frame, so the browser snapshots the undimmed card as the drag image.
    requestAnimationFrame(() => {
      if (!_pipe.drag) return;
      card.classList.add('dragging');
      const root = board.querySelector('.pipe-board');
      if (root) root.classList.add('is-dragging');
      board.querySelectorAll('.pipe-col').forEach(col =>
        col.classList.toggle('drop-ok', pipeCanDrop(col.dataset.col)));
    });
  });

  board.addEventListener('dragover', e => {
    if (!_pipe.drag) return;
    const col = e.target.closest('.pipe-col');
    if (!col || !pipeCanDrop(col.dataset.col)) {
      e.dataTransfer.dropEffect = 'none';
      board.querySelectorAll('.drop-hover').forEach(c => c.classList.remove('drop-hover'));
      return;
    }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    board.querySelectorAll('.drop-hover').forEach(c => { if (c !== col) c.classList.remove('drop-hover'); });
    col.classList.add('drop-hover');
  });

  board.addEventListener('dragleave', e => {
    const col = e.target.closest('.pipe-col');
    if (col && !col.contains(e.relatedTarget)) col.classList.remove('drop-hover');
  });

  board.addEventListener('drop', e => {
    if (!_pipe.drag) return;
    e.preventDefault();
    const col = e.target.closest('.pipe-col');
    const id = _pipe.drag.id;
    const ok = col && pipeCanDrop(col.dataset.col);
    pipeDragEnd();
    if (ok) pipeMove(id, col.dataset.col);
  });

  board.addEventListener('dragend', pipeDragEnd);
})();

// Keyboard / no-drag path: a small menu of the columns a human may set.
function openMoveMenu(id, anchor) {
  const f = pipeFind(id);
  const menu = document.getElementById('move-menu');
  if (!f || !menu) return;
  if (!menu.hidden && _pipe.menuFor === String(id)) { closeMoveMenu(true); return; }

  const targets = _pipe.columns.filter(c => !c.locked && c.key !== f.col.key);
  menu.innerHTML = '<div class="menu-label">Move to…</div>' +
    (targets.length ? targets.map(c =>
      '<button role="menuitem" data-to="' + escHtml(c.key) + '">' +
        '<span>' + escHtml(c.label) + '</span>' +
        (PIPE_STOPS.has(c.key) ? '<small>stops emails</small>' : '') +
      '</button>').join('')
      : '<div class="menu-empty">No other column takes manual moves.</div>');
  menu.hidden = false;
  _pipe.menuFor = String(id);
  _pipe.menuAnchor = anchor || null;

  const r = (anchor || document.querySelector('.pipe-card[data-id="' + CSS.escape(String(id)) + '"]') || document.body)
    .getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
  menu.style.top = top + 'px';
  menu.style.left = left + 'px';

  menu.onclick = e => {
    const b = e.target.closest('[data-to]');
    if (!b) return;
    closeMoveMenu(true);
    pipeMove(id, b.dataset.to);
  };
  menu.onkeydown = e => {
    const items = [...menu.querySelectorAll('[data-to]')];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); (items[i + 1] || items[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
    else if (e.key === 'Tab') closeMoveMenu();
  };
  const first = menu.querySelector('[data-to]');
  if (first) first.focus({preventScroll: true});
}

function closeMoveMenu(restoreFocus) {
  const menu = document.getElementById('move-menu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  const anchor = _pipe.menuAnchor;
  _pipe.menuFor = null;
  _pipe.menuAnchor = null;
  if (restoreFocus && anchor && document.contains(anchor)) anchor.focus({preventScroll: true});
}

document.addEventListener('click', e => {
  const menu = document.getElementById('move-menu');
  if (menu && !menu.hidden && !menu.contains(e.target)) closeMoveMenu();
});
document.addEventListener('scroll', e => {
  const menu = document.getElementById('move-menu');
  if (menu && !menu.hidden && !menu.contains(e.target)) closeMoveMenu();
}, true);
window.addEventListener('resize', () => closeMoveMenu());

async function pipeMove(id, to) {
  const f = pipeFind(id);
  if (!f || f.col.key === to) return;
  const target = _pipe.columns.find(c => c.key === to);
  if (!target || target.locked) {
    showToast('Mercury manages that column itself.', 'error');
    return;
  }
  const name = f.item.name || f.item.email || 'this contact';

  if (PIPE_STOPS.has(to)) {
    const pending = Number(f.item.pending_count || 0);
    const ok = await confirmModal({
      title: 'Move ' + name + ' to ' + target.label + '?',
      copy: 'This stops any queued emails to ' + name + '.' +
        (pending ? ' ' + pending + ' email' + (pending === 1 ? ' is' : 's are') + ' waiting to send.' : ''),
      ok: 'Move to ' + target.label,
    });
    if (!ok) return;
  }

  // Optimistic: move the card now, put it back if the server says no.
  const snapshot = JSON.parse(JSON.stringify(_pipe.columns));
  const fromCount = pipeCount(f.col), toCount = pipeCount(target);
  f.col.items.splice(f.index, 1);
  f.col.count = fromCount - 1;
  target.items.unshift({ ...f.item, next_send_at: PIPE_STOPS.has(to) ? null : f.item.next_send_at,
                         pending_count: PIPE_STOPS.has(to) ? 0 : f.item.pending_count });
  target.count = toCount + 1;
  renderBoard();
  const moved = document.querySelector('.pipe-card[data-id="' + CSS.escape(String(id)) + '"]');
  if (moved) moved.classList.add('just-moved');

  const res = await postJSON('/api/pipeline/' + encodeURIComponent(id) + '/move', { column: to });
  if (!res.ok) {
    _pipe.columns = snapshot;
    renderBoard();
    showToast('Couldn\'t move ' + name + ': ' + res.error, 'error');
    return;
  }
  const n = Number((res.data && res.data.cancelled) || 0);
  showToast('Moved ' + name + ' to ' + target.label +
    (n ? ' · ' + n + ' queued email' + (n === 1 ? '' : 's') + ' cancelled' : '') + '.', 'success');
  if (!pipeBusy()) loadPipeline();
}

async function openProspectDrawer(id) {
  const f = pipeFind(id);
  if (!f) return;
  const it = f.item;
  _drawerCtx = { type: 'prospect', id: String(id) };
  const next = parseUTC(it.next_send_at), last = parseUTC(it.last_activity);
  const sent = Number(it.sent_count || 0), pending = Number(it.pending_count || 0);
  const hasScore = it.score !== null && it.score !== undefined && it.score !== '';
  const sub = [it.title, it.company].filter(Boolean).map(escHtml).join(' &middot; ');
  const targets = _pipe.columns.filter(c => !c.locked && c.key !== f.col.key);

  const body =
    facts([
      ['Stage', escHtml(f.col.label) + (f.col.locked ? ' <span class="muted">&middot; moved by Mercury</span>' : '')],
      ['Email', it.email
        ? '<span class="mono">' + escHtml(it.email) + '</span>' + (it.email_status ? ' ' + badge(it.email_status) : '')
        : '<span class="muted">None found yet</span>'],
      ['Status', it.status ? badge(it.status) : '<span class="muted">—</span>'],
      ['Fit score', hasScore ? '<span class="mono">' + escHtml(fmtScore(it.score)) + '</span>' : '<span class="muted">Not scored</span>'],
      ['Emails', '<span class="mono">' + sent + '</span> sent &middot; <span class="mono">' + pending + '</span> queued'],
      ['Next send', next ? escHtml(fullWhen(next)) : '<span class="muted">Nothing queued</span>'],
      ['Last activity', last ? escHtml(fullWhen(last)) + ' <span class="muted">&middot; ' + escHtml(relWhen(last)) + '</span>'
                             : '<span class="muted">—</span>'],
    ]) +
    ((targets.length || it.conversation_id)
      ? '<div class="drawer-actions">' +
          targets.map(c => '<button class="btn btn-secondary btn-sm" data-move-to="' + escHtml(c.key) + '">' +
            'Move to ' + escHtml(c.label) + '</button>').join('') +
          (it.conversation_id ? '<button class="btn btn-secondary btn-sm" data-act="convo">' +
            icon('chat-circle-text') + 'Conversation</button>' : '') +
        '</div>'
      : '') +
    '<div class="drawer-section"><h4>Emails</h4>' +
      '<div id="drawer-emails"><p class="loading-note">Loading emails…</p></div></div>';

  openDrawer('Contact', it.name || it.email || 'Unknown', sub, body);

  const start = addDays(startOfDay(new Date()), -30), end = addDays(start, 61);
  const res = await getJSON('/api/calendar?start=' + ymd(start) + '&end=' + ymd(end));
  if (!_drawerCtx || _drawerCtx.type !== 'prospect' || _drawerCtx.id !== String(id)) return;
  const box = document.getElementById('drawer-emails');
  if (!box) return;
  if (!res.ok || !res.data) {
    box.innerHTML = '<p class="drawer-note">' + (res.status === 404
      ? 'This server predates the calendar, so emails can\'t be listed here yet.'
      : 'Couldn\'t load emails right now.') + '</p>';
    return;
  }
  const items = (res.data.items || [])
    .filter(e => String(e.prospect_id) === String(id))
    .map(e => ({ ...e, _at: parseUTC(e.at) }))
    .sort((a, b) => (a._at || 0) - (b._at || 0));
  items.forEach(e => _evIndex.set(String(e.id), e));
  box.innerHTML = items.length
    ? '<div class="ev-list">' + items.map(evRow).join('') + '</div>'
    : '<p class="drawer-note">No emails in the last 30 days or the next month.</p>';
}

function evRow(e) {
  return '<button class="ev-row" data-ev="' + escHtml(String(e.id)) + '">' +
    '<span class="ev-kind">' + icon(e.kind === 'reply' ? 'arrow-bend-up-left' : 'envelope-simple') + '</span>' +
    '<span class="ev-main"><b>' + escHtml(e.label || 'Email') + '</b>' +
      '<small>' + escHtml(e.subject || '(no subject)') + '</small></span>' +
    '<span class="ev-side">' + calBadge(e.status) +
      '<small>' + (e._at ? escHtml(fullWhen(e._at)) : '') + '</small></span>' +
  '</button>';
}

// ── Calendar: every email, by day, in local time ──

const CAL_STATUS = {
  sent:           ['check-circle', 'Sent'],
  approved:       ['clock', 'Scheduled'],
  pending_review: ['clock', 'Needs approval'],
  cancelled:      ['prohibit', 'Cancelled'],
  rejected:       ['prohibit', 'Rejected'],
  failed:         ['warning-circle', 'Failed'],
};
// which filter checkbox governs each status
const CAL_FILTER_OF = { sent: 'sent', approved: 'approved', pending_review: 'pending_review',
                        cancelled: 'cancelled', rejected: 'cancelled', failed: 'failed' };

const _evIndex = new Map();   // outbox id → item, fed by every calendar fetch
let _cal = {
  month: firstOfMonth(new Date()), view: null, items: [], seq: 0, loadedKey: null,
  filters: { sent: true, approved: true, pending_review: true, cancelled: false, failed: false },
};

function calIcon(status) {
  const s = CAL_STATUS[status] || ['circle-dashed', status];
  return '<span class="ce-ic s-' + escHtml(status || 'unknown') + '">' + icon(s[0]) + '</span>';
}

function calBadge(status) {
  const s = CAL_STATUS[status];
  if (!s) return badge(status);
  return '<span class="badge cal-badge s-' + escHtml(status) + '">' + icon(s[0]) + escHtml(s[1]) + '</span>';
}

function calRange() {
  const first = _cal.month;
  const start = addDays(first, -((first.getDay() + 6) % 7));   // back to Monday
  return { start, end: addDays(start, 42) };
}

function calVisible(e) {
  const key = CAL_FILTER_OF[e.status];
  return key ? !!_cal.filters[key] : true;
}

function renderCalChrome() {
  document.getElementById('cal-title').textContent =
    _cal.month.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
  for (const v of ['month', 'agenda']) {
    const b = document.getElementById('cal-v-' + v);
    b.classList.toggle('on', _cal.view === v);
    b.setAttribute('aria-pressed', String(_cal.view === v));
  }
}

async function loadCalendar(quiet) {
  if (!_cal.view) _cal.view = window.matchMedia('(max-width: 700px)').matches ? 'agenda' : 'month';
  renderCalChrome();
  const { start, end } = calRange();
  const key = ymd(_cal.month);
  const body = document.getElementById('cal-body');
  if (!quiet || _cal.loadedKey !== key) {
    body.innerHTML = _cal.view === 'month'
      ? renderMonth([], true)
      : '<p class="loading-note">Loading…</p>';
    document.getElementById('cal-summary').textContent = '';
  }
  const seq = ++_cal.seq;
  // One extra day each side: the server buckets by UTC date, we bucket by local.
  const res = await getJSON('/api/calendar?start=' + ymd(addDays(start, -1)) + '&end=' + ymd(addDays(end, 1)));
  if (seq !== _cal.seq) return;   // a newer month was requested meanwhile
  if (!res.ok || !res.data) {
    _cal.loadedKey = null;
    _cal.items = [];
    body.innerHTML = unavailableState(res, 'calendar-blank', 'The calendar');
    return;
  }
  _cal.items = (res.data.items || [])
    .map(e => ({ ...e, _at: parseUTC(e.at) }))
    .filter(e => e._at)
    .sort((a, b) => a._at - b._at);
  _cal.items.forEach(e => _evIndex.set(String(e.id), e));
  _cal.loadedKey = key;
  renderCalendar();
}

function renderCalendar() {
  const body = document.getElementById('cal-body');
  const y = _cal.month.getFullYear(), m = _cal.month.getMonth();
  const monthAll = _cal.items.filter(e => e._at.getFullYear() === y && e._at.getMonth() === m);
  const visible = _cal.items.filter(calVisible);
  const inMonth = visible.filter(e => e._at.getFullYear() === y && e._at.getMonth() === m);
  const hidden = monthAll.length - inMonth.length;

  document.getElementById('cal-summary').innerHTML = monthAll.length
    ? '<b>' + inMonth.length + '</b> email' + (inMonth.length === 1 ? '' : 's') + ' this month' +
      (hidden ? ' &middot; ' + hidden + ' hidden by filters' : '')
    : '';

  if (!inMonth.length) {
    body.innerHTML = emptyState('calendar-blank', 'Nothing scheduled this month',
      hidden ? hidden + ' email' + (hidden === 1 ? ' is' : 's are') + ' hidden by the filters above. Tick them to see ' +
               (hidden === 1 ? 'it' : 'them') + '.'
             : 'Approved emails and their follow-ups land here on the day they\'ll send. Try another month, or review the <b>Outbox</b>.');
    return;
  }
  body.innerHTML = _cal.view === 'agenda' ? renderAgenda(inMonth) : renderMonth(visible, false);
}

function calGroup(items) {
  const by = {};
  for (const e of items) (by[ymd(e._at)] = by[ymd(e._at)] || []).push(e);
  return by;
}

function calChip(e) {
  const who = e.name || e.to_email || 'Unknown';
  return '<button class="cal-ev s-' + escHtml(e.status) + '" data-ev="' + escHtml(String(e.id)) + '" ' +
      'title="' + escHtml((e.label || 'Email') + ' · ' + who + (e.subject ? ' · ' + e.subject : '') +
        ' · ' + ((CAL_STATUS[e.status] || [0, e.status])[1])) + '">' +
    calIcon(e.status) +
    '<span class="ce-time">' + hhmm(e._at) + '</span>' +
    '<span class="ce-name">' + escHtml(who) + '</span>' +
  '</button>';
}

function renderMonth(items, loading) {
  const { start } = calRange();
  const by = calGroup(items);
  const todayKey = ymd(new Date());
  const m = _cal.month.getMonth();
  let cells = '';
  for (let i = 0; i < 42; i++) {
    const d = addDays(start, i), k = ymd(d), evs = by[k] || [];
    const label = d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
    cells += '<div class="cal-day' + (d.getMonth() !== m ? ' out' : '') + (k === todayKey ? ' today' : '') +
        '" aria-label="' + escHtml(label) + (evs.length ? ', ' + evs.length + ' emails' : '') + '">' +
      '<div class="cal-dnum"><span>' + d.getDate() + '</span></div>' +
      evs.slice(0, 3).map(calChip).join('') +
      (evs.length > 3 ? '<button class="cal-more" data-day="' + k + '">+' + (evs.length - 3) + ' more</button>' : '') +
    '</div>';
  }
  return '<div class="cal-month' + (loading ? ' loading' : '') + '">' +
    '<div class="cal-dow">' + ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => '<div>' + d + '</div>').join('') + '</div>' +
    '<div class="cal-grid">' + cells + '</div>' +
    (loading ? '<div class="cal-loading">Loading…</div>' : '') +
  '</div>';
}

function renderAgenda(items) {
  const by = calGroup(items);
  const todayKey = ymd(new Date());
  return '<div class="agenda">' + Object.keys(by).map(k => {
    const evs = by[k], d = evs[0]._at;
    const label = d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
    return '<section class="ag-day" id="agenda-' + k + '">' +
      '<header class="ag-head' + (k === todayKey ? ' today' : '') + '">' +
        '<span>' + escHtml(label) + '</span>' +
        (k === todayKey ? '<span class="ag-today">Today</span>' : '') +
        '<span class="ag-count">' + evs.length + '</span>' +
      '</header>' +
      evs.map(e =>
        '<button class="ag-row" data-ev="' + escHtml(String(e.id)) + '">' +
          '<span class="ag-time">' + hhmm(e._at) + '</span>' +
          '<span class="ag-label">' + icon(e.kind === 'reply' ? 'arrow-bend-up-left' : 'envelope-simple') +
            escHtml(e.label || 'Email') + '</span>' +
          '<span class="ag-who">' + escHtml(e.name || e.to_email || 'Unknown') +
            (e.company ? ' <span class="muted">&middot; ' + escHtml(e.company) + '</span>' : '') + '</span>' +
          '<span class="ag-subj">' + escHtml(e.subject || '(no subject)') + '</span>' +
          '<span class="ag-status">' + calBadge(e.status) + '</span>' +
        '</button>').join('') +
    '</section>';
  }).join('') + '</div>';
}

function calShift(n) {
  _cal.month = new Date(_cal.month.getFullYear(), _cal.month.getMonth() + n, 1);
  loadCalendar();
}

async function calToday() {
  const same = ymd(_cal.month) === ymd(firstOfMonth(new Date()));
  _cal.month = firstOfMonth(new Date());
  if (!same || _cal.loadedKey !== ymd(_cal.month)) await loadCalendar();
  if (_cal.view === 'agenda') calScrollTo(ymd(new Date()));
}

function calView(v) {
  _cal.view = v;
  renderCalChrome();
  if (_cal.loadedKey === ymd(_cal.month)) renderCalendar();
  else loadCalendar();
}

function calFilter(el) {
  _cal.filters[el.dataset.f] = el.checked;
  if (_cal.loadedKey === ymd(_cal.month)) renderCalendar();
}

function calScrollTo(k) {
  const el = document.getElementById('agenda-' + k);
  if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

async function calJumpDay(k) {
  const d = new Date(k + 'T00:00:00');
  const month = firstOfMonth(d);
  _cal.view = 'agenda';
  if (ymd(month) !== ymd(_cal.month)) { _cal.month = month; await loadCalendar(); }
  else { renderCalChrome(); renderCalendar(); }
  calScrollTo(k);
}

document.getElementById('cal-body').addEventListener('click', e => {
  const ev = e.target.closest('[data-ev]');
  if (ev) { openEventDrawer(ev.dataset.ev, null); return; }
  const more = e.target.closest('[data-day]');
  if (more) calJumpDay(more.dataset.day);
});

// ── Email drawer (opened from a calendar event, or a contact's email list) ──

function openEventDrawer(id, fromProspect) {
  const e = _evIndex.get(String(id));
  if (!e) return;
  _drawerCtx = { type: 'event', id: String(id), from: fromProspect || null };
  const at = e._at || parseUTC(e.at);
  const whenLabel = { sent: 'Sent', failed: 'Tried', cancelled: 'Was due', rejected: 'Was due' }[e.status] || 'Sends';
  const canDecide = e.status === 'pending_review';
  const canMove = e.status === 'pending_review' || e.status === 'approved';
  const sub = [escHtml(e.name || ''), escHtml(e.company || '')].filter(Boolean).join(' &middot; ');
  const back = fromProspect && pipeFind(fromProspect);

  let note = '';
  if (e.error) {
    const lead = { cancelled: 'Cancelled', rejected: 'Rejected', failed: 'Didn\'t send' }[e.status] || 'Note';
    note = '<div class="ev-note' + (e.status === 'failed' ? ' bad' : '') + '">' +
      icon(e.status === 'failed' ? 'warning-circle' : 'prohibit') +
      '<div><b>' + lead + '.</b> ' + escHtml(e.error) + '</div></div>';
  }

  const actions = (canDecide || canMove)
    ? '<div class="drawer-section"><h4>' + (canDecide ? 'Decide' : 'Change the send time') + '</h4>' +
        (canDecide
          ? '<div class="drawer-actions" style="margin-top:0">' +
              '<button class="btn btn-primary btn-sm" data-act="approve">' + icon('check-circle') + 'Approve</button>' +
              '<button class="btn btn-secondary btn-sm" data-act="reject">' + icon('prohibit') + 'Reject</button>' +
            '</div>'
          : '') +
        '<div class="resched">' +
          '<label class="form-label" for="ev-resched">' + (canDecide ? 'Or send at a different time' : 'Send at') + '</label>' +
          '<div class="resched-row">' +
            '<input type="datetime-local" class="form-input" id="ev-resched" value="' +
              escHtml(localInputValue(at && at > new Date() ? at : new Date(Date.now() + 3600e3))) + '" ' +
              'min="' + escHtml(localInputValue(new Date())) + '">' +
            '<button class="btn btn-secondary btn-sm" data-act="resched">' + icon('pencil-simple') + 'Reschedule</button>' +
          '</div>' +
          '<p class="drawer-note">Your local time (' + escHtml(Intl.DateTimeFormat().resolvedOptions().timeZone || 'local') + ').</p>' +
        '</div>' +
      '</div>'
    : '';

  const body =
    (back ? '<button class="link-btn drawer-back" data-act="back">' + icon('caret-left') +
              'Back to ' + escHtml(back.item.name || 'contact') + '</button>' : '') +
    facts([
      ['To', '<span class="mono">' + escHtml(e.to_email || '—') + '</span>'],
      ['Step', icon(e.kind === 'reply' ? 'arrow-bend-up-left' : 'envelope-simple') + ' ' + escHtml(e.label || 'Email')],
      ['Status', calBadge(e.status)],
      [whenLabel, at ? escHtml(fullWhen(at)) + ' <span class="muted">&middot; ' + escHtml(relWhen(at)) + '</span>' : '<span class="muted">—</span>'],
    ]) +
    note + actions +
    '<div class="drawer-section"><h4>Message</h4>' +
      (e.body ? '<div class="ev-body">' + escHtml(e.body) + '</div>' : '<p class="drawer-note">No body stored for this email.</p>') +
    '</div>';

  openDrawer(e.label || 'Email', e.subject || '(no subject)', sub, body);
}

async function evAct(id, action) {
  document.querySelectorAll('#drawer-body [data-act]').forEach(b => { b.disabled = true; });
  const res = await postJSON('/api/outbox/' + encodeURIComponent(id) + '/' + action);
  if (res.ok) showToast(action === 'approve' ? 'Approved — it sends on schedule.' : 'Rejected — it won\'t send.', 'success');
  else showToast('Couldn\'t ' + action + ': ' + res.error, 'error');
  afterEventChange(id);
}

async function evReschedule(id) {
  const input = document.getElementById('ev-resched');
  const d = input && input.value ? new Date(input.value) : null;   // datetime-local parses as local
  if (!d || isNaN(d)) { showToast('Pick a date and time first.', 'error'); return; }
  if (d < new Date()) { showToast('Pick a time in the future.', 'error'); return; }
  document.querySelectorAll('#drawer-body [data-act]').forEach(b => { b.disabled = true; });
  const res = await postJSON('/api/outbox/' + encodeURIComponent(id) + '/reschedule',
    { send_at: d.toISOString().replace(/\.\d{3}Z$/, 'Z') });
  if (res.ok) showToast('Rescheduled for ' + fullWhen(d) + '.', 'success');
  else showToast('Couldn\'t reschedule: ' + res.error, 'error');
  afterEventChange(id);
}

async function afterEventChange(id) {
  const ctx = _drawerCtx;
  if (ctx && ctx.from) {
    await loadPipeline();
    if (_drawerCtx === ctx) {
      if (pipeFind(ctx.from)) openProspectDrawer(ctx.from); else closeDrawer();
    }
    return;
  }
  if (currentTab === 'calendar') {
    await loadCalendar(true);
    if (_drawerCtx === ctx && ctx) {
      if (_evIndex.has(String(id)) && _cal.items.some(e => String(e.id) === String(id))) openEventDrawer(id, null);
      else closeDrawer();
    }
  }
}

document.getElementById('drawer-body').addEventListener('click', e => {
  const t = e.target.closest('[data-ev], [data-move-to], [data-act]');
  if (!t || t.disabled || !_drawerCtx) return;
  const ctx = _drawerCtx;
  if (t.dataset.ev) {
    openEventDrawer(t.dataset.ev, ctx.type === 'prospect' ? ctx.id : null);
  } else if (t.dataset.moveTo) {
    closeDrawer();
    pipeMove(ctx.id, t.dataset.moveTo);
  } else {
    switch (t.dataset.act) {
      case 'back': openProspectDrawer(ctx.from); break;
      case 'approve': case 'reject': evAct(ctx.id, t.dataset.act); break;
      case 'resched': evReschedule(ctx.id); break;
      case 'convo': closeDrawer(); goTab('conversations'); break;
    }
  }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (modalOpen()) { e.preventDefault(); closeModal(false); return; }
    if (wuAddOpen()) { e.preventDefault(); wuCloseAdd(); return; }
    const menu = document.getElementById('move-menu');
    if (menu && !menu.hidden) { e.preventDefault(); closeMoveMenu(true); return; }
    if (drawerOpen()) { e.preventDefault(); closeDrawer(); }
    return;
  }
  // Keep Tab inside the confirm dialog while it's up.
  if (e.key === 'Tab' && modalOpen()) {
    const btns = [...document.querySelectorAll('#modal .modal button')];
    const i = btns.indexOf(document.activeElement);
    e.preventDefault();
    btns[(i + (e.shiftKey ? -1 : 1) + btns.length) % btns.length].focus();
  }
});

// ── Charts: hand-built SVG, no library ──
//
// Every chart here is drawn at the container's real pixel width (the viewBox
// matches it 1:1), so text and hairlines stay crisp at any size. A
// ResizeObserver redraws on width changes; height is fixed per chart.

const fmtN = v => Number(v || 0).toLocaleString('en-US');
const fmtPct = v => (v === null || v === undefined || !isFinite(v)) ? '—' : (v * 100).toFixed(1) + '%';
const svgEsc = escHtml;

function utcDay(s) {                       // "2026-10-06" → Date at UTC midnight
  const d = new Date(String(s).slice(0, 10) + 'T00:00:00Z');
  return isNaN(d) ? null : d;
}
function shortDay(s) {                     // "Oct 6"
  const d = utcDay(s);
  return d ? d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'}) : String(s || '');
}
function longDay(s) {                      // "Tue, Oct 6"
  const d = utcDay(s);
  return d ? d.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC'}) : String(s || '');
}

// Round axis steps (1, 2, 5 × 10ⁿ), integers only — these are counts.
function niceTicks(max, count) {
  count = count || 4;
  if (!(max > 0)) return { top: count, ticks: Array.from({length: count + 1}, (_, i) => i) };
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / mag;
  let step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
  step = Math.max(1, Math.round(step));
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  return { top, ticks };
}

// Monotone cubic through the points: smooth, but never dips below zero or
// overshoots a peak the way a plain Catmull-Rom curve does.
function smoothPath(pts) {
  const n = pts.length;
  if (!n) return '';
  if (n < 3) return 'M' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L');
  const m = [], t = [];
  for (let i = 0; i < n - 1; i++) m.push((pts[i + 1][1] - pts[i][1]) / (pts[i + 1][0] - pts[i][0]));
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : 2 / (1 / m[i - 1] + 1 / m[i]);
  let d = 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1], h = (x1 - x0) / 3;
    d += 'C' + (x0 + h).toFixed(1) + ',' + (y0 + t[i] * h).toFixed(1) + ' ' +
         (x1 - h).toFixed(1) + ',' + (y1 - t[i + 1] * h).toFixed(1) + ' ' + x1.toFixed(1) + ',' + y1.toFixed(1);
  }
  return d;
}

function yAxis(ticks, y, x0, x1, faint) {
  return '<g class="ch-grid' + (faint ? ' faint' : '') + '">' + ticks.map(v =>
    '<line x1="' + x0 + '" x2="' + x1 + '" y1="' + y(v).toFixed(1) + '" y2="' + y(v).toFixed(1) + '"/>' +
    '<text x="' + (x0 - 8) + '" y="' + (y(v) + 4).toFixed(1) + '" text-anchor="end">' + fmtN(v) + '</text>'
  ).join('') + '</g>';
}

// Crosshair + tooltip. `xs` are the hover stops (one per datum); `html(i)`
// builds the tooltip; `dots(i)` returns [{y, cls}] markers to pin on lines.
function chartHover(wrap, opts) {
  const svg = wrap.querySelector(':scope > svg');
  if (!svg) return;
  let tip = wrap.querySelector('.chart-tip');
  if (!tip) { tip = document.createElement('div'); tip.className = 'chart-tip'; wrap.appendChild(tip); }
  const hov = svg.querySelector('.ch-hover');
  const show = e => {
    const r = svg.getBoundingClientRect();
    const mx = e.clientX - r.left;
    if (mx < opts.x0 - 12 || mx > opts.x1 + 12) { hide(); return; }
    let i = 0, best = Infinity;
    opts.xs.forEach((x, k) => { const dd = Math.abs(x - mx); if (dd < best) { best = dd; i = k; } });
    const x = opts.xs[i];
    hov.innerHTML = (opts.band
        ? '<rect class="ch-band-hi" x="' + (x - opts.band / 2).toFixed(1) + '" y="' + opts.y0 + '" width="' + opts.band.toFixed(1) + '" height="' + (opts.y1 - opts.y0) + '" rx="3"/>'
        : '<line class="ch-cross" x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="' + opts.y0 + '" y2="' + opts.y1 + '"/>') +
      (opts.dots ? opts.dots(i).map(d =>
        '<circle class="ch-dot ' + d.cls + '" cx="' + x.toFixed(1) + '" cy="' + d.y.toFixed(1) + '" r="4"/>').join('') : '');
    tip.innerHTML = opts.html(i);
    tip.classList.add('on');
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = x + 14;
    if (left + tw > r.width - 4) left = x - 14 - tw;
    const top = Math.max(4, Math.min(e.clientY - r.top - th / 2, r.height - th - 4));
    tip.style.transform = 'translate(' + Math.max(4, left).toFixed(0) + 'px,' + top.toFixed(0) + 'px)';
  };
  const hide = () => { hov.innerHTML = ''; tip.classList.remove('on'); };
  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointerleave', hide);
}

// Redraw a chart when its container's width changes (and only then).
function observeWidth(el, draw) {
  if (!el || el._ro || typeof ResizeObserver === 'undefined') return;
  el._lastW = el.clientWidth;
  el._ro = new ResizeObserver(() => {
    const w = el.clientWidth;
    if (w && Math.abs(w - (el._lastW || 0)) >= 2) { el._lastW = w; draw(); }
  });
  el._ro.observe(el);
}

// ── Today: rates + outreach trend ──

let _trend = { days: 30, data: null, key: '', show: { sent: true, replies: true, bounces: true }, seq: 0 };

async function loadTrend(quiet) {
  const chart = document.getElementById('trend-chart');
  if (!chart) return;
  const seq = ++_trend.seq;
  if (!quiet && !_trend.data) chart.innerHTML = '<div class="chart-note">Loading the trend…</div>';
  const res = await getJSON('/api/trends?days=' + _trend.days);
  if (seq !== _trend.seq) return;                 // a newer range was picked meanwhile
  if (!res.ok || !res.data || !Array.isArray(res.data.series)) {
    if (quiet && _trend.data) return;             // keep the last good chart on a blip
    _trend.data = null; _trend.key = '';
    document.getElementById('today-rates').hidden = true;
    document.getElementById('today-trend').classList.add('unavailable');
    chart.innerHTML = '<div class="chart-note">' + icon('info') + '<span>' + (res.status === 404
      ? 'The trend isn\'t available on this server yet. Restart <b>mercury dashboard</b> to get it.'
      : res.status >= 500 ? 'The trend failed to load. Check <b>data/mercury.log</b>, then refresh.'
      : 'Can\'t reach the dashboard server for the trend.') + '</span></div>';
    document.getElementById('trend-foot').innerHTML = '&nbsp;';
    return;
  }
  const key = JSON.stringify(res.data);
  if (quiet && key === _trend.key) return;        // nothing changed; don't disturb a hover
  _trend.data = res.data; _trend.key = key;
  document.getElementById('today-trend').classList.remove('unavailable');
  renderRates();
  renderTrend();
  observeWidth(chart, renderTrend);
}

function trendRange(days) {
  if (_trend.days === days) return;
  _trend.days = days;
  document.querySelectorAll('#trend-range button').forEach(b =>
    b.classList.toggle('on', Number(b.dataset.days) === days));
  loadTrend();
}

function trendToggle(btn) {
  const k = btn.dataset.series;
  const on = !_trend.show[k];
  // Never hide every line — an empty frame reads as "no data".
  if (!on && Object.values(_trend.show).filter(Boolean).length === 1) return;
  _trend.show[k] = on;
  btn.classList.toggle('on', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  renderTrend();
}

// "↑ 1.2 pts vs prior 30 days" — upIsGood flips the colour for bounce rate.
function rateDelta(cur, prev, prevSent, upIsGood, days) {
  const span = ' vs prior ' + days + ' days';
  if (cur === null || cur === undefined) return '<span class="delta flat">No sends in this window</span>';
  if (!prevSent || prev === null || prev === undefined) return '<span class="delta flat">No sends in the prior ' + days + ' days</span>';
  const pts = Math.round((cur - prev) * 1000) / 10;
  if (pts === 0) return '<span class="delta flat">' + icon('arrows-left-right') + 'No change</span><span class="delta-span">' + span + '</span>';
  const up = pts > 0, good = up === upIsGood;
  return '<span class="delta ' + (good ? 'good' : 'bad') + '">' + icon(up ? 'trend-up' : 'trend-down') +
    Math.abs(pts).toFixed(1) + ' pts</span><span class="delta-span">' + span + '</span>';
}

function renderRates() {
  const el = document.getElementById('today-rates');
  const d = _trend.data;
  if (!el || !d) return;
  const t = d.totals || {}, p = d.prior || {}, days = d.days || _trend.days;
  const card = (label, rate, sub, delta, tab, tone) =>
    '<button class="kpi" onclick="goTab(\'' + tab + '\')">' +
      '<span class="kpi-label">' + label + '</span>' +
      '<span class="kpi-value' + (rate === null || rate === undefined ? ' zero' : '') + (tone ? ' ' + tone : '') + '">' +
        fmtPct(rate) + '<small>' + sub + '</small></span>' +
      '<span class="kpi-foot">' + delta + '</span>' +
    '</button>';
  const sent = Number(t.sent || 0);
  const bounceTone = (t.bounce_rate || 0) >= 0.05 ? 'is-bad' : '';
  el.innerHTML =
    card('Reply rate', t.reply_rate, fmtN(t.replies) + ' repl' + (Number(t.replies) === 1 ? 'y' : 'ies') + ' of ' + fmtN(sent) + ' sent',
      rateDelta(t.reply_rate, p.reply_rate, p.sent, true, days), 'conversations') +
    card('Positive replies', t.positive_rate, fmtN(t.positive) + ' interested of ' + fmtN(sent) + ' sent',
      rateDelta(t.positive_rate, p.positive_rate, p.sent, true, days), 'conversations') +
    card('Bounce rate', t.bounce_rate, fmtN(t.bounces) + ' bounce' + (Number(t.bounces) === 1 ? '' : 's') + ' of ' + fmtN(sent) + ' sent',
      rateDelta(t.bounce_rate, p.bounce_rate, p.sent, false, days), 'warmup', bounceTone);
  el.hidden = false;
}

const TREND_SERIES = [
  { key: 'sent', label: 'Sent' },
  { key: 'replies', label: 'Replies' },
  { key: 'bounces', label: 'Bounces' },
];

function renderTrend() {
  const wrap = document.getElementById('trend-chart');
  const d = _trend.data;
  if (!wrap || !d) return;
  const W = wrap.clientWidth;
  if (!W) return;                                  // hidden tab; the observer redraws later
  const series = d.series || [];
  const n = series.length;
  const H = W < 560 ? 220 : 268;
  const show = _trend.show;
  const empty = !series.some(r => r.sent || r.replies || r.bounces);
  const max = Math.max(0, ...series.map(r => Math.max(show.sent ? r.sent || 0 : 0,
    show.replies ? r.replies || 0 : 0, show.bounces ? r.bounces || 0 : 0)));
  const { top, ticks } = niceTicks(max, 4);
  const padL = 14 + Math.max(2, String(fmtN(top)).length) * 7, padR = 18, padT = 14, padB = 30;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const x = i => n <= 1 ? (x0 + x1) / 2 : x0 + i * (x1 - x0) / (n - 1);
  const y = v => y1 - (v / top) * (y1 - y0);
  const xs = series.map((_, i) => x(i));

  let svg = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
    'aria-label="Emails sent, replies and bounces per day over the last ' + (d.days || n) + ' days">' +
    '<defs><linearGradient id="tr-fill" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" class="tr-stop-a"/><stop offset="1" class="tr-stop-b"/></linearGradient></defs>' +
    yAxis(ticks, y, x0, x1, empty);

  // x labels: ~6 evenly spaced days (fewer on narrow screens)
  const want = Math.min(n, W < 520 ? 4 : 6);
  const idx = want <= 1 ? [0] : [...new Set(Array.from({length: want}, (_, k) => Math.round(k * (n - 1) / (want - 1))))];
  svg += '<g class="ch-x' + (empty ? ' faint' : '') + '">' + idx.map((i, k) =>
    '<text x="' + x(i).toFixed(1) + '" y="' + (H - 9) + '" text-anchor="' +
      (k === 0 && idx.length > 1 ? 'start' : k === idx.length - 1 && idx.length > 1 ? 'end' : 'middle') + '">' +
      svgEsc(shortDay(series[i].date)) + '</text>').join('') + '</g>';

  if (!empty) {
    if (show.bounces) {
      const bw = Math.max(2, Math.min(6, (x1 - x0) / Math.max(1, n) * 0.35));
      svg += '<g class="tr-bounces">' + series.map((r, i) => r.bounces
        ? '<rect x="' + (x(i) - bw / 2).toFixed(1) + '" y="' + y(r.bounces).toFixed(1) + '" width="' + bw.toFixed(1) +
          '" height="' + (y1 - y(r.bounces)).toFixed(1) + '" rx="1"/>' : '').join('') + '</g>';
    }
    if (show.sent) {
      const pts = series.map((r, i) => [x(i), y(r.sent || 0)]);
      const line = smoothPath(pts);
      svg += '<path class="tr-area" d="' + line + 'L' + x(n - 1).toFixed(1) + ',' + y1 + 'L' + x(0).toFixed(1) + ',' + y1 + 'Z"/>' +
             '<path class="tr-line tr-sent" d="' + line + '"/>';
    }
    if (show.replies) {
      svg += '<path class="tr-line tr-replies" d="' + smoothPath(series.map((r, i) => [x(i), y(r.replies || 0)])) + '"/>';
    }
  }
  svg += '<line class="ch-base" x1="' + x0 + '" x2="' + x1 + '" y1="' + y1 + '" y2="' + y1 + '"/>' +
    '<g class="ch-hover"></g></svg>';

  wrap.innerHTML = svg + (empty
    ? '<div class="chart-empty">' + icon('chart-line-up') + '<span>No sends yet — the trend appears after Mercury\'s first emails go out.</span></div>'
    : '');

  if (!empty) {
    chartHover(wrap, {
      xs, x0, x1, y0, y1,
      dots: i => TREND_SERIES.filter(s => show[s.key] && s.key !== 'bounces')
        .map(s => ({ y: y(series[i][s.key] || 0), cls: 'tr-' + s.key })),
      html: i => {
        const r = series[i];
        return '<div class="tip-date">' + svgEsc(longDay(r.date)) + '</div>' +
          TREND_SERIES.map(s => '<div class="tip-row' + (show[s.key] ? '' : ' off') + '"><span class="sw sw-' + s.key + '"></span>' +
            s.label + '<b>' + fmtN(r[s.key]) + '</b></div>').join('') +
          (r.positive ? '<div class="tip-row sub">of which interested<b>' + fmtN(r.positive) + '</b></div>' : '');
      },
    });
  }

  const t = d.totals || {};
  document.getElementById('trend-foot').innerHTML = Number(t.sent || 0)
    ? '<b>' + fmtPct(t.reply_rate) + '</b> reply rate over ' + (d.days || n) + ' days &middot; <b>' +
      fmtN(t.bounces) + '</b> bounce' + (Number(t.bounces) === 1 ? '' : 's')
    : 'Nothing sent in the last ' + (d.days || n) + ' days';
}

// ── Warm-up: a new inbox earns its volume ──
//
// Mercury caps what an inbox may send each day and raises the cap on a
// 28-day schedule. This tab shows where the inbox is on that ramp, whether
// bounces are holding it back, the setup checklist for each week, and the
// DNS records that decide whether mail lands in the inbox at all.

let _wu = { data: null, key: '', sel: null, open: {}, dns: {}, dnsLoading: {}, seq: 0 };

const WU_STATUS = {
  not_started: ['Not started', 'idle'],
  warming:     ['Warming up', 'active'],
  paused:      ['Paused', 'waiting'],
  complete:    ['Fully warmed', 'good'],
};
const wuBadge = st => { const m = WU_STATUS[st] || [String(st || 'Unknown'), 'idle']; return toneBadge(m[1], m[0]); };
const wuEnc = email => encodeURIComponent(email);
const wuDomain = email => String(email || '').split('@')[1] || '';

function wuInbox() {
  const list = (_wu.data && _wu.data.inboxes) || [];
  return list.find(b => b.email === _wu.sel) || null;
}

function wuBusy() {
  const a = document.activeElement;
  return modalOpen() || wuAddOpen() || !!(a && a.closest && a.closest('#warmup') && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
}

async function loadWarmup(quiet) {
  const body = document.getElementById('wu-body');
  const seq = ++_wu.seq;
  const res = await getJSON('/api/warmup');
  if (seq !== _wu.seq) return;
  const addBtn = document.getElementById('wu-add-btn');
  if (!res.ok || !res.data || !Array.isArray(res.data.inboxes)) {
    if (quiet && _wu.data) return;
    _wu.data = null; _wu.key = '';
    addBtn.hidden = true;
    document.getElementById('wu-switch').innerHTML = '';
    body.innerHTML = unavailableState(res, 'fire', 'Warm-up');
    return;
  }
  addBtn.hidden = false;
  const key = JSON.stringify(res.data);
  if (quiet && key === _wu.key) return;
  _wu.data = res.data; _wu.key = key;
  const list = res.data.inboxes;
  if (!list.some(b => b.email === _wu.sel)) {
    const pick = list.find(b => b.is_sender) || list.find(b => b.email === res.data.active_email) || list[0];
    _wu.sel = pick ? pick.email : null;
  }
  wuNavFlag();
  renderWarmup();
}

function wuNavFlag() {
  const el = document.getElementById('nav-warmup');
  if (!el) return;
  const list = (_wu.data && _wu.data.inboxes) || [];
  const worst = list.some(b => b.health && b.health.gate === 'pause') ? 'bad'
    : list.some(b => b.health && b.health.gate === 'hold') ? 'wait' : '';
  el.hidden = !worst;
  el.className = 'nav-flag ' + worst;
  el.title = worst === 'bad' ? 'An inbox is paused' : worst === 'wait' ? 'A ramp is on hold' : '';
}

function wuSelect(i) {
  const x = ((_wu.data && _wu.data.inboxes) || [])[i];
  if (!x) return;
  _wu.sel = x.email;
  renderWarmup();
}

function renderWarmup() {
  const body = document.getElementById('wu-body');
  const sw = document.getElementById('wu-switch');
  const list = (_wu.data && _wu.data.inboxes) || [];
  if (!list.length) {
    sw.innerHTML = '';
    body.innerHTML = emptyState('envelope-simple', 'No inbox to warm up yet',
      'Add the mailbox Mercury sends from and it gets a 28-day ramp, bounce monitoring and a setup checklist.') ;
    body.querySelector('.empty').insertAdjacentHTML('beforeend',
      '<div class="btn-group" style="justify-content:center"><button class="btn btn-primary btn-sm" onclick="wuOpenAdd()">' +
      icon('plus') + 'Add inbox</button></div>');
    return;
  }
  const b = wuInbox();

  sw.innerHTML = list.length > 1
    ? '<div class="toolbar wu-switch"><div class="segmented wu-seg" role="tablist" aria-label="Inbox">' + list.map((x, i) =>
        '<button role="tab" aria-selected="' + (x.email === _wu.sel) + '" class="' + (x.email === _wu.sel ? 'on' : '') +
          '" onclick="wuSelect(' + i + ')">' +
          '<span class="wu-seg-ic t-' + ((WU_STATUS[x.status] || [0, 'idle'])[1]) + '">' + icon(TONE_ICON[(WU_STATUS[x.status] || [0, 'idle'])[1]]) + '</span>' +
          '<span class="mono">' + escHtml(x.email) + '</span>' +
          (x.is_sender ? '<span class="wu-seg-tag">sender</span>' : '') + '</button>').join('') +
      '</div></div>'
    : '<div class="toolbar wu-switch"><span class="wu-one">' + icon('envelope-simple') +
        '<span class="mono">' + escHtml(b.email) + '</span>' +
        (b.is_sender ? '<span class="toolbar-note">the inbox Mercury sends from</span>' : '') + '</span></div>';

  body.innerHTML =
    '<div class="kpis" id="wu-hero">' + wuHero(b) + '</div>' +
    '<div class="grid-2-1">' +
      '<div class="stack">' +
        '<section class="panel" id="wu-ramp-panel">' + wuRampPanel(b) + '</section>' +
        '<section class="panel" id="wu-plan-panel"></section>' +
      '</div>' +
      '<div class="stack">' +
        '<section class="panel" id="wu-dns-panel"></section>' +
        '<section class="panel" id="wu-notes-panel">' + wuNotesPanel(b) + '</section>' +
        '<section class="panel">' + wuRulesPanel(b) + '</section>' +
      '</div>' +
    '</div>';

  renderWuPlan();
  renderWuDns();
  renderWuRamp();
  observeWidth(document.getElementById('wu-ramp'), renderWuRamp);
  if (!_wu.dns[wuDomain(b.email)]) loadWuDns(false);
}

function wuHero(b) {
  const st = b.status;
  const started = st !== 'not_started';
  const cap = b.today_cap, target = b.target_daily;
  const kpi = (label, value, foot, extra, cls) =>
    '<div class="kpi static' + (cls ? ' ' + cls : '') + '"><span class="kpi-label">' + label + '</span>' +
      value + (extra || '') + '<span class="kpi-foot">' + foot + '</span></div>';

  // Today's cap
  const capFoot = st === 'complete' ? 'Ramp complete &middot; full volume'
    : st === 'not_started' ? 'Not started'
    : '<b>Day ' + fmtN(b.day) + '</b> of 28' + (st === 'paused' ? ' &middot; paused' : '');
  const capVal = '<span class="kpi-value' + (cap === null || cap === undefined ? ' zero' : '') + '">' +
    (cap === null || cap === undefined ? '—' : fmtN(cap)) + '<small>/ ' + fmtN(target) + ' target</small></span>';

  // Sent today
  const sent = Number(b.sent_today || 0);
  const pct = cap ? Math.min(100, sent / cap * 100) : 0;
  const left = cap ? Math.max(0, cap - sent) : 0;
  const halted = st === 'paused' || (b.health && b.health.gate === 'pause');
  const sentFoot = !started ? 'Sends wait until the warm-up starts'
    : halted ? 'Paused &middot; nothing sends from this inbox'
    : !cap ? 'No sends allowed today'
    : left ? '<b>' + fmtN(left) + '</b> more allowed today'
    : 'Cap reached &middot; the rest wait for tomorrow';
  const sentVal = '<span class="kpi-value tight' + (sent ? '' : ' zero') + '">' + fmtN(sent) +
    (cap ? '<small>/ ' + fmtN(cap) + '</small>' : '') + '</span>';
  const bar = '<span class="kpi-bar' + (cap && !left ? ' full' : '') + '"><span style="width:' + pct.toFixed(1) + '%"></span></span>';

  // Bounce rate (7d) + gate
  const h = b.health || {};
  const gate = h.gate || 'ok';
  const gateFoot = gate === 'pause' ? toneBadge('bad', 'Paused' + (h.reason ? ': ' + h.reason : ''))
    : gate === 'hold' ? toneBadge('waiting', 'Ramp on hold' + (h.reason ? ': ' + h.reason : ''))
    : toneBadge('good', h.sent_7d ? 'Healthy' : 'Healthy · nothing sent yet');
  const bVal = '<span class="kpi-value' + (h.bounce_rate === null || h.bounce_rate === undefined ? ' zero' : '') +
    (gate === 'pause' ? ' is-bad' : gate === 'hold' ? ' is-wait' : '') + '">' + fmtPct(h.bounce_rate) +
    '<small>' + fmtN(h.sent_7d) + ' sent in 7 days</small></span>';

  // Status + actions
  const act = (a, label, cls, ic) => '<button class="btn ' + cls + ' btn-sm" onclick="wuAction(\'' + a + '\')">' +
    (ic ? icon(ic) : '') + label + '</button>';
  let actions = '';
  if (st === 'not_started') actions += act('start', 'Start warm-up', 'btn-primary', 'play');
  if (st === 'warming') actions += act('pause', 'Pause', 'btn-secondary', 'pause');
  if (st === 'paused') actions += act('resume', 'Resume', 'btn-primary', 'play');
  if (st !== 'not_started') actions += act('reset', 'Reset', 'btn-secondary', 'arrow-counter-clockwise').replace('<button ', '<button title="Reset the ramp to day one" ');
  if (!b.is_sender) actions += '<button class="btn-square sm" title="Remove this inbox" aria-label="Remove this inbox" ' +
    'onclick="wuAction(\'remove\')">' + icon('trash') + '</button>';
  const m = WU_STATUS[st] || [st, 'idle'];
  const stVal = '<span class="kpi-value wu-status t-' + m[1] + '">' + icon(TONE_ICON[m[1]]) + escHtml(m[0]) + '</span>';
  const since = b.start_date ? '<span class="kpi-sub">' + (st === 'not_started' ? 'Starts ' : 'Started ') + escHtml(shortDay(b.start_date)) + '</span>' : '';

  return kpi('Today\'s cap', capVal, capFoot) +
    kpi('Sent today', sentVal, sentFoot, bar) +
    kpi('Bounce rate (7d)', bVal, gateFoot, '', 'gate-' + gate) +
    kpi('Status', stVal, '<span class="kpi-actions">' + actions + '</span>', since, 'wu-status-card');
}

function wuRampPanel(b) {
  const started = b.status !== 'not_started' && (b.plan || []).length;
  return '<div class="panel-head wrap"><div><h3>Ramp</h3><p>' + (started
      ? 'Planned daily cap for 28 days, with what actually went out.'
      : 'Four weeks from a handful a day to your target.') + '</p></div>' +
      (started ? '<div class="legend static">' +
        '<span class="legend-btn"><span class="sw sw-cap"></span>Planned cap</span>' +
        '<span class="legend-btn"><span class="sw sw-sent-bar"></span>Sent</span>' +
        '<span class="legend-btn"><span class="sw sw-target"></span>Target</span></div>' : '') +
    '</div>' +
    (started
      ? '<div class="chart-wrap ramp" id="wu-ramp"></div>'
      : '<div class="panel-body">' + emptyState('fire', 'Start the warm-up to see your ramp',
          'Day one allows a few emails; the cap climbs every day until it reaches <b>' + fmtN(b.target_daily) +
          ' a day</b>. Bounces above the safe line pause the climb automatically.') + '</div>') +
    '<div class="panel-foot wu-ramp-foot">' +
      '<div class="wu-inline">' +
        '<span class="wu-field"><label for="wu-target">Target per day</label>' +
        '<input class="form-input sm" id="wu-target" type="number" min="5" max="500" step="1" value="' + escHtml(String(b.target_daily || '')) + '"></span>' +
        '<span class="wu-field"><label for="wu-start">Start</label>' +
        '<input class="form-input sm" id="wu-start" type="date" value="' + escHtml(String(b.start_date || '').slice(0, 10)) + '"></span>' +
        '<button class="btn btn-secondary btn-sm" onclick="wuSaveSettings()">Save</button>' +
      '</div>' +
      (b.status === 'not_started'
        ? '<button class="btn btn-primary btn-sm" onclick="wuAction(\'start\')">' + icon('play') + 'Start warm-up</button>'
        : '<span class="toolbar-note">' + (b.day ? 'Day <b>' + b.day + '</b> of 28' : '') + '</span>') +
    '</div>';
}

function renderWuRamp() {
  const wrap = document.getElementById('wu-ramp');
  const b = wuInbox();
  if (!wrap || !b) return;
  const plan = b.plan || [];
  const W = wrap.clientWidth;
  if (!W || !plan.length) return;
  const n = plan.length, H = W < 560 ? 220 : 252;
  const target = Number(b.target_daily || 0);
  const max = Math.max(target, ...plan.map(p => Math.max(p.cap || 0, p.sent || 0)));
  const { top, ticks } = niceTicks(max * 1.08, 4);
  const padL = 14 + Math.max(2, String(top).length) * 7, padR = 12, padT = 26, padB = 30;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const slot = (x1 - x0) / n, bw = Math.max(3, Math.min(22, slot * 0.62));
  const cx = i => x0 + slot * (i + 0.5);
  const y = v => y1 - (v / top) * (y1 - y0);
  const todayIdx = b.day ? plan.findIndex(p => p.day === b.day) : -1;
  const curWeek = todayIdx >= 0 ? Math.floor(todayIdx / 7) : -1;

  let svg = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
    'aria-label="28-day send ramp: planned daily cap and emails actually sent">';
  // week bands, labelled W1–W4
  for (let w = 0; w * 7 < n; w++) {
    const bx = x0 + w * 7 * slot, bwid = Math.min(7, n - w * 7) * slot;
    svg += '<rect class="rp-band' + (w % 2 ? ' alt' : '') + (w === curWeek ? ' cur' : '') + '" x="' + bx.toFixed(1) + '" y="' + (y0 - 20) +
      '" width="' + bwid.toFixed(1) + '" height="' + (y1 - y0 + 20) + '" rx="6"/>' +
      '<text class="rp-week' + (w === curWeek ? ' cur' : '') + '" x="' + (bx + 8).toFixed(1) + '" y="' + (y0 - 6) + '">W' + (w + 1) + '</text>';
  }
  svg += yAxis(ticks, y, x0, x1, false);
  // planned cap (soft) with actual sent laid over it (solid)
  svg += '<g>' + plan.map((p, i) => {
    const capH = y1 - y(p.cap || 0);
    let r = '<rect class="rp-cap' + (i === todayIdx ? ' today' : '') + (todayIdx >= 0 && i > todayIdx ? ' future' : '') +
      '" x="' + (cx(i) - bw / 2).toFixed(1) + '" y="' + y(p.cap || 0).toFixed(1) + '" width="' + bw.toFixed(1) +
      '" height="' + Math.max(0, capH).toFixed(1) + '" rx="3"/>';
    if (p.sent !== null && p.sent !== undefined && p.sent > 0) {
      r += '<rect class="rp-sent' + (p.sent > (p.cap || 0) ? ' over' : '') + '" x="' + (cx(i) - bw / 2).toFixed(1) + '" y="' + y(p.sent).toFixed(1) +
        '" width="' + bw.toFixed(1) + '" height="' + (y1 - y(p.sent)).toFixed(1) + '" rx="3"/>';
    }
    return r;
  }).join('') + '</g>';
  // target line
  if (target) {
    svg += '<line class="rp-target" x1="' + x0 + '" x2="' + x1 + '" y1="' + y(target).toFixed(1) + '" y2="' + y(target).toFixed(1) + '"/>' +
      '<text class="rp-target-l" x="' + (x1 - 4) + '" y="' + (y(target) - 6).toFixed(1) + '" text-anchor="end">Target ' + fmtN(target) + '/day</text>';
  }
  // x labels: the first day of each week, plus a Today marker
  const xl = [];
  for (let i = 0; i < n; i += 7) xl.push(i);
  if (n - 1 - xl[xl.length - 1] >= 4) xl.push(n - 1);
  svg += '<g class="ch-x">' + xl.filter(i => todayIdx < 0 || Math.abs(i - todayIdx) * slot > 46).map(i =>
    '<text x="' + cx(i).toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle">' + svgEsc(shortDay(plan[i].date)) + '</text>').join('') + '</g>';
  if (todayIdx >= 0) {
    svg += '<line class="rp-today" x1="' + cx(todayIdx).toFixed(1) + '" x2="' + cx(todayIdx).toFixed(1) + '" y1="' + y0 + '" y2="' + (y1 + 4) + '"/>' +
      '<text class="rp-today-l" x="' + cx(todayIdx).toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle">Today</text>';
  }
  svg += '<line class="ch-base" x1="' + x0 + '" x2="' + x1 + '" y1="' + y1 + '" y2="' + y1 + '"/><g class="ch-hover"></g></svg>';
  wrap.innerHTML = svg;

  chartHover(wrap, {
    xs: plan.map((_, i) => cx(i)), x0, x1, y0: y0 - 20, y1, band: slot * 0.92,
    html: i => {
      const p = plan[i];
      const when = i === todayIdx ? 'Today' : (todayIdx >= 0 && i > todayIdx) ? 'Planned' : '';
      return '<div class="tip-date">Day ' + p.day + ' &middot; ' + svgEsc(longDay(p.date)) + '</div>' +
        '<div class="tip-row"><span class="sw sw-cap"></span>Cap<b>' + fmtN(p.cap) + '</b></div>' +
        '<div class="tip-row"><span class="sw sw-sent-bar"></span>Sent<b>' + (p.sent === null || p.sent === undefined ? '—' : fmtN(p.sent)) + '</b></div>' +
        (when ? '<div class="tip-row sub">' + when + '</div>' : '');
    },
  });
}

function renderWuPlan() {
  const el = document.getElementById('wu-plan-panel');
  const b = wuInbox();
  if (!el || !b) return;
  const weeks = b.weeks || [];
  const cur = b.current_week === null || b.current_week === undefined ? 0 : b.current_week;
  const open = _wu.open[b.email] || (_wu.open[b.email] = new Set([cur]));
  const allTasks = weeks.flatMap(w => w.tasks || []);
  const doneAll = allTasks.filter(t => t.done).length;

  el.innerHTML = '<div class="panel-head"><div><h3>Plan</h3><p>What to do each week so the ramp lands in the inbox, not spam.</p></div>' +
      (allTasks.length ? '<span class="toolbar-note"><b>' + doneAll + '</b> of ' + allTasks.length + ' done</span>' : '') + '</div>' +
    (weeks.length ? '<div class="wu-weeks">' + weeks.map(w => {
      const tasks = w.tasks || [];
      const done = tasks.filter(t => t.done).length;
      const isCur = w.week === cur, isPast = w.week < cur, isOpen = open.has(w.week);
      const state = isCur ? '<span class="wu-now">This week</span>'
        : (tasks.length && done === tasks.length) ? '<span class="wu-count good">' + icon('check-circle') + 'Done</span>'
        : isPast ? '<span class="wu-count">' + done + '/' + tasks.length + ' done</span>'
        : '<span class="wu-count">' + tasks.length + ' task' + (tasks.length === 1 ? '' : 's') + '</span>';
      return '<div class="wu-week' + (isCur ? ' cur' : '') + (isPast ? ' past' : '') + (isOpen ? ' open' : '') + '">' +
        '<button class="wu-week-head" aria-expanded="' + isOpen + '" onclick="wuToggleWeek(' + w.week + ')">' +
          '<span class="wu-week-n">' + (w.week === 0 ? 'Prep' : 'W' + w.week) + '</span>' +
          '<span class="wu-week-t"><b>' + escHtml(w.title) + '</b><small>' + escHtml(w.range || '') + '</small></span>' +
          state + icon('caret-down', 'wu-caret') +
        '</button>' +
        (isOpen ? '<div class="wu-tasks">' + tasks.map(t => wuTask(b, t)).join('') + '</div>' : '') +
      '</div>';
    }).join('') + '</div>'
    : '<div class="panel-body"><p class="muted" style="font-size:13px">No checklist for this inbox.</p></div>');
}

function wuTask(b, t) {
  if (t.key === 'dns') {
    return '<div class="wu-task auto' + (t.done ? ' done' : '') + '">' +
      '<span class="wu-auto-ic' + (t.done ? ' ok' : '') + '">' + icon(t.done ? 'check-circle' : 'circle-dashed') + '</span>' +
      '<span class="wu-task-l">' + escHtml(t.label) + '</span>' +
      '<span class="wu-auto-tag">auto</span>' +
      '<button class="link-btn" onclick="wuScrollDns()">See DNS' + icon('arrow-right') + '</button></div>';
  }
  const id = 'wut-' + escHtml(t.key);
  return '<label class="wu-task' + (t.done ? ' done' : '') + '" for="' + id + '">' +
    '<input type="checkbox" id="' + id + '" ' + (t.done ? 'checked ' : '') +
      'onchange="wuToggleTask(\'' + escHtml(t.key) + '\', this.checked)">' +
    '<span class="wu-task-l">' + escHtml(t.label) + '</span></label>';
}

function wuToggleWeek(w) {
  const b = wuInbox();
  if (!b) return;
  const s = _wu.open[b.email] || (_wu.open[b.email] = new Set());
  if (s.has(w)) s.delete(w); else s.add(w);
  renderWuPlan();
}

async function wuToggleTask(key, done) {
  const b = wuInbox();
  if (!b) return;
  const task = (b.weeks || []).flatMap(w => w.tasks || []).find(t => t.key === key);
  if (!task) return;
  task.done = done;                                   // optimistic
  renderWuPlan();
  const res = await postJSON('/api/warmup/inboxes/' + wuEnc(b.email) + '/task', { key, done });
  if (!res.ok) {
    task.done = !done;
    renderWuPlan();
    showToast('Couldn\'t save that: ' + res.error, 'error');
    return;
  }
  _wu.key = JSON.stringify(_wu.data);                 // keep the quiet refresh from flickering
}

function wuScrollDns() {
  const el = document.getElementById('wu-dns-panel');
  if (el) { el.scrollIntoView({ block: 'center', behavior: 'smooth' }); el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1200); }
}

function agoShort(d) {
  if (!d) return '';
  const min = Math.round((Date.now() - d) / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return min + ' min ago';
  if (min < 24 * 60) return Math.round(min / 60) + ' h ago';
  return relWhen(d);
}

const DNS_ICON = {
  pass: ['check-circle', 'Pass'], warn: ['warning', 'Needs attention'],
  fail: ['x-circle', 'Missing'], unknown: ['question', 'Couldn\'t check'],
};

async function loadWuDns(force) {
  const b = wuInbox();
  if (!b) return;
  const domain = wuDomain(b.email);
  if (_wu.dnsLoading[domain]) return;
  _wu.dnsLoading[domain] = true;
  if (force) renderWuDns();
  const res = await getJSON('/api/warmup/dns' + (domain ? '?domain=' + encodeURIComponent(domain) : ''));
  _wu.dnsLoading[domain] = false;
  _wu.dns[domain] = res.ok && res.data && Array.isArray(res.data.checks) ? res.data : { error: res };
  if (wuInbox() && wuDomain(wuInbox().email) === domain) renderWuDns();
}

function renderWuDns() {
  const el = document.getElementById('wu-dns-panel');
  const b = wuInbox();
  if (!el || !b) return;
  const domain = wuDomain(b.email);
  const d = _wu.dns[domain];
  const loading = _wu.dnsLoading[domain];
  const head = '<div class="panel-head"><div><h3>Domain authentication</h3><p>The DNS records that tell inboxes <span class="mono">' +
    escHtml(domain || 'your domain') + '</span> is really you.</p></div></div>';
  let bodyHtml;
  if (!d) {
    bodyHtml = '<div class="panel-body"><div class="loading-note">Checking DNS…</div></div>';
  } else if (d.error) {
    bodyHtml = '<div class="panel-body"><div class="chart-note inline">' + icon('info') + '<span>' +
      (d.error.status === 404 ? 'DNS checks aren\'t available on this server yet.' : 'Couldn\'t run the DNS check right now.') +
      '</span></div></div>';
  } else {
    const pass = d.checks.filter(c => c.status === 'pass').length;
    bodyHtml = '<div class="dns-list">' + d.checks.map(c => {
      const m = DNS_ICON[c.status] || DNS_ICON.unknown;
      return '<div class="dns-row s-' + escHtml(c.status) + '">' +
        '<span class="dns-ic">' + icon(m[0]) + '</span>' +
        '<div class="dns-main">' +
          '<div class="dns-top"><b>' + escHtml(c.label || String(c.key).toUpperCase()) + '</b><span class="dns-st">' + m[1] + '</span></div>' +
          (c.detail ? '<div class="dns-detail">' + escHtml(c.detail) + '</div>' : '') +
          (c.record ? '<div class="dns-rec"><code title="' + escHtml(c.record) + '">' + escHtml(c.record) + '</code>' +
            '<button class="btn-square xs ghost" title="Copy record" aria-label="Copy ' + escHtml(c.label || c.key) + ' record" ' +
            'data-copy="' + escHtml(c.record) + '" onclick="wuCopy(this)">' + icon('copy') + '</button></div>' : '') +
        '</div></div>';
    }).join('') + '</div>';
    bodyHtml += '<div class="panel-foot"><span class="dns-sum"><span>' + (pass === d.checks.length
        ? toneBadge('good', 'All ' + pass + ' records pass')
        : '<b class="mono">' + pass + '/' + d.checks.length + '</b> passing') + '</span>' +
      (d.checked_at ? '<small class="dns-checked">Checked ' + escHtml(agoShort(parseUTC(d.checked_at))) + '</small>' : '') + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="loadWuDns(true)"' + (loading ? ' disabled' : '') + '>' +
        icon('arrow-clockwise') + (loading ? 'Checking…' : 'Re-check') + '</button></div>';
  }
  if (d && d.error) {
    bodyHtml += '<div class="panel-foot"><span class="muted">' + escHtml(domain) + '</span><button class="btn btn-secondary btn-sm" onclick="loadWuDns(true)"' +
      (loading ? ' disabled' : '') + '>' + icon('arrow-clockwise') + (loading ? 'Checking…' : 'Re-check') + '</button></div>';
  }
  el.innerHTML = head + bodyHtml;
}

async function wuCopy(btn) {
  const text = btn.dataset.copy || '';
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch { /* nothing else to try */ }
    ta.remove();
  }
  btn.innerHTML = icon('check');
  setTimeout(() => { btn.innerHTML = icon('copy'); }, 1200);
}

function wuNotesPanel(b) {
  return '<div class="panel-head"><div><h3>Notes</h3><p>Anything worth remembering about this inbox.</p></div></div>' +
    '<div class="panel-body"><textarea class="form-input wu-notes" id="wu-notes" rows="4" ' +
      'placeholder="e.g. Google Postmaster verified Oct 2; forwarding set up for replies" ' +
      'onblur="wuSaveNotes(this)">' + escHtml(b.notes || '') + '</textarea>' +
      '<div class="wu-notes-hint" id="wu-notes-hint">Saves when you click away.</div></div>';
}

function wuRulesPanel(b) {
  return '<div class="panel-head"><div><h3>How the ramp protects you</h3><p>Rules Mercury enforces while warming.</p></div></div>' +
    '<div class="panel-body"><ul class="wu-rules">' +
      '<li>' + icon('shield-check') + '<span>Never sends more than today\'s cap from this inbox, no matter how much is approved.</span></li>' +
      '<li>' + icon('pause') + '<span>Holds the ramp when bounces climb, and pauses sending if they spike.</span></li>' +
      '<li>' + icon('globe-simple') + '<span>Re-checks SPF, DKIM and DMARC so a broken record is caught before it costs you.</span></li>' +
    '</ul></div>';
}

async function wuSaveNotes(ta) {
  const b = wuInbox();
  if (!b || (b.notes || '') === ta.value) return;
  const hint = document.getElementById('wu-notes-hint');
  if (hint) hint.textContent = 'Saving…';
  const res = await postJSON('/api/warmup/inboxes/' + wuEnc(b.email), { notes: ta.value });
  if (res.ok) {
    b.notes = ta.value;
    _wu.key = JSON.stringify(_wu.data);
    if (hint) hint.textContent = 'Saved.';
  } else {
    if (hint) hint.textContent = 'Not saved: ' + res.error;
    showToast('Couldn\'t save notes: ' + res.error, 'error');
  }
}

async function wuSaveSettings() {
  const b = wuInbox();
  if (!b) return;
  const target = Number(document.getElementById('wu-target').value);
  const start = document.getElementById('wu-start').value;
  if (!Number.isFinite(target) || target < 1) { showToast('Set a target of at least 1 email a day.', 'error'); return; }
  const body = {};
  if (target !== Number(b.target_daily)) body.target_daily = Math.round(target);
  if (start && start !== String(b.start_date || '').slice(0, 10)) body.start_date = start;
  if (!Object.keys(body).length) { showToast('Nothing changed.', 'success'); return; }
  const res = await postJSON('/api/warmup/inboxes/' + wuEnc(b.email), body);
  if (res.ok) { showToast('Ramp updated.', 'success'); document.activeElement && document.activeElement.blur(); loadWarmup(); }
  else showToast('Couldn\'t update the ramp: ' + res.error, 'error');
}

const WU_CONFIRM = {
  reset: b => ({ title: 'Reset the ramp for ' + b.email + '?',
    copy: 'The cap goes back to day one and climbs again over 28 days. Sent history and your checklist are kept.', ok: 'Reset ramp' }),
  remove: b => ({ title: 'Remove ' + b.email + ' from warm-up?',
    copy: 'Mercury stops tracking its ramp and checklist. This doesn\'t touch the mailbox itself.', ok: 'Remove inbox' }),
};
const WU_DONE = {
  start: 'Warm-up started — today\'s cap is live.', pause: 'Warm-up paused. Nothing sends from this inbox until you resume.',
  resume: 'Warm-up resumed.', reset: 'Ramp reset to day one.', remove: 'Inbox removed from warm-up.',
};

async function wuAction(action) {
  const b = wuInbox();
  if (!b) return;
  const email = b.email;
  if (WU_CONFIRM[action] && !(await confirmModal(WU_CONFIRM[action](b)))) return;
  const res = await postJSON('/api/warmup/inboxes/' + wuEnc(email) + '/action', { action });
  if (res.ok) {
    showToast(WU_DONE[action] || 'Done.', 'success');
    if (action === 'remove' && _wu.sel === email) _wu.sel = null;
  } else {
    showToast('Couldn\'t ' + action + ': ' + res.error, 'error');
  }
  loadWarmup();
}

// Add-inbox dialog
let _wuPrevFocus = null;
function wuAddOpen() { const m = document.getElementById('wu-modal'); return !!m && m.classList.contains('open'); }

function wuOpenAdd() {
  const m = document.getElementById('wu-modal');
  _wuPrevFocus = document.activeElement;
  document.getElementById('wu-new-email').value = '';
  document.getElementById('wu-new-start').value = '';
  document.getElementById('wu-new-target').value = '';
  document.getElementById('wu-new-error').hidden = true;
  m.classList.add('open');
  m.setAttribute('aria-hidden', 'false');
  setTimeout(() => document.getElementById('wu-new-email').focus(), 0);
}

function wuCloseAdd() {
  const m = document.getElementById('wu-modal');
  if (!m.classList.contains('open')) return;
  m.classList.remove('open');
  m.setAttribute('aria-hidden', 'true');
  if (_wuPrevFocus && document.contains(_wuPrevFocus)) _wuPrevFocus.focus({ preventScroll: true });
}

async function wuSubmitAdd(e) {
  e.preventDefault();
  const email = document.getElementById('wu-new-email').value.trim();
  const start = document.getElementById('wu-new-start').value;
  const target = document.getElementById('wu-new-target').value;
  const err = document.getElementById('wu-new-error');
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    err.textContent = 'Enter a full email address, like you@yourdomain.co.';
    err.hidden = false;
    return;
  }
  const body = { email };
  if (start) body.start_date = start;
  if (target) body.target_daily = Math.round(Number(target));
  const ok = document.getElementById('wu-new-ok');
  ok.disabled = true;
  const res = await postJSON('/api/warmup/inboxes', body);
  ok.disabled = false;
  if (!res.ok) { err.textContent = 'Couldn\'t add it: ' + res.error; err.hidden = false; return; }
  wuCloseAdd();
  _wu.sel = email;
  showToast('Added ' + email + ' to warm-up.', 'success');
  loadWarmup();
}

// ── Sending activity heatmap (GitHub-style, Monday-first, UTC days) ──

let _hmSig = '';

async function loadHeatmap(quiet) {
  const grid = document.getElementById('hm-grid');
  if (!grid) return;
  const res = await getJSON('/api/heatmap?weeks=53');
  if (!res.ok || !res.data || !Array.isArray(res.data.days)) {
    if (!quiet) grid.innerHTML = '<div class="chart-note">Activity isn\'t available on this server yet.</div>';
    return;
  }
  const sig = JSON.stringify([res.data.end, res.data.total_sent, res.data.max]);
  if (quiet && sig === _hmSig) return;   // nothing changed; don't redraw under the cursor
  _hmSig = sig;
  renderHeatmap(res.data);
}

function hmDate(iso, opts) {
  return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-US', Object.assign({ timeZone: 'UTC' }, opts));
}

function renderHeatmap(data) {
  const grid = document.getElementById('hm-grid');
  const days = data.days;
  const max = data.max || 0;
  // Four buckets relative to the busiest day, like GitHub; any send is at least level 1.
  const level = n => !n ? 0 : (max <= 4 ? Math.min(4, n) : Math.min(4, Math.ceil(n / max * 4)));
  const weeks = Math.ceil(days.length / 7);

  let months = '', lastMonth = -1;
  for (let w = 0; w < weeks; w++) {
    const first = days[w * 7];
    const m = new Date(first.date + 'T00:00:00Z').getUTCMonth();
    // Label a column when its Monday starts a new month (skip a cramped label at the far left).
    if (m !== lastMonth && !(w === 0 && new Date(first.date + 'T00:00:00Z').getUTCDate() > 21)) {
      months += '<span style="grid-column:' + (w + 1) + '">' + hmDate(first.date, { month: 'short' }) + '</span>';
    }
    lastMonth = m;
  }
  const cells = days.map(d =>
    '<i class="hm-c l' + level(d.sent) + '" data-d="' + d.date + '" data-n="' + d.sent +
    '" data-r="' + (d.replies || 0) + '"></i>').join('');

  grid.innerHTML =
    '<div class="hm" style="--weeks:' + weeks + '">' +
      '<div class="hm-months">' + months + '</div>' +
      '<div class="hm-days"><span></span><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span></div>' +
      '<div class="hm-grid" role="img" aria-label="' + data.total_sent + ' emails sent in the last year">' + cells + '</div>' +
    '</div><div class="hm-tip" hidden></div>';

  // Narrow screens scroll; start at the most recent week, like GitHub.
  grid.scrollLeft = grid.scrollWidth;

  const fmt = n => Number(n || 0).toLocaleString('en-US');
  document.getElementById('hm-total').innerHTML =
    '<b>' + fmt(data.total_sent) + '</b> sent &middot; <b>' + fmt(data.active_days) + '</b> active days';
  const best = data.best_day
    ? 'Busiest day <b>' + hmDate(data.best_day.date, { month: 'short', day: 'numeric', year: 'numeric' }) +
      '</b> (' + fmt(data.best_day.sent) + ')'
    : 'No sends yet';
  document.getElementById('hm-stats').innerHTML =
    icon('fire') + 'Current streak <b>' + data.streak_current + '</b> day' + (data.streak_current === 1 ? '' : 's') +
    '<span class="hm-dot">&middot;</span>Longest <b>' + data.streak_longest + '</b>' +
    '<span class="hm-dot">&middot;</span>' + best;
}

(function wireHeatmapTip() {
  const wrap = document.getElementById('hm-grid');
  if (!wrap) return;
  wrap.addEventListener('mouseover', e => {
    const c = e.target.closest('.hm-grid .hm-c');
    const tip = wrap.querySelector('.hm-tip');
    if (!c || !tip) return;
    const n = Number(c.dataset.n), r = Number(c.dataset.r);
    tip.innerHTML = '<b>' + (n ? n + ' email' + (n === 1 ? '' : 's') + ' sent' : 'No emails sent') + '</b>' +
      (r ? '<span>' + r + ' repl' + (r === 1 ? 'y' : 'ies') + '</span>' : '') +
      '<small>' + hmDate(c.dataset.d, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }) + '</small>';
    tip.hidden = false;
    const wr = wrap.getBoundingClientRect(), cr = c.getBoundingClientRect();
    let x = cr.left - wr.left + wrap.scrollLeft + cr.width / 2;
    x = Math.max(tip.offsetWidth / 2 + 4, Math.min(x, wrap.scrollWidth - tip.offsetWidth / 2 - 4));
    tip.style.left = x + 'px';
    tip.style.top = (cr.top - wr.top - tip.offsetHeight - 8) + 'px';
  });
  wrap.addEventListener('mouseleave', () => { const t = wrap.querySelector('.hm-tip'); if (t) t.hidden = true; });
})();

// ── Init & live refresh ──

loadToday();
loadSetupStatus();
loadRuns();
loadTodayActivity();
loadTrend();
loadHeatmap();
loadMercuryStatus();

// Agent status: quick poll
setInterval(loadMercuryStatus, 8000);

// Nav counts stay live wherever you are, so "something needs me" is visible
// from any tab without polling that tab's contents.
setInterval(async () => {
  if (document.hidden || currentTab === 'today') return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
}, 20000);

// Data tabs: auto-refresh live views without clobbering anything in progress.
// Outbox, settings and help are deliberately excluded — re-rendering the desk
// under someone mid-decision loses their place, and settings may be mid-edit.
setInterval(() => {
  if (document.hidden) return;
  switch (currentTab) {
    case 'today': loadToday(); loadRuns(); loadTodayActivity(); loadTrend(true); loadHeatmap(true); break;
    case 'warmup': if (!wuBusy()) loadWarmup(true); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'pipeline': if (!pipeBusy()) loadPipeline(); break;
    case 'calendar': if (!drawerOpen()) loadCalendar(true); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); break;
  }
}, 15000);
