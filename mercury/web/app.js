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
  sending:        ['Sending', 'active'],
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
  // imports
  imported:       ['Held (imported)', 'waiting'],
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
  campaigns: ['Campaigns', 'megaphone'], outbox: ['Outbox', 'tray'], mailboxes: ['Mailboxes', 'envelope-simple'],
  personas: ['Voice & Personas', 'sparkle'],
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
  document.title = meta[0] + ' · Mercury Agent';
  toggleSidebar(false);
  document.querySelectorAll('.hm-tip').forEach(t => { t.hidden = true; });
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
    case 'mailboxes': loadMailboxes(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'pipeline': loadPipeline(); break;
    case 'calendar': loadCalendar(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'personas': loadPersonas(); break;
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
  chartIntro(el, steps.map(st => st[1]).join(','));
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
  // Agents log the same step from several places (main + scout both write
  // "prospect" each cycle). Merge identical consecutive actions within an hour
  // so the feed reads as events, not log lines.
  const groups = [];
  for (const a of data) {
    const g = groups[groups.length - 1];
    const t = parseUTC(a.created_at);
    if (g && g.type === a.action_type && t && g.first && (g.first - t) < 3600e3) {
      g.n++; g.agents.add(a.agent); g.last = t; continue;
    }
    groups.push({ type: a.action_type, agents: new Set([a.agent]), n: 1, first: t, last: t, at: a.created_at });
    if (groups.length > 7) break;
  }
  el.innerHTML = '<div class="activity-feed">' + groups.slice(0, 7).map(g => {
    const [ico, label] = activityLabel(g.type);
    return '<div class="activity-item act-row">' +
      '<span class="act-ico">' + icon(ico) + '</span>' +
      '<span class="action">' + escHtml(label) +
        '<span class="agent">' + escHtml([...g.agents].filter(Boolean).join(', ')) + '</span>' +
        (g.n > 1 ? '<span class="times">&times;' + g.n + '</span>' : '') + '</span>' +
      '<span class="time" title="' + escHtml(g.first ? fullWhen(g.first) : '') + '">' +
        escHtml(g.first ? relWhen(g.first) : formatDate(g.at)) + '</span>' +
    '</div>';
  }).join('') + '</div>';
}

const ACTIVITY_LABELS = {
  prospect: ['magnifying-glass', 'Looked for new prospects'],
  reply_received: ['chat-circle-text', 'Reply received'],
  bounce: ['warning-circle', 'Email bounced'],
  email_sent: ['paper-plane-tilt', 'Email sent'],
  pipeline_move: ['kanban', 'Moved a contact in the pipeline'],
  outbox_reschedule: ['calendar-blank', 'Rescheduled an email'],
  write_campaign: ['pencil-simple', 'Drafted a campaign'],
  send_campaign: ['paper-plane-tilt', 'Sent a campaign'],
  analyze: ['chart-line-up', 'Updated analytics'],
  discover: ['compass', 'Discovered businesses'],
  profile: ['buildings', 'Read company websites'],
};

function activityLabel(type) {
  const hit = ACTIVITY_LABELS[type];
  if (hit) return hit;
  const t = String(type || 'activity').replace(/_/g, ' ');
  return ['pulse', t.charAt(0).toUpperCase() + t.slice(1)];
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
  savedPh('serper-key', data.serper_api_key_set, '2,500 free credits — serper.dev');
  savedPh('tavily-key', data.tavily_api_key_set, 'Fallback search — tavily.com');
  savedPh('semrush-key', data.semrush_api_key_set, 'Qualifies domains by real traffic — semrush.com');
  savedPh('treg-token', data.treg_token_set, 'One prepaid balance for verification & enrichment — treg.to');
  savedPh('dfs-pass', data.dataforseo_password_set, 'From dataforseo.com → API access');
  const dfsl = document.getElementById('dfs-login');
  if (dfsl) dfsl.value = data.dataforseo_login || '';
  api('/api/mailboxes').then(mb => {
    const el = document.getElementById('settings-mailboxes');
    if (el) el.innerHTML = mb && !mb.error && mb.rotation ? renderMailboxes(mb, true) : '';
  });
  await loadInboxSettings();
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
  tag('serper-status', data.serper_api_key_set, 'set');
  tag('tavily-status', data.tavily_api_key_set, 'set');
  tag('semrush-status', data.semrush_api_key_set, 'set');
  tag('treg-status', data.treg_token_set, 'set');
  tag('dfs-status', data.dataforseo_password_set, 'set');
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

// Inbox configuration is separate from the rotation's live capacity report.
let _inboxSettings = null, _inboxEditing = null;

async function loadInboxSettings() {
  const data = await api('/api/settings/mailboxes');
  const el = document.getElementById('inbox-list');
  const add = document.getElementById('inbox-add');
  if (!el || !add) return;
  if (!data || data.error) {
    _inboxSettings = null;
    add.disabled = true;
    el.innerHTML = '<p class="muted">Could not load inbox settings. Check the configuration and reload Settings.</p>';
    return;
  }
  _inboxSettings = data;
  add.disabled = false;
  el.innerHTML = (data.inboxes || []).map((b, i) =>
    '<div class="inbox-setting-row"><div class="inbox-setting-info"><b>' + escHtml(b.email) + '</b>' +
      '<div class="inbox-setting-meta">' + toneBadge(b.password_set ? 'good' : 'waiting', b.password_set ? 'Password saved' : 'Password needed') +
      (b.password_set && !b.configured ? toneBadge('waiting', 'Server needed') : '') +
      '<span>' + fmtN(b.daily_cap) + '/day · ' + (b.enabled ? 'New outreach enabled' : 'Existing threads only') + '</span></div></div>' +
    '<div class="inbox-setting-actions"><button type="button" class="btn btn-secondary btn-sm" onclick="openInboxEditor(' + i + ')">Edit inbox</button>' +
    '<button type="button" class="btn btn-secondary btn-sm" onclick="testInbox(' + i + ', this)">Test connection</button></div></div>'
  ).join('') || '<p class="muted">No SMTP inboxes added yet. Add your first inbox to get started.</p>';
  el.innerHTML += '<p class="inbox-limit-note muted">All inboxes share the overall limit of ' + fmtN(data.max_daily_sends) +
    ' emails/day. Each new inbox starts with its warm-up ramp.</p>';
}

function openInboxEditor(index) {
  if (!_inboxSettings) return;
  const b = Number.isInteger(index) ? _inboxSettings.inboxes[index] : null;
  _inboxEditing = b ? b.email : null;
  const defaults = _inboxSettings.defaults || {};
  const input = (id, label, type, value, extra = '') => '<div class="form-group"><label class="form-label" for="inbox-' + id + '">' + label +
    '</label><input class="form-input" id="inbox-' + id + '" type="' + type + '" value="' + escHtml(value) + '" ' + extra + '></div>';
  const host = (id, label, placeholder) => input(id, label, 'text', b && b[id.replace('-', '_')] || '',
    'placeholder="' + escHtml(placeholder || 'Enter server hostname') + '" autocomplete="off"');
  const editor = document.getElementById('inbox-editor');
  editor.hidden = false;
  editor.innerHTML = '<form id="inbox-form" onsubmit="saveInbox(event)">' +
    '<h4>' + (b ? 'Edit inbox' : 'Add inbox') + '</h4>' +
    '<div class="form-row">' + input('email', 'Email address', 'email', b ? b.email : '', 'required autocomplete="off"' + (b ? ' readonly' : '')) +
    input('name', 'Sender name', 'text', b ? b.name : '', 'autocomplete="off" placeholder="Uses your default sender name"') + '</div>' +
    input('password', b && b.password_set ? 'Change password' : 'Password / app password', 'password', '',
      'autocomplete="new-password" placeholder="' + (b && b.password_set ? 'Leave blank to keep the saved password' : 'Can be added later') + '"') +
    '<div class="form-row">' + input('cap', 'Daily sending limit', 'number', b ? b.daily_cap : 30, 'required min="0" step="1"') +
    input('start', 'Warm-up start date', 'date', b ? b.warmup_start || '' : _inboxSettings.today) + '</div>' +
    '<p class="muted inbox-field-hint">Clear the date only if this inbox is already warmed up.</p>' +
    '<details class="inbox-servers"' + (!defaults.smtp_host || b && !b.configured ? ' open' : '') + '><summary>Server settings</summary>' +
    '<p class="muted">Blank fields use the shared SMTP and IMAP settings.</p>' +
    '<div class="form-row">' + host('smtp-host', 'SMTP host', defaults.smtp_host) +
    input('smtp-port', 'SMTP port', 'number', b && b.smtp_port || '', 'min="1" max="65535" placeholder="' + (defaults.smtp_port || 587) + '"') + '</div>' +
    '<div class="form-row">' + host('imap-host', 'IMAP host', defaults.imap_host) +
    input('imap-port', 'IMAP port', 'number', b && b.imap_port || '', 'min="1" max="65535" placeholder="' + (defaults.imap_port || 993) + '"') + '</div>' +
    input('username', 'SMTP login', 'text', b ? b.username : '', 'autocomplete="off" placeholder="Defaults to the inbox email"') +
    input('imap-username', 'IMAP login', 'text', b ? b.imap_username : '', 'autocomplete="off" placeholder="Defaults to the SMTP login"') +
    input('imap-password', b && b.imap_password_set ? 'Change IMAP password' : 'IMAP password', 'password', '',
      'autocomplete="new-password" placeholder="' + (b && b.imap_password_set ? 'Leave blank to keep the saved password' : 'Leave blank to use the SMTP password') + '"') + '</details>' +
    '<label class="inbox-checkbox"><input type="checkbox" id="inbox-enabled"' + (!b || b.enabled ? ' checked' : '') + '>Use this inbox for new outreach</label>' +
    '<p class="muted inbox-field-hint">Turning this off keeps replies and existing follow-ups on this inbox.</p>' +
    (_inboxSettings.provider !== 'smtp' ? '<label class="inbox-checkbox"><input type="checkbox" id="inbox-activate" required>Switch email sending from ' +
      escHtml(_inboxSettings.provider) + ' to SMTP + IMAP</label>' : '') +
    '<p id="inbox-form-error" class="inbox-form-error" role="alert"></p>' +
    '<div class="inbox-setting-actions"><button type="submit" id="inbox-save" class="btn btn-primary btn-sm">' +
    (b ? 'Save inbox' : 'Add inbox') + '</button><button type="button" class="btn btn-secondary btn-sm" onclick="closeInboxEditor()">Cancel</button></div></form>';
  document.getElementById(b ? 'inbox-password' : 'inbox-email').focus({preventScroll: true});
  editor.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function closeInboxEditor() {
  const editor = document.getElementById('inbox-editor');
  editor.replaceChildren();
  editor.hidden = true;
  _inboxEditing = null;
  document.getElementById('inbox-add').focus({preventScroll: true});
}

async function saveInbox(event) {
  event.preventDefault();
  const button = document.getElementById('inbox-save');
  if (button.disabled) return;
  const val = id => document.getElementById('inbox-' + id).value;
  const data = {email: val('email').trim(), name: val('name').trim(),
    password: val('password'), daily_cap: Number(val('cap')), warmup_start: val('start') || null,
    smtp_host: val('smtp-host').trim(), smtp_port: Number(val('smtp-port')) || 0,
    imap_host: val('imap-host').trim(), imap_port: Number(val('imap-port')) || 0,
    username: val('username').trim(), imap_username: val('imap-username').trim(),
    imap_password: val('imap-password'),
    enabled: document.getElementById('inbox-enabled').checked};
  const activate = document.getElementById('inbox-activate');
  if (activate) data.activate_smtp = activate.checked;
  button.disabled = true;
  const result = await getJSON('/api/settings/mailboxes' + (_inboxEditing ? '/' + encodeURIComponent(_inboxEditing) : ''), {
    method: _inboxEditing ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  });
  button.disabled = false;
  if (!result.ok || !result.data.success) {
    document.getElementById('inbox-form-error').textContent = result.data && result.data.message || 'Could not save. Try again.';
    return;
  }
  closeInboxEditor();
  document.getElementById('inbox-notice').textContent = result.data.restart_required
    ? 'Inbox saved. Stop and start Mercury in Controls to apply the changes to the running agent.'
    : 'Inbox saved. Changes apply on your next Mercury run; restart it if you run it outside this dashboard.';
  await loadSettings();
  _mb.key = '';
  showToast('Inbox saved.', 'success');
}

async function testInbox(index, button) {
  const b = _inboxSettings && _inboxSettings.inboxes[index];
  if (!b) return;
  const label = button.textContent;
  button.disabled = true; button.textContent = 'Testing…';
  const res = await getJSON('/api/settings/mailboxes/' + encodeURIComponent(b.email) + '/test', {method: 'POST'});
  button.disabled = false; button.textContent = label;
  document.getElementById('inbox-notice').textContent = res.data && res.data.message || 'Could not test the connection. Try again.';
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

function saveSearchKeys() {
  const payload = {};
  const val = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  if (val('serper-key')) payload.SERPER_API_KEY = val('serper-key');
  if (val('tavily-key')) payload.TAVILY_API_KEY = val('tavily-key');
  if (val('semrush-key')) payload.SEMRUSH_API_KEY = val('semrush-key');
  if (val('treg-token')) payload.TREG_TOKEN = val('treg-token');
  if (val('dfs-login')) payload.DATAFORSEO_LOGIN = val('dfs-login');
  const dp = document.getElementById('dfs-pass');
  if (dp && dp.value) payload.DATAFORSEO_PASSWORD = dp.value;
  if (!Object.keys(payload).length) { showToast('Enter at least one key first.', 'error'); return; }
  saveEnv(payload, 'Search keys saved.').then(loadSettings);
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

// Sending capacity, shared by the Outbox panel, the desk and Settings.
let _mailboxes = null;

async function loadOutbox() {
  const [data, mbox] = await Promise.all([api('/api/outbox'), api('/api/mailboxes')]);
  _mailboxes = mbox && !mbox.error ? mbox : null;
  const mboxEl = document.getElementById('outbox-mailboxes');
  if (mboxEl) mboxEl.innerHTML = mbox && mbox.error
    ? '<section class="panel"><div class="panel-head"><div><h3 class="icon-title">' + icon('warning-circle') +
        'Mailbox settings unreadable</h3><p>' + escHtml(mbox.error) + '</p></div></div></section>'
    : (_mailboxes && _mailboxes.rotation ? renderMailboxes(_mailboxes) : '');
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
    table('Sending', data.sending, [
      ['To', r => r.to_email], ['Subject', r => r.subject],
      ['From', r => r.from_mailbox || r.mailbox || '—', true],
      ['Persona', r => personaChip(r), false, true],
      ['Started', r => formatDate(r.updated_at), true],
    ]) +
    table('Approved &amp; scheduled', data.approved, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['From', r => fromCell(r), true, true],
      ['Persona', r => personaChip(r), false, true],
      ['Sends', r => formatDate(r.send_at), true],
    ]) +
    table('Recently sent', data.sent, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['From', r => r.from_mailbox || r.mailbox || '—', true],
      ['Persona', r => personaChip(r), false, true],
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
        formatDate(cur.send_at) +
        (_mailboxes && _mailboxes.rotation ? ' &middot; from <b>' + escHtml(fromLabel(cur)) + '</b>' : '') +
        '</div>' +
      followupNote(cur) +
      '<div class="desk-persona">' + personaChip(cur) +
        (cur.manually_edited ? '<span class="muted">Edited before sending</span>' : '') + '</div>' +
      '<label class="sr-only" for="desk-subject">Subject</label>' +
      '<input class="form-input desk-subject" id="desk-subject" value="' + escAttr(cur.subject || '') +
        '" placeholder="Subject" autocomplete="off">' +
      '<label class="sr-only" for="desk-body">Body</label>' +
      '<textarea class="form-input desk-body" id="desk-body" rows="12">' + escHtml(cur.body) + '</textarea>' +
      '<div class="desk-regen">' +
        '<label class="sr-only" for="desk-instruction">Rewrite instruction</label>' +
        '<input class="form-input" id="desk-instruction" autocomplete="off" ' +
          'placeholder="Optional instruction for a rewrite: shorter, warmer, mention their reviews…">' +
        '<button class="btn btn-secondary btn-sm" id="desk-regen-btn" onclick="outboxRegenerate(\'' + cur.id + '\')">' +
          icon('sparkle') + 'Regenerate</button>' +
      '</div>' +
      '<div class="desk-actions">' +
        '<button class="btn btn-primary" onclick="outboxAct(\'' + cur.id + '\',\'approve\')">' +
          'Approve <kbd>A</kbd></button>' +
        '<button class="btn btn-secondary" onclick="outboxAct(\'' + cur.id + '\',\'reject\')">' +
          'Reject <kbd>R</kbd></button>' +
        '<button class="btn btn-secondary" onclick="outboxSave(\'' + cur.id + '\')">Save edits</button>' +
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

function escAttr(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// What the reviewer changed on the desk, or null when it matches the draft.
function deskEdits(id) {
  const it = _desk.items.find(x => x.id === id);
  const s = document.getElementById('desk-subject');
  const b = document.getElementById('desk-body');
  if (!it || !s || !b) return null;
  const subject = s.value.trim(), body = b.value.trim();
  if (subject === (it.subject || '') && body === (it.body || '')) return null;
  return { it, subject, body };
}

async function outboxSave(id, quiet) {
  const e = deskEdits(id);
  if (!e) { if (!quiet) showToast('Nothing changed.', 'success'); return true; }
  if (!e.subject || !e.body) { showToast('Subject and body can\'t be empty.', 'error'); return false; }
  const data = await api('/api/outbox/' + encodeURIComponent(id), {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subject: e.subject, body: e.body}),
  });
  if (data && data.success) {
    e.it.subject = e.subject; e.it.body = e.body;
    e.it.manually_edited = 1;
    if (!quiet) showToast('Edits saved.', 'success');
    return true;
  }
  showToast('Couldn\'t save the edits' + (data && data.message ? ': ' + data.message : '.'), 'error');
  return false;
}

async function outboxRegenerate(id) {
  const instruction = (document.getElementById('desk-instruction') || {}).value || '';
  const btn = document.getElementById('desk-regen-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = icon('sparkle') + 'Writing…'; }
  showToast('Rewriting — this can take up to a minute.', 'success');
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/regenerate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({instruction: instruction.trim()}),
  });
  if (data && data.success) {
    const it = _desk.items.find(x => x.id === id);
    if (it) Object.assign(it, data);
    document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
    showToast('New draft ready. Review it before approving.', 'success');
  } else {
    if (btn) { btn.disabled = false; btn.innerHTML = icon('sparkle') + 'Regenerate'; }
    showToast('Rewrite failed' + (data && data.message ? ': ' + data.message : '.'), 'error');
  }
}

async function outboxAct(id, action) {
  // Approving sends what is on screen, so unsaved edits go first.
  if (action === 'approve' && deskEdits(id) && !(await outboxSave(id, true))) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  if (data && data.success) {
    if (action === 'approve') {
      const fu = Number(data.followups_approved || 0);
      showToast(fu ? 'Approved, with ' + fu + ' follow-up' + (fu === 1 ? '' : 's') + ' — they send on schedule.'
                   : 'Approved — will send on schedule.', 'success');
    } else {
      const later = Math.max(0, Number(data.rejected || 1) - 1);
      showToast(later ? 'Rejected, with ' + later + ' later step' + (later === 1 ? '' : 's') + ' of the sequence.'
                      : 'Rejected.', 'success');
    }
  } else showToast('Action failed.', 'error');
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

// ── Sending mailboxes ──
//
// The same numbers the sender enforces (/api/mailboxes): caps after the
// warm-up ramp and health gates, sends in the rolling 24 hours.

function fromLabel(r) {
  if (r.from_mailbox && r.from_removed) return r.from_mailbox + ' (removed — held until you add it back)';
  if (r.from_mailbox) return r.from_mailbox;
  if (r.mailbox) return r.mailbox;
  if (_mailboxes && _mailboxes.rotation) return 'next free mailbox';
  const first = _mailboxes && (_mailboxes.mailboxes || [])[0];
  return (first && first.email) || '—';
}

function fromCell(r) {
  const label = r.from_removed ? r.from_mailbox : fromLabel(r);
  return (r.from_removed ? toneBadge('waiting', 'held') + ' ' : '') +
    '<span class="' + (r.from_mailbox || r.mailbox ? 'mono-sm' : 'muted') + '"' +
      (r.from_removed ? ' title="Its mailbox was removed from the config; held until you add it back"' : '') + '>' +
      escHtml(label) + '</span>';
}

function autoFollowups(it) {
  return !!(_mailboxes && _mailboxes.auto_approve_followups &&
            it.kind === 'sequence' && Number(it.step) === 1);
}

function followupNote(it) {
  return autoFollowups(it)
    ? '<div class="desk-note">' + icon('info') + 'Approving this also approves its follow-ups.</div>' : '';
}

function mailboxStage(m) {
  if (m.stage === 'removed') return toneBadge('idle', 'Removed') +
    ' <span class="muted mb-sub">still counts toward max_daily_sends</span>';
  if (m.configured === false) return toneBadge('bad', 'No password');
  let h;
  if (m.stage === 'paused') h = toneBadge('waiting', 'Paused');
  else if (m.stage === 'scheduled') h = toneBadge('idle', 'Starts ' + shortDay(m.warmup_start));
  else if (m.stage === 'warming') h = toneBadge('active', 'Warming') +
    (m.full_on ? ' <span class="muted mb-sub">' + escHtml(fmtN(m.daily_cap) + '/day from ' + shortDay(m.full_on)) + '</span>' : '');
  else if (m.stage === 'fixed') h = toneBadge('note', 'Fixed cap') +
    ' <span class="muted mb-sub">no weekly ramp</span>';
  else h = toneBadge('good', 'Warm');
  if (m.gate === 'hold') h += ' ' + toneBadge('waiting', 'On hold');
  if (m.accepts_new === false) h += ' <span class="muted mb-sub" title="Finishes its threads and is still read; starts no new ones">no new threads</span>';
  return h;
}

function renderMailboxes(mb, compact) {
  if (!mb || !mb.mailboxes || !mb.mailboxes.length) return '';
  const flags = [];
  if (mb.require_approval) flags.push(mb.auto_approve_followups
    ? 'you approve first emails; follow-ups ride along' : 'every email needs your approval');
  else flags.push('autopilot: no approval step');
  if (mb.spread_sends) flags.push('paced through the day');
  const capNote = mb.capped_by_global ? ' (capped by max_daily_sends ' + fmtN(mb.max_daily_sends) + ')' : '';
  const rows = mb.mailboxes.map(m => {
    const pct = m.cap_today ? Math.min(100, Math.round(100 * m.sent_24h / m.cap_today)) : 0;
    return '<tr><td><span class="mono-sm">' + escHtml(m.email || (m.stage === 'removed' ? 'other' : '(default mailbox)')) + '</span>' +
        (m.name && !compact ? '<div class="muted mb-sub">' + escHtml(m.name) + '</div>' : '') + '</td>' +
      '<td class="mb-meter"><div class="progress-label"><span class="pct">' + fmtN(m.sent_24h) + ' / ' + fmtN(m.cap_today) +
        '</span><span class="text">' + fmtN(m.remaining) + ' left</span></div>' +
        '<div class="progress-bar"><div class="progress-fill green" style="width:' + pct + '%"></div></div></td>' +
      '<td>' + mailboxStage(m) + '</td></tr>';
  }).join('');
  const table = '<div class="table-card mb-table"><table><thead><tr><th>Mailbox</th><th>Last 24 h</th><th>Stage</th></tr></thead><tbody>' +
    rows + '</tbody></table></div>';
  const summary = fmtN(mb.sent_24h) + ' of ' + fmtN(mb.capacity_today) + ' sent in the last 24 hours' + escHtml(capNote);
  if (compact) {
    return '<div class="mb-compact"><div class="mb-compact-head"><b>Rotation</b><span class="muted">' + summary + '</span></div>' +
      table + '<p class="lede mb-hint">Manage addresses, passwords, and sending limits under Sending inboxes below. ' +
      '<button class="link-btn" onclick="goTab(\'mailboxes\')">Mailboxes' + icon('arrow-right') + '</button></p></div>';
  }
  return '<section class="panel"><div class="panel-head"><div><h3>Sending mailboxes</h3><p>' + summary +
      ' &middot; ' + flags.map(escHtml).join(' &middot; ') + '</p></div>' +
      '<button class="link-btn" onclick="goTab(\'mailboxes\')">Mailboxes' + icon('arrow-right') + '</button></div>' +
    '<div class="panel-body">' + table + '</div></section>';
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
    // Imported contacts say where they came from and what they still lack.
    const needs = p.import_batch_id
      ? [['first name', p.first_name], ['last name', p.last_name], ['title', p.title]].filter(x => !x[1]).map(x => x[0]) : [];
    const source = p.import_batch_id
      ? '<span title="Import batch ' + escHtml(p.import_batch_id) + '">Import ' + escHtml(p.import_batch_id.slice(0, 6)) +
        ' · row ' + escHtml(String(p.import_row)) + '</span>'
      : escHtml(p.source);
    html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) +
      (needs.length ? '<div class="muted imp-needs">needs ' + escHtml(needs.join(', ')) + '</div>' : '') + '</td>' +
      '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.company) + '</td>' +
      '<td>' + emailV + '</td><td>' + phoneV + '</td><td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + source + '</td><td class="muted">' + formatDate(p.created_at) + '</td>' +
      '<td><button class="btn btn-secondary btn-sm" onclick="fbProspect(' + i + ')">Feedback</button></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

// ── Contacts: CSV import ──
//
// upload -> map columns -> preview -> commit -> verify / release. The browser
// keeps the file and re-sends it with each preview and the commit, so the
// server never stores an upload. Every count and row outcome comes from the
// same service the CLI (`mercury import`) uses.

const IMP_MAX_BYTES = 5 * 1024 * 1024;
const IMP_OUTCOME = {
  new: ['New', 'good'], incomplete: ['Needs enrichment', 'waiting'],
  duplicate: ['Duplicate', 'idle'], invalid: ['Invalid', 'bad'],
};
const IMP_ACTION = {
  create: 'Import', fill: 'Fill blanks', skip: 'Skip', exclude: 'Leave out',
  created: 'Imported', filled: 'Filled', skipped: 'Skipped', excluded: 'Left out',
};
const _imp = {
  open: false, name: '', b64: '', mapping: null, delimiter: '', policy: 'skip',
  preview: null, error: null, exclude: new Set(), filter: 'problems',
  skipInvalid: false, result: null, busy: false, batches: [], providers: [], verifying: '',
};

function toggleImport(force) {
  _imp.open = force === undefined ? !_imp.open : force;
  const panel = document.getElementById('import-panel');
  panel.hidden = !_imp.open;
  document.getElementById('imp-toggle').setAttribute('aria-expanded', String(_imp.open));
  if (_imp.open) { renderImport(); loadImportBatches(); }
}

function impReset() {
  Object.assign(_imp, { name: '', b64: '', mapping: null, delimiter: '', policy: 'skip', preview: null,
    error: null, exclude: new Set(), filter: 'problems', skipInvalid: false, result: null });
  renderImport();
}

async function impSend(path, body) {
  try {
    const r = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch { /* not JSON */ }
    if (r.ok) return { ok: true, data };
    const d = data && data.detail;
    const fallback = 'Request failed (' + r.status + ').';
    let error;
    if (Array.isArray(d)) {
      // Pydantic validation: one entry per bad field, each with loc and msg.
      const parts = d.map(x => {
        if (!x || !x.msg) return '';
        const field = Array.isArray(x.loc) ? x.loc.filter(l => l !== 'body').join('.') : '';
        return (field ? field + ': ' : '') + x.msg;
      }).filter(Boolean);
      error = { code: 'validation', message: parts.length ? parts.join('. ') + '.' : fallback };
    } else if (d && typeof d === 'object') {
      error = d;
    } else {
      error = { code: 'http', message: typeof d === 'string' ? d : fallback };
    }
    return { ok: false, error };
  } catch {
    return { ok: false, error: { code: 'offline', message: 'Can\'t reach the dashboard server.' } };
  }
}

function impBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

async function impPickFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > IMP_MAX_BYTES) {
    _imp.error = { code: 'too_large', message: 'That file is ' + (file.size / 1048576).toFixed(1) +
      ' MB. The limit is 5 MB; split it and import the parts.' };
    renderImport();
    return;
  }
  impReset();
  _imp.name = file.name;
  _imp.b64 = impBase64(await file.arrayBuffer());
  await impPreview();
}

function impBody() {
  return { filename: _imp.name, content_b64: _imp.b64, mapping: _imp.mapping,
    delimiter: _imp.delimiter, policy: _imp.policy, exclude_rows: [..._imp.exclude] };
}

async function impPreview() {
  _imp.busy = true; renderImport();
  const res = await impSend('/api/imports/preview', impBody());
  _imp.busy = false;
  if (res.ok) {
    _imp.preview = res.data; _imp.error = null; _imp.mapping = res.data.mapping;
    if (!_imp.delimiter) _imp.delimiter = res.data.delimiter;
  } else {
    // Keep the mapping controls when the server named the columns, but drop
    // counts and rows: they described a mapping that no longer applies.
    _imp.error = res.error;
    _imp.preview = res.error.headers
      ? { headers: res.error.headers, fields: IMP_FIELDS_FALLBACK, mapping: _imp.mapping || {} } : null;
  }
  renderImport();
}

const IMP_FIELDS_FALLBACK = [
  ['email', 'Email'], ['first_name', 'First name'], ['last_name', 'Last name'], ['full_name', 'Full name'],
  ['title', 'Title'], ['company_name', 'Company'], ['website', 'Website'], ['industry', 'Industry'],
  ['linkedin_url', 'LinkedIn URL'], ['phone', 'Phone'], ['personalization', 'Personalization notes'],
].map(([key, label]) => ({ key, label }));

function impMap(field, header) {
  const mapping = Object.assign({}, _imp.mapping || {});
  if (header) mapping[field] = header; else delete mapping[field];
  _imp.mapping = mapping;
  impPreview();
}

function impSetDelimiter(value) { _imp.delimiter = value; _imp.mapping = null; impPreview(); }
function impSetPolicy(value) { _imp.policy = value; impPreview(); }
function impSetFilter(value) { _imp.filter = value; renderImport(); }

function impToggleRow(row, on) {
  if (on) _imp.exclude.delete(row); else _imp.exclude.add(row);
  renderImport();
}

function impRowsShown(rows) {
  const f = _imp.filter;
  if (f === 'all') return rows;
  if (f === 'problems') return rows.filter(r => r.outcome !== 'new' || r.warnings.length);
  return rows.filter(r => r.outcome === f);
}

function impPlannedAction(r) {
  if ((r.action === 'create' || r.action === 'fill') && _imp.exclude.has(r.row)) return 'exclude';
  return r.action;
}

async function impCommit() {
  const p = _imp.preview;
  const going = p.rows.filter(r => ['create', 'fill'].includes(impPlannedAction(r))).length;
  const ok = await confirmModal({ title: 'Import ' + going + ' contact' + (going === 1 ? '' : 's') + '?',
    copy: 'They are added as held, unverified contacts. Nothing is verified, drafted or sent until you ask.',
    ok: 'Import' });
  if (!ok) return;
  _imp.busy = true; renderImport();
  const res = await impSend('/api/imports/commit', Object.assign(impBody(), { skip_invalid: _imp.skipInvalid }));
  _imp.busy = false;
  if (!res.ok) { _imp.error = res.error; renderImport(); return; }
  _imp.result = res.data; _imp.error = null;
  showToast(res.data.already_committed ? 'Already imported. Nothing changed.' : 'Import complete.', 'success');
  renderImport();
  loadImportBatches();
  loadProspects();
}

async function loadImportBatches() {
  const res = await impSend('/api/imports');
  if (res.ok) { _imp.batches = res.data.batches || []; _imp.providers = res.data.providers || []; }
  if (_imp.open) renderImport();
}

async function impVerify(batchId) {
  const est = await impSend('/api/imports/' + encodeURIComponent(batchId) + '/verify');
  if (!est.ok) { showToast(est.error.message, 'error'); return; }
  const e = est.data;
  if (!e.providers.length) { showToast(e.cost, 'error'); return; }
  if (!e.addresses) { showToast('Every address in this batch is already verified or settled.', 'success'); return; }
  const ok = await confirmModal({ title: 'Verify ' + e.addresses + ' address' + (e.addresses === 1 ? '' : 'es') + '?',
    copy: e.cost + ' Providers: ' + e.providers.map(p => p.name + ' (' + p.free_tier + ')').join(', ') + '.',
    ok: 'Verify' });
  if (!ok) return;
  let after = 0, done = 0, unresolved = 0;
  const totals = {};
  _imp.verifying = batchId;
  while (true) {
    _imp.verifyNote = 'Verifying ' + done + ' / ' + e.addresses + '…';
    renderImport();
    const res = await impSend('/api/imports/' + encodeURIComponent(batchId) + '/verify', { limit: 10, after_row: after });
    if (!res.ok) { showToast(res.error.message, 'error'); break; }
    const s = res.data;
    done += s.checked; after = s.next_after_row; unresolved += s.unresolved || 0;
    for (const [k, v] of Object.entries(s.results)) totals[k] = (totals[k] || 0) + v;
    if (s.stopped) { showToast(s.stopped, 'error'); break; }
    if (!s.remaining || !s.checked) break;
  }
  _imp.verifying = ''; _imp.verifyNote = '';
  const summary = Object.entries(totals).map(([k, v]) => v + ' ' + statusMeta(k).label.toLowerCase()).join(', ');
  showToast((summary ? 'Verified: ' + summary + '.' : 'Nothing was checked.') +
    (unresolved ? ' ' + unresolved + ' could not be settled and stay unverified; verifying again spends credits on them again.' : ''),
    'success');
  loadImportBatches();
  loadProspects();
}

async function impRelease(batchId) {
  const ok = await confirmModal({ title: 'Release verified contacts to outreach?',
    copy: 'Verified contacts from this import join the pipeline as new. The Writer drafts for them on its next cycle, and drafts wait in the Outbox for your approval unless approval is off. Unverified contacts stay held.',
    ok: 'Release' });
  if (!ok) return;
  const res = await impSend('/api/imports/' + encodeURIComponent(batchId) + '/release', { include_risky: false });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  showToast('Released ' + res.data.released + '. ' + res.data.still_held + ' still held.', 'success');
  loadImportBatches();
  loadProspects();
}

function impCounts(c) {
  const tile = (k, n, label) => '<div class="sig-stat imp-' + k + '"><div class="n">' + n + '</div><div class="k">' + label + '</div></div>';
  return '<div class="sig-summary imp-summary">' +
    tile('new', c.new, 'new') + tile('incomplete', c.incomplete, 'needs enrichment') +
    tile('duplicate', c.duplicate, 'duplicate' + (c.suppressed ? ' (' + c.suppressed + ' opted out or invalid)' : '')) +
    tile('invalid', c.invalid, 'invalid') + '</div>';
}

function impRowsTable(rows, committed) {
  if (!rows.length) return '<p class="loading-note">No rows in this view.</p>';
  const cap = 500;
  let html = '<div class="table-card imp-rows"><table><thead><tr>' + (committed ? '' : '<th></th>') +
    '<th class="num">Row</th><th>Email</th><th>Name</th><th>Company</th><th>Outcome</th><th>Action</th><th>Detail</th></tr></thead><tbody>';
  rows.slice(0, cap).forEach(r => {
    const action = committed ? r.action : impPlannedAction(r);
    const o = IMP_OUTCOME[r.outcome] || [r.outcome, 'idle'];
    const can = !committed && (r.action === 'create' || r.action === 'fill');
    const detail = [r.reason, (r.fill || []).length ? 'fills ' + r.fill.join(', ') : '', ...(r.warnings || [])]
      .filter(Boolean).map(escHtml).join(' · ');
    html += '<tr>' + (committed ? '' : '<td>' + (can
      ? '<input type="checkbox" aria-label="Import row ' + r.row + '"' + (_imp.exclude.has(r.row) ? '' : ' checked') +
        ' onchange="impToggleRow(' + r.row + ', this.checked)">' : '') + '</td>') +
      '<td class="num">' + r.row + '</td><td>' + escHtml(r.email || '') + '</td><td>' + escHtml(r.name || '') +
      '</td><td>' + escHtml(r.company || '') + '</td><td>' + toneBadge(o[1], o[0]) + '</td><td>' +
      escHtml(IMP_ACTION[action] || action) + '</td><td class="muted">' + detail + '</td></tr>';
  });
  html += '</tbody></table></div>';
  if (rows.length > cap) html += '<p class="drawer-note">Showing ' + cap + ' of ' + rows.length + ' rows.</p>';
  return html;
}

function impMappingGrid(p) {
  const fields = p.fields || IMP_FIELDS_FALLBACK;
  const mapping = _imp.mapping || p.mapping || {};
  return '<div class="imp-map">' + fields.map(f => {
    const opts = '<option value="">Not imported</option>' + p.headers.map(h =>
      '<option value="' + escHtml(h) + '"' + (mapping[f.key] === h ? ' selected' : '') + '>' + escHtml(h) + '</option>').join('');
    return '<label class="form-group"><span class="form-label">' + escHtml(f.label) + (f.key === 'email' ? ' (required)' : '') +
      '</span><select class="form-input" onchange="impMap(\'' + f.key + '\', this.value)"' + (_imp.busy ? ' disabled' : '') + '>' +
      opts + '</select></label>';
  }).join('') + '</div>';
}

function impBatchesHtml() {
  if (!_imp.batches.length) return '';
  return '<div class="imp-step"><h3>Recent imports</h3><div class="table-card"><table><thead><tr>' +
    '<th>File</th><th>Imported</th><th class="num">Created</th><th class="num">Held</th><th class="num">Unverified</th><th></th></tr></thead><tbody>' +
    _imp.batches.map(b => {
      const id = escHtml(b.id);
      const busy = _imp.verifying === b.id;
      return '<tr><td>' + escHtml(b.filename || 'import') + ' <span class="muted">' + id + ' · ' + escHtml(b.origin || '') + '</span></td>' +
        '<td class="muted">' + formatDate(b.created_at) + '</td><td class="num">' + b.created + '</td><td class="num">' + b.held +
        '</td><td class="num">' + b.unverified + '</td><td class="imp-actions">' +
        (busy ? '<span class="muted">' + escHtml(_imp.verifyNote || 'Verifying…') + '</span>'
          : '<button class="btn btn-secondary btn-sm" onclick="impVerify(\'' + id + '\')"' + (b.unverified ? '' : ' disabled') + '>Verify</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="impRelease(\'' + id + '\')"' + (b.held ? '' : ' disabled') + '>Release</button>') +
        '</td></tr>';
    }).join('') + '</tbody></table></div>' +
    '<p class="drawer-note">Verify spends one verifier credit per address' +
    (_imp.providers.length ? ' (' + _imp.providers.map(p => escHtml(p.name)).join(', ') + ')' : '; no verifier is configured in .env') +
    '. Release hands verified contacts to the Writer; unverified ones stay held.</p></div>';
}

function renderImport() {
  const el = document.getElementById('import-panel');
  if (!el || !_imp.open) return;
  const p = _imp.preview, err = _imp.error, r = _imp.result;
  let html = '<div class="card imp-card"><div class="imp-head"><div><h2>Import contacts from CSV</h2>' +
    '<p class="lede">UTF-8 CSV, up to 5 MB and 5,000 rows. Every contact needs an email. Imported addresses start unverified ' +
    '(any status column in the file is ignored), and nothing is drafted or sent until you verify and release them.</p></div>' +
    '<button class="btn btn-secondary btn-sm" onclick="toggleImport(false)">Close</button></div>';

  if (r) {
    const b = r.batch;
    html += '<div class="imp-step"><h3>' + (r.already_committed ? 'Already imported' : 'Imported') + ' · ' + escHtml(b.filename || '') + '</h3>' +
      '<p class="toolbar-note"><b>' + b.created + '</b> created · <b>' + b.filled + '</b> filled · <b>' + b.skipped +
      '</b> skipped · <b>' + b.excluded + '</b> left out · batch <b>' + escHtml(b.id) + '</b></p>' +
      impRowsTable((r.rows || []).filter(x => x.action !== 'created' || (x.missing || []).length), true) +
      '<div class="drawer-actions"><button class="btn btn-primary btn-sm" onclick="impVerify(\'' + escHtml(b.id) + '\')">Verify addresses</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="impReset()">Import another file</button></div></div>';
  } else {
    html += '<div class="imp-step"><label class="btn btn-secondary btn-sm imp-file">' + icon('upload-simple') +
      (_imp.name ? 'Choose a different file' : 'Choose a CSV file') +
      '<input type="file" accept=".csv,.tsv,.txt,text/csv" onchange="impPickFile(this)" hidden></label>' +
      (_imp.name ? ' <span class="toolbar-note">' + escHtml(_imp.name) + (_imp.busy ? ' · reading…' : '') + '</span>' : '') + '</div>';

    if (err) {
      html += '<div class="imp-error" role="alert">' + icon('warning-circle') + '<span>' + escHtml(err.message) +
        (err.rows ? ' Rows: ' + err.rows.map(escHtml).join(', ') + '.' : '') + '</span></div>';
    }
    if (_imp.name && !_imp.busy || p) {
      const delims = ['comma', 'semicolon', 'tab'];
      html += '<div class="imp-step"><h3>1 · Columns</h3><div class="toolbar">' +
        '<span class="toolbar-note">Delimiter</span><div class="segmented" role="radiogroup" aria-label="Delimiter">' +
        delims.map(d => '<button type="button" role="radio" aria-checked="' + (_imp.delimiter === d) + '" class="' + (_imp.delimiter === d ? 'on' : '') +
          '" onclick="impSetDelimiter(\'' + d + '\')">' + d + '</button>').join('') + '</div>' +
        (err && err.candidates ? '<span class="toolbar-note">Choose ' + err.candidates.map(escHtml).join(' or ') + '</span>' : '') +
        '</div>' + (p && p.headers ? impMappingGrid(p) : '') +
        (p && (p.ignored_columns || []).length ? '<p class="drawer-note">Ignored ' + p.ignored_columns.map(escHtml).join(', ') +
          ': a CSV can\'t vouch for an address. Mercury verifies it when you ask.</p>' : '') + '</div>';
    }
    if (p && p.counts) {
      const c = p.counts;
      const invalidLeft = p.rows.filter(x => x.outcome === 'invalid').length;
      const going = p.rows.filter(x => ['create', 'fill'].includes(impPlannedAction(x))).length;
      const filters = [['problems', 'Needs a look'], ['all', 'All'], ['new', 'New'], ['incomplete', 'Needs enrichment'],
        ['duplicate', 'Duplicates'], ['invalid', 'Invalid']];
      html += '<div class="imp-step"><h3>2 · Preview</h3>' + impCounts(c) +
        '<div class="toolbar"><span class="toolbar-note">Existing contacts</span>' +
        '<div class="segmented" role="radiogroup" aria-label="Existing contacts">' +
        [['skip', 'Skip them'], ['fill', 'Fill their blank fields']].map(([k, l]) =>
          '<button type="button" role="radio" aria-checked="' + (_imp.policy === k) + '" class="' + (_imp.policy === k ? 'on' : '') +
          '" onclick="impSetPolicy(\'' + k + '\')">' + l + '</button>').join('') + '</div>' +
        '<div class="segmented" role="tablist" aria-label="Rows">' + filters.map(([k, l]) =>
          '<button type="button" role="tab" aria-selected="' + (_imp.filter === k) + '" class="' + (_imp.filter === k ? 'on' : '') +
          '" onclick="impSetFilter(\'' + k + '\')">' + l + '</button>').join('') + '</div></div>' +
        impRowsTable(impRowsShown(p.rows), false) +
        '<p class="drawer-note">Filling never replaces a value that is already there, and never changes a contact\'s status, ' +
        'verification or notes. Opted-out and invalid contacts are always skipped.</p></div>' +
        '<div class="imp-step imp-commit">' +
        (invalidLeft ? '<label class="check"><input type="checkbox"' + (_imp.skipInvalid ? ' checked' : '') +
          ' onchange="_imp.skipInvalid = this.checked; renderImport()"> Leave out the ' + invalidLeft + ' invalid row' +
          (invalidLeft === 1 ? '' : 's') + ' and import the rest</label>' : '') +
        '<button class="btn btn-primary btn-sm" onclick="impCommit()"' +
        ((_imp.busy || !going || (invalidLeft && !_imp.skipInvalid)) ? ' disabled' : '') + '>Import ' + going + ' contact' +
        (going === 1 ? '' : 's') + '</button></div>';
    }
  }
  html += impBatchesHtml() + '</div>';
  el.innerHTML = html;
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
      // 'harvey' = threads recorded before the rename; still our side.
      const ours = msg.sender === 'mercury' || msg.sender === 'harvey';
      const cls = ours ? 'sent' : 'received';
      threadHtml += '<div class="thread-msg ' + cls + '"><div class="sender">' + escHtml(ours ? 'mercury' : msg.sender) +
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
async function getJSON(path, opts) {
  try {
    const r = await fetch(path, opts);
    return { ok: r.ok, status: r.status, data: await r.json() };
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
  const actions = document.getElementById('drawer-actions');
  if (actions) actions.innerHTML = '';
  const body = document.getElementById('drawer-body');
  body.innerHTML = bodyHtml || '';
  body.scrollTop = 0;
  if (!drawerOpen()) _drawerPrevFocus = document.activeElement;
  d.classList.add('open');
  d.setAttribute('aria-hidden', 'false');
  document.getElementById('drawer-scrim').classList.add('open');
  const close = d.querySelector('.drawer-head > .btn-square');
  if (close) close.focus({preventScroll: true});
}

// Re-render an open drawer without resetting its scroll position or moving
// focus. Used by quiet background refreshes and in-drawer toggles, where
// openDrawer's scroll-to-top and focus-the-close-button would get in the way.
function updateDrawer(kicker, title, subHtml, bodyHtml) {
  if (!drawerOpen()) { openDrawer(kicker, title, subHtml, bodyHtml); return; }
  const body = document.getElementById('drawer-body');
  const top = body.scrollTop;
  const active = document.activeElement;
  const activeId = active && body.contains(active) ? active.id : '';
  const selStart = active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  const selEnd = active && typeof active.selectionEnd === 'number' ? active.selectionEnd : null;
  document.getElementById('drawer-kicker').textContent = kicker || '';
  document.getElementById('drawer-title').textContent = title || '';
  document.getElementById('drawer-sub').innerHTML = subHtml || '';
  body.innerHTML = bodyHtml || '';
  body.scrollTop = top;
  if (activeId) {
    const again = document.getElementById(activeId);
    if (again) {
      again.focus({ preventScroll: true });
      if (selStart !== null && typeof again.setSelectionRange === 'function') {
        try { again.setSelectionRange(selStart, selEnd); } catch { /* not a text control */ }
      }
    }
  }
}

function closeDrawer() {
  if (!drawerOpen()) return;
  const d = document.getElementById('drawer');
  d.classList.remove('open');
  d.setAttribute('aria-hidden', 'true');
  document.getElementById('drawer-scrim').classList.remove('open');
  _drawerCtx = null;
  if (_mb.view) { _mb.view = null; document.querySelectorAll('#mb-table .mb-row.sel').forEach(r => r.classList.remove('sel')); }
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
  if (it.status === 'imported') meta.push(badge('imported'));
  if (it.email_status) meta.push(badge(it.email_status));
  if (next) {
    meta.push('<span class="pc-fact" title="Next email: ' + escHtml(fullWhen(next)) + '">' + icon('clock') +
      escHtml(next < new Date() ? 'Due now' : relWhen(next)) + '</span>');
  }
  if (sent) meta.push('<span class="pc-fact">' + icon('paper-plane-tilt') + sent + ' sent</span>');
  // A score of 0 means "not scored yet"; showing it on every card is noise.
  const hasScore = it.score !== null && it.score !== undefined && it.score !== '' && Number(it.score) > 0;

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
      ['From', e.mailbox ? '<span class="mono">' + escHtml(e.mailbox) + '</span>'
        : '<span class="muted">' + (e.status === 'sent' ? '—' : 'Picked when it sends') + '</span>'],
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
// Play a chart's entrance animation only when its data changes, not on every
// resize redraw or quiet 15-second refresh. CSS does the work under .ch-anim.
function chartIntro(el, sig) {
  if (!el || el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.classList.remove('ch-anim');
  void el.offsetWidth;                       // restart if it was mid-animation
  el.classList.add('ch-anim');
  clearTimeout(el._introT);
  el._introT = setTimeout(() => el.classList.remove('ch-anim'), 1600);
}

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
      rateDelta(t.bounce_rate, p.bounce_rate, p.sent, false, days), 'mailboxes', bounceTone);
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
             '<path class="tr-line tr-sent" pathLength="1" d="' + line + '"/>';
    }
    if (show.replies) {
      svg += '<path class="tr-line tr-replies" pathLength="1" d="' + smoothPath(series.map((r, i) => [x(i), y(r.replies || 0)])) + '"/>';
    }
  }
  svg += '<line class="ch-base" x1="' + x0 + '" x2="' + x1 + '" y1="' + y1 + '" y2="' + y1 + '"/>' +
    '<g class="ch-hover"></g></svg>';

  wrap.innerHTML = svg + (empty
    ? '<div class="chart-empty">' + icon('chart-line-up') + '<span>No sends yet — the trend appears after Mercury\'s first emails go out.</span></div>'
    : '');
  chartIntro(wrap, _trend.days + '|' + JSON.stringify(d.totals || {}) + '|' + JSON.stringify(show));

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

// ── Mailboxes: every inbox Mercury sends from ──
//
// Which inboxes exist, their daily caps and their ramp (warmup_start, then
// +warmup_weekly_increase every week) come from mercury.yaml →
// channels.email.mailboxes, edited here through /api/settings/mailboxes. The
// sender enforces those caps, lowered by the health gate (bounces) or a
// manual pause. The tab is a table of every inbox, grouped by sending domain
// so DNS (a domain setting) shows once, with a drawer per inbox (ramp,
// health, checklist, notes, settings) and per domain (DNS records).

let _mb = {
  data: null, key: '', seq: 0, q: '', filter: 'all', open: {}, dns: {}, dnsLoading: {},
  view: null, settings: null, test: {},
};

const MB_FILTERS = [['all', 'All'], ['needs', 'Needs you'], ['warming', 'Warming'], ['soon', 'Starting soon'], ['warm', 'Warm']];
const mbEnc = email => encodeURIComponent(email);
const mbDomainOf = email => (String(email || '').split('@')[1] || '').toLowerCase();
const mbList = () => (_mb.data && _mb.data.inboxes) || [];
const mbFind = email => mbList().find(b => b.email === email) || null;
const mbDns = domain => ((_mb.data && _mb.data.domains) || {})[domain] || null;
// A record that is missing or wrong needs the user; a lookup that couldn't run (network) doesn't.
const mbDnsFails = domain => { const d = mbDns(domain); return !!d && (d.checks || []).some(c => c.status === 'fail'); };
const mbArg = s => escHtml(JSON.stringify(String(s)));   // safe inline-handler string argument

// Stage, as a status word + tone. Paused and held override the ramp stage.
function mbStage(b) {
  const gate = (b.health || {}).gate || 'ok';
  if (b.status === 'paused') return { key: 'paused', label: 'Paused', tone: 'bad' };
  if (gate === 'hold') return { key: 'hold', label: 'On hold', tone: 'waiting' };
  if (b.accepts_new === false) return { key: 'replies', label: 'Replies only', tone: 'idle' };
  if (b.stage === 'scheduled') return { key: 'soon', label: 'Starts ' + shortDay(b.start_date), tone: 'idle' };
  if (b.stage === 'warming' || b.stage === 'fixed') return { key: 'warming', label: 'Warming', tone: 'active' };
  return { key: 'warm', label: 'Warm', tone: 'good' };
}

function mbOpenTasks(b) {
  if (!b.start_date) return [];   // added already warm: Mercury never ramped it, so no catch-up list
  const cur = b.current_week === null || b.current_week === undefined ? 0 : b.current_week;
  return (b.weeks || []).filter(w => w.week <= cur)
    .flatMap(w => (w.tasks || []).filter(t => !t.done && t.key !== 'dns').map(t => ({ ...t, week: w.week, cur: w.week === cur })));
}

// What the inbox needs next, worst first: [text, tone] where tone '' is muted.
function mbNext(b) {
  const st = mbStage(b);
  if (st.key === 'paused') return [b.verdict === 'CANCEL_CANDIDATE' ? 'Domain burned: consider replacing it' : 'Resume once bounces are fixed', 'bad'];
  if (b.configured === false) return ['Add the inbox password', 'bad'];
  if (st.key === 'hold') return ['Bounces over 3%', 'waiting'];
  if (b.throttled_until) return ['Throttled: cap halved until ' + shortDay(b.throttled_until.slice(0, 10)), 'waiting'];
  if (mbDnsFails(b.domain)) return ['Waiting on domain DNS', ''];
  if (st.key === 'soon') return ['Starts ' + shortDay(b.start_date), ''];
  const open = mbOpenTasks(b);
  if (open.length) return ['Checklist: ' + open.length + ' open', ''];
  return ['Nothing to do', ''];
}

function mbNeeds(b) {
  const st = mbStage(b);
  return st.key === 'paused' || st.key === 'hold' || !!b.throttled_until || b.configured === false || mbDnsFails(b.domain);
}

// Bounce buckets, as the handler classifies them from the DSN status code.
const MB_BUCKETS = [
  ['LIST', 'Bad address'], ['SENDER', 'Sender blocked'], ['BURNED', 'Domain burned'],
  ['THROTTLE', 'Throttled'], ['NOISE', 'Ignored'], ['UNKNOWN', 'No code'],
];

function mbBucketSection(b) {
  const bb = b.bounce_buckets || {};
  const bad = (bb.SENDER || 0) + (bb.BURNED || 0);
  let notes = '';
  if (b.verdict === 'CANCEL_CANDIDATE') {
    notes += '<div class="ev-note bad">' + icon('warning-circle') + '<span><b>' + escHtml(b.domain) + ' is a cancel candidate.</b> ' +
      'A receiving server rejected it as burned (5.7.606 to 5.7.614). Every inbox on the domain is paused. Resuming one clears this flag.</span></div>';
  }
  if (b.throttled_until) {
    notes += '<div class="ev-note">' + icon('warning-circle') + '<span>A server asked Mercury to slow down (a 4.x.x reply), so this inbox sends half its usual cap until ' +
      escHtml(shortDay(b.throttled_until.slice(0, 10))) + '.</span></div>';
  }
  return '<div class="drawer-section"><div class="mb-sec-head"><h4>Bounce types</h4><span class="muted">last 7 days</span></div>' +
    '<div class="mb-stats">' + MB_BUCKETS.map(([k, label]) =>
      '<div><span>' + label + '</span><b class="' + ((k === 'SENDER' || k === 'BURNED') && bb[k] ? 'is-bad' : k === 'THROTTLE' && bb[k] ? 'is-wait' : '') + '">' +
      fmtN(bb[k] || 0) + '</b></div>').join('') + '</div>' + notes +
    (bad ? '<p class="drawer-note">Sender and burned blocks are reputation problems, not list problems. Fix the cause before resuming.</p>' : '') +
    '</div>';
}

function mbInFilter(b, k) {
  const st = mbStage(b).key;
  switch (k) {
    case 'needs': return mbNeeds(b);
    case 'warming': return st === 'warming' || (st === 'hold' && b.stage === 'warming');
    case 'soon': return st === 'soon';
    case 'warm': return st === 'warm';
    default: return true;
  }
}

function mbMatches(b) {
  const q = _mb.q.trim().toLowerCase();
  if (q && !(b.email + ' ' + (b.name || '')).toLowerCase().includes(q)) return false;
  return mbInFilter(b, _mb.filter);
}

function mbBusy() {
  const a = document.activeElement;
  return modalOpen() || !!(a && a.closest && a.closest('#drawer, #mailboxes') && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.id !== 'mb-search');
}

async function loadMailboxes(quiet) {
  const body = document.getElementById('mb-body');
  const seq = ++_mb.seq;
  const [res, voices] = await Promise.all([getJSON('/api/warmup'), getJSON('/api/voices')]);
  if (seq !== _mb.seq) return;
  if (voices.ok) { _mb.voices = voices.data.mailboxes; _mb.voicePersonas = voices.data.personas; _mb.voiceDefault = voices.data.default_id; }
  if (!res.ok || !res.data || !Array.isArray(res.data.inboxes)) {
    if (quiet && _mb.data) return;
    _mb.data = null; _mb.key = '';
    body.innerHTML = unavailableState(res, 'envelope-simple', 'Mailboxes');
    return;
  }
  const key = JSON.stringify(res.data);
  if (quiet && key === _mb.key) return;
  _mb.data = res.data; _mb.key = key;
  mbNavFlag();
  renderMailboxes();
  if (quiet) mbRefreshDrawer();
}

function mbNavFlag() {
  const el = document.getElementById('nav-mailboxes');
  if (!el) return;
  const list = mbList();
  const worst = list.some(b => b.status === 'paused') ? 'bad'
    : list.some(b => mbNeeds(b)) ? 'wait' : '';
  el.hidden = !worst;
  el.className = 'nav-flag ' + worst;
  el.title = worst === 'bad' ? 'An inbox is paused' : worst === 'wait' ? 'A mailbox needs you' : '';
}

function mbConfigHint() {
  return '<div class="wu-config"><p>' + icon('info') + '<span>Mailboxes live in <span class="mono">mercury.yaml</span> &rarr; ' +
    '<span class="mono">channels.email.mailboxes</span>. Add one here or edit the file; Mercury picks changes up on its next run.</span></p></div>';
}

function renderMailboxes() {
  const body = document.getElementById('mb-body');
  const list = mbList();
  if (!list.length) {
    const note = _mb.data && _mb.data.note;
    body.innerHTML = '<section class="panel">' + emptyState('envelope-simple',
        note ? 'Nothing to manage here' : 'No sending inbox yet',
        note ? escHtml(note) : 'Add the inbox Mercury sends from. It shows up here with its warm-up ramp, ' +
          'bounce monitoring and a setup checklist.') +
      (note ? '' : '<div class="panel-body mb-empty-act"><button class="btn btn-primary" onclick="mbOpenSettings(null)">' +
        icon('plus') + 'Add inbox</button></div>') +
      '<div class="panel-foot">' + mbConfigHint() + '</div></section>';
    return;
  }
  if (!document.getElementById('mb-table')) {
    body.innerHTML = '<div class="kpis kpis-3" id="mb-summary"></div>' +
      '<div class="mb-limits" id="mb-limits"></div>' +
      '<div class="toolbar mb-toolbar">' +
        '<label class="search-field">' + icon('magnifying-glass') +
          '<input type="search" id="mb-search" placeholder="Search ' + list.length + ' inboxes" autocomplete="off" ' +
          'aria-label="Search inboxes" oninput="mbSearch(this.value)"></label>' +
        '<div class="segmented" id="mb-filters" role="tablist" aria-label="Filter inboxes"></div>' +
        '<button class="btn btn-primary btn-sm mb-add" onclick="mbOpenSettings(null)">' + icon('plus') + 'Add inbox</button>' +
      '</div>' +
      '<div class="table-card mb-table" id="mb-table"></div>';
  }
  const search = document.getElementById('mb-search');
  if (search) search.placeholder = 'Search ' + list.length + ' inbox' + (list.length === 1 ? '' : 'es');
  document.getElementById('mb-summary').innerHTML = mbSummary();
  document.getElementById('mb-limits').innerHTML = mbLimitNotes();
  document.getElementById('mb-filters').innerHTML = MB_FILTERS.map(([k, label]) => {
    const n = list.filter(b => mbInFilter(b, k)).length;
    return '<button role="tab" aria-selected="' + (_mb.filter === k) + '" class="' + (_mb.filter === k ? 'on' : '') +
      '" onclick="mbSetFilter(\'' + k + '\')">' + label + '<span class="mb-count' + (k === 'needs' && n ? ' bad' : '') + '">' + n + '</span></button>';
  }).join('');
  renderMbTable();
}

// Inbox lifecycle limits the config breaks (too many inboxes on a domain, a
// cap over the provider ceiling, an inbox younger than two weeks). Advisory:
// nothing is lowered, so the operator decides.
function mbLimitNotes() {
  const w = (_mb.data && _mb.data.limit_warnings) || [];
  if (!w.length) return '';
  return '<section class="panel"><div class="panel-head"><div><h3>Inbox limits</h3><p>' + w.length +
    (w.length === 1 ? ' limit is' : ' limits are') + ' broken. Mercury still sends at the configured caps.</p></div></div>' +
    '<div class="panel-body mb-limit-list">' + w.map(x =>
      '<div>' + toneBadge('waiting', x.code === 'domain_inboxes' ? 'Too many inboxes'
        : x.code === 'cap_over_ceiling' ? 'Cap too high' : 'Young inbox') +
      '<span>' + escHtml(x.message) + '</span></div>').join('') + '</div></section>';
}

function mbSearch(v) { _mb.q = v || ''; renderMbTable(); }
function mbSetFilter(k) { _mb.filter = k; renderMailboxes(); }

function mbSummary() {
  const d = _mb.data || {};
  const list = mbList();
  const kpi = (label, value, foot, extra) =>
    '<div class="kpi static"><span class="kpi-label">' + label + '</span>' + value + (extra || '') +
    '<span class="kpi-foot">' + foot + '</span></div>';

  const sent = Number(d.sent_24h || 0), cap = Number(d.capacity_today || 0);
  const pct = cap ? Math.min(100, sent / cap * 100) : 0;
  const sending = kpi('Sending today',
    '<span class="kpi-value tight' + (sent ? '' : ' zero') + '">' + fmtN(sent) + '<small>/ ' + fmtN(cap) + ' allowed</small></span>',
    cap ? (Math.max(0, cap - sent) ? '<b>' + fmtN(Math.max(0, cap - sent)) + '</b> more allowed in the last 24 h' : 'Today\'s capacity is used up')
      + (d.capped_by_global ? ' &middot; overall limit ' + fmtN(d.max_daily_sends) : '')
      : 'No cold email allowed today',
    '<span class="kpi-bar' + (cap && sent >= cap ? ' full' : '') + '"><span style="width:' + pct.toFixed(1) + '%"></span></span>');

  const groups = [['warm', 'warm', 'mb-s-good'], ['warming', 'warming', 'mb-s-active'], ['soon', 'starting soon', 'mb-s-idle'],
    ['hold', 'on hold', 'mb-s-wait'], ['paused', 'paused', 'mb-s-bad'], ['replies', 'replies only', 'mb-s-idle']];
  const counts = {};
  list.forEach(b => { const k = mbStage(b).key; counts[k] = (counts[k] || 0) + 1; });
  const stack = '<span class="mb-stack" aria-hidden="true">' + groups.filter(g => counts[g[0]]).map(g =>
    '<span class="' + g[2] + '" style="flex:' + counts[g[0]] + '"></span>').join('') + '</span>';
  const inboxes = kpi('Inboxes',
    '<span class="kpi-value tight">' + fmtN(list.length) + '<small>across ' + fmtN(new Set(list.map(b => b.domain)).size) + ' domain' +
      (new Set(list.map(b => b.domain)).size === 1 ? '' : 's') + '</small></span>',
    groups.filter(g => counts[g[0]]).map(g => '<b>' + counts[g[0]] + '</b> ' + g[1]).join(' &middot; '), stack);

  const issues = [];
  const failing = [...new Set(list.filter(b => mbDnsFails(b.domain)).map(b => b.domain))];
  failing.forEach(dm => issues.push(['bad', dm + ' fails DNS checks']));
  list.filter(b => b.status === 'paused').forEach(b => issues.push(['bad', b.email + ' is paused']));
  const held = list.filter(b => mbStage(b).key === 'hold').length;
  if (held) issues.push(['waiting', held + ' ramp' + (held === 1 ? '' : 's') + ' on hold from bounces']);
  const limits = ((d.limit_warnings) || []).length;
  if (limits) issues.push(['waiting', limits + ' inbox limit' + (limits === 1 ? '' : 's') + ' broken']);
  const noPw = list.filter(b => b.configured === false).length;
  if (noPw) issues.push(['bad', noPw + ' inbox' + (noPw === 1 ? '' : 'es') + ' missing a password']);
  const needs = kpi('Needs you',
    '<span class="kpi-value tight' + (issues.length ? '' : ' zero') + '">' + issues.length + '<small>' + (issues.length === 1 ? 'item' : 'items') + '</small></span>',
    issues.length ? '<span class="mb-issues">' + issues.slice(0, 3).map(i => toneBadge(i[0], i[1])).join('') +
      (issues.length > 3 ? '<span class="muted">and ' + (issues.length - 3) + ' more</span>' : '') + '</span>'
      : toneBadge('good', 'Nothing needs you'));
  return sending + inboxes + needs;
}

function mbMini(parts, total) {
  return '<span class="mb-mini" aria-hidden="true">' + parts.filter(p => p[0] > 0).map(p =>
    '<span class="' + p[1] + '" style="width:' + Math.min(100, p[0] / (total || 1) * 100).toFixed(1) + '%"></span>').join('') + '</span>';
}

function mbDnsMini(domain) {
  const d = mbDns(domain);
  if (!d) return '<span class="mb-dns muted">DNS not checked</span>';
  const keys = ['mx', 'spf', 'dkim', 'dmarc'];
  const st = Object.fromEntries((d.checks || []).map(c => [c.key, c.status]));
  const pass = keys.filter(k => st[k] === 'pass').length;
  return '<span class="mb-dns" title="MX, SPF, DKIM, DMARC">' + keys.map(k =>
    '<span class="mb-dns-b s-' + escHtml(st[k] || 'unknown') + '"></span>').join('') +
    '<span class="mb-dns-l">' + (d.all_pass ? 'DNS ok' : (mbDnsFails(domain) || (d.checks || []).some(c => c.status === 'warn')) ? pass + '/4 DNS' : 'DNS unclear') + '</span></span>';
}

function renderMbTable() {
  const el = document.getElementById('mb-table');
  if (!el) return;
  const list = mbList();
  const shown = list.filter(mbMatches);
  if (!shown.length) {
    el.innerHTML = '<div class="panel-body">' + emptyState('magnifying-glass', 'No inboxes match',
      'Try a different search or filter.') + '</div>';
    return;
  }
  const byDomain = {};
  shown.forEach(b => (byDomain[String(b.domain || mbDomainOf(b.email)).toLowerCase()] ||= []).push(b));
  const domainNeeds = dm => (byDomain[dm] || []).some(mbNeeds) || mbDnsFails(dm);
  const domains = Object.keys(byDomain).sort((a, b) => (domainNeeds(b) - domainNeeds(a)) || a.localeCompare(b));
  const filtering = !!_mb.q.trim() || _mb.filter !== 'all';
  const autoOpen = list.length <= 12;

  const rows = domains.map(dm => {
    const boxes = byDomain[dm];
    const all = list.filter(b => b.domain === dm);
    const open = filtering || (_mb.open[dm] !== undefined ? _mb.open[dm] : (autoOpen || domainNeeds(dm)));
    const sent = all.reduce((s, b) => s + Number(b.sent_today || 0), 0);
    const cap = all.reduce((s, b) => s + Number(b.today_cap || 0), 0);
    const flags = [];
    if (mbDnsFails(dm)) flags.push(toneBadge('bad', 'Fix DNS'));
    const paused = all.filter(b => b.status === 'paused').length;
    if (paused) flags.push(toneBadge('bad', paused + ' paused'));
    const held = all.filter(b => mbStage(b).key === 'hold').length;
    if (held) flags.push(toneBadge('waiting', held + ' on hold'));
    const head = '<tr class="mb-group"><td colspan="7"><div class="mb-group-in">' +
      '<button class="mb-caret" aria-expanded="' + open + '" aria-label="' + (open ? 'Collapse ' : 'Expand ') + escHtml(dm) +
        '" onclick="mbToggleDomain(' + mbArg(dm) + ')">' + icon(open ? 'caret-down' : 'caret-right') + '</button>' +
      '<button class="mb-domain mono" onclick="mbOpenDomain(' + mbArg(dm) + ')">' + escHtml(dm) + '</button>' +
      '<span class="mb-group-meta">' + all.length + ' inbox' + (all.length === 1 ? '' : 'es') + ' &middot; <span class="mono">' + fmtN(sent) + '/' + fmtN(cap) + '</span> today</span>' +
      mbDnsMini(dm) + flags.join('') +
      '<button class="link-btn mb-group-open" onclick="mbOpenDomain(' + mbArg(dm) + ')">Domain' + icon('arrow-right') + '</button>' +
    '</div></td></tr>';
    if (!open) return head;
    return head + boxes.map(b => {
      const st = mbStage(b);
      const [next, nextTone] = mbNext(b);
      const target = Number(b.target_daily || 0), tcap = Number(b.today_cap || 0), sentB = Number(b.sent_today || 0);
      const ramp = st.key === 'soon' ? mbMini([], 1) + '<span class="mb-cell-n">not started</span>'
        : b.ramp_days ? mbMini([[Math.min(b.day || 0, b.ramp_days), b.stage === 'warm' ? 'mb-s-good' : 'mb-s-active']], b.ramp_days) +
            '<span class="mb-cell-n">Day ' + fmtN(Math.min(b.day || 0, b.ramp_days)) + '/' + fmtN(b.ramp_days) + '</span>'
        : mbMini([[1, 'mb-s-good']], 1) + '<span class="mb-cell-n">Full</span>';
      const today = tcap
        ? mbMini([[sentB, 'mb-s-sent'], [Math.max(0, tcap - sentB), 'mb-s-left']], target || tcap) + '<span class="mb-cell-n">' + fmtN(sentB) + '/' + fmtN(tcap) + '</span>'
        : mbMini([], 1) + '<span class="mb-cell-n muted">none</span>';
      const h = b.health || {};
      const br = h.bounce_rate;
      const brCls = br === null || br === undefined ? ' muted' : br >= 0.05 ? ' is-bad' : br >= 0.03 ? ' is-wait' : '';
      return '<tr class="mb-row' + (_mb.view && _mb.view.email === b.email ? ' sel' : '') + '" tabindex="0" data-email="' + escHtml(b.email) + '" ' +
        'onclick="mbOpenInbox(' + mbArg(b.email) + ')" onkeydown="if(event.key===\'Enter\'){event.preventDefault();mbOpenInbox(' + mbArg(b.email) + ')}">' +
        '<td class="mb-c-inbox"><span class="mono">' + escHtml(b.email) + '</span></td>' +
        '<td>' + toneBadge(st.tone, st.label) + '</td>' +
        '<td><span class="mb-cell">' + ramp + '</span></td>' +
        '<td><span class="mb-cell">' + today + '</span></td>' +
        '<td class="num' + brCls + '">' + fmtPct(br) + '</td>' +
        '<td class="num' + (h.reply_rate === null || h.reply_rate === undefined ? ' muted' : '') + '">' + fmtPct(h.reply_rate) + '</td>' +
        '<td class="mb-c-next' + (nextTone === 'bad' ? ' is-bad' : nextTone === 'waiting' ? ' is-wait' : '') + '">' + escHtml(next) + '</td>' +
      '</tr>';
    }).join('');
  }).join('');

  el.innerHTML = '<table><thead><tr><th>Inbox</th><th>Stage</th><th>Ramp</th><th>Today</th>' +
    '<th class="num">Bounces 7d</th><th class="num">Replies 7d</th><th>Next</th></tr></thead><tbody>' + rows + '</tbody></table>' +
    '<div class="panel-foot mb-foot"><span>Domains that need you come first' + (autoOpen ? '.' : '; the rest stay folded.') + '</span>' +
    '<span class="muted">' + fmtN(shown.length) + ' of ' + fmtN(list.length) + ' shown</span></div>';
}

function mbToggleDomain(dm) {
  const el = document.querySelector('#mb-table');
  const list = mbList();
  const cur = _mb.open[dm] !== undefined ? _mb.open[dm]
    : (list.length <= 12 || list.some(b => b.domain === dm && mbNeeds(b)) || mbDnsFails(dm));
  _mb.open[dm] = !cur;
  if (el) renderMbTable();
}

// ── Inbox drawer ──

function mbDrawerActions(html) {
  const el = document.getElementById('drawer-actions');
  if (el) el.innerHTML = html || '';
}

function mbOpenInbox(email, mode, inPlace) {
  const b = mbFind(email);
  if (!b) return;
  _mb.view = { kind: 'inbox', email, mode: mode || 'overview' };
  if (_mb.view.mode === 'settings') { mbOpenSettings(email); return; }
  const st = mbStage(b);
  const peers = mbList().filter(x => x.domain === b.domain);
  const idx = peers.findIndex(x => x.email === email);
  const sub = toneBadge(st.tone, st.label) +
    (b.ramp_days && b.day ? '<span class="mb-sub-t">Day ' + fmtN(Math.min(b.day, b.ramp_days)) + ' of ' + fmtN(b.ramp_days) + '</span>'
      : st.key === 'soon' ? '<span class="mb-sub-t">Ramp starts ' + escHtml(shortDay(b.start_date)) + '</span>' : '');
  (inPlace ? updateDrawer : openDrawer)(escHtml(b.domain) + (peers.length > 1 ? ' · ' + (idx + 1) + ' of ' + peers.length : ''), b.email, sub, mbInboxBody(b));
  const nav = peers.length > 1
    ? '<button class="btn-square xs" title="Previous inbox" aria-label="Previous inbox" onclick="mbStep(-1)">' + icon('caret-up') + '</button>' +
      '<button class="btn-square xs" title="Next inbox" aria-label="Next inbox" onclick="mbStep(1)">' + icon('caret-down') + '</button>' : '';
  mbDrawerActions(nav +
    (b.status === 'paused'
      ? '<button class="btn btn-primary btn-sm" onclick="mbAction(\'resume\')">' + icon('play') + 'Resume</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="mbAction(\'pause\')">' + icon('pause') + 'Pause</button>') +
    '<button class="btn-square" title="Inbox settings" aria-label="Inbox settings" onclick="mbOpenSettings(' + mbArg(b.email) + ')">' + icon('gear-six') + '</button>');
  const wrap = document.getElementById('mb-ramp');
  if (wrap) { mbDrawRamp(); observeWidth(wrap, mbDrawRamp); }
  document.querySelectorAll('#mb-table .mb-row').forEach(r => r.classList.toggle('sel', r.dataset.email === b.email));
}

function mbStep(dir) {
  const b = _mb.view && mbFind(_mb.view.email);
  if (!b) return;
  const peers = mbList().filter(x => x.domain === b.domain);
  const i = peers.findIndex(x => x.email === b.email);
  const next = peers[(i + dir + peers.length) % peers.length];
  if (next) mbOpenInbox(next.email);
}

function mbRefreshDrawer() {
  if (!drawerOpen() || !_mb.view || mbBusy()) return;
  if (_mb.view.kind === 'inbox' && _mb.view.mode === 'overview') mbOpenInbox(_mb.view.email, undefined, true);
  else if (_mb.view.kind === 'domain') mbOpenDomain(_mb.view.domain, true);
}

function mbInboxBody(b) {
  const st = mbStage(b);
  const h = b.health || {};
  const target = Number(b.target_daily || 0), cap = Number(b.today_cap || 0), sent = Number(b.sent_today || 0);
  const left = Math.max(0, Number(b.remaining || 0));
  const headline = st.key === 'paused' ? 'Paused. No cold email goes out until you resume.'
    : st.key === 'soon' ? 'The ramp starts ' + escHtml(longDay(b.start_date)) + '.'
    : b.configured === false ? 'Add the password before this inbox can send.'
    : st.key === 'replies' ? 'Replies only. No new outreach from this inbox.'
    : !cap ? 'No cold email allowed today.'
    : left ? 'Can send ' + fmtN(left) + ' more today.' : 'Today\'s cap is used up.';
  const locked = Math.max(0, target - cap);
  let html = '<div class="drawer-section mb-today"><p class="mb-headline">' + headline + '</p>' +
    (target ? '<div class="mb-dist" aria-hidden="true">' +
      (sent ? '<span class="mb-s-sent" style="flex:' + sent + '"></span>' : '') +
      (Math.max(0, cap - sent) ? '<span class="mb-s-left" style="flex:' + Math.max(0, cap - sent) + '"></span>' : '') +
      (locked ? '<span class="mb-s-lock" style="flex:' + locked + '"></span>' : '') + '</div>' +
      '<div class="mb-legend"><span><i class="mb-s-sent"></i>' + fmtN(sent) + ' sent</span>' +
        '<span><i class="mb-s-left"></i>' + fmtN(Math.max(0, cap - sent)) + ' left today</span>' +
        (locked ? '<span><i class="mb-s-lock"></i>' + fmtN(locked) + ' unlock as it warms</span>' : '') + '</div>' : '') +
    (b.cap_reason ? '<p class="drawer-note">Limit: ' + escHtml(b.cap_reason) + '</p>' : '') +
    '</div>';

  if ((b.plan || []).length) {
    html += '<div class="drawer-section"><div class="mb-sec-head"><h4>Ramp</h4><span class="muted">' + fmtN(cap) + '/day now' +
      (b.full_on ? ', ' + fmtN(target) + '/day from ' + escHtml(shortDay(b.full_on)) : '') + '</span></div>' +
      '<div class="chart-wrap ramp mb-ramp" id="mb-ramp"></div>' +
      '<div class="legend static mb-ramp-legend"><span class="legend-btn"><span class="sw sw-cap"></span>Planned cap</span>' +
        '<span class="legend-btn"><span class="sw sw-sent-bar"></span>Sent</span>' +
        '<span class="legend-btn"><span class="sw sw-target"></span>Daily cap</span></div></div>';
  } else {
    html += '<div class="drawer-section"><h4>Ramp</h4><p class="drawer-note">Already at full volume: up to <b>' + fmtN(target) +
      ' a day</b>. Give it a warm-up start date in settings to ramp it.</p></div>';
  }

  html += '<div class="drawer-section"><div class="mb-stats">' +
    '<div><span>Bounces 7d</span><b class="' + (h.gate === 'pause' ? 'is-bad' : h.gate === 'hold' ? 'is-wait' : '') + '">' + fmtPct(h.bounce_rate) + '</b></div>' +
    '<div><span>Replies 7d</span><b>' + fmtPct(h.reply_rate) + '</b></div>' +
    '<div><span>Sent 7d</span><b>' + fmtN(h.sent_7d || 0) + '</b></div></div>' +
    (h.gate && h.gate !== 'ok' || b.status === 'paused'
      ? '<div class="ev-note bad">' + icon('warning-circle') + '<span>' + escHtml(b.pause_reason || h.reason || 'Paused by hand.') +
        ' Mercury holds the ramp at 3% bounces and pauses the inbox at 5%. Replies still go out.</span></div>' : '') +
    '</div>';

  html += mbBucketSection(b);

  html += mbVoiceSection(b.email);

  if (mbDnsFails(b.domain)) {
    const d = mbDns(b.domain);
    const fails = (d.checks || []).filter(c => c.status === 'fail').length;
    html += '<div class="ev-note bad mb-dns-note">' + icon('warning-circle') + '<span><b>' + escHtml(b.domain) + ' fails ' + fails + ' DNS check' +
      (fails === 1 ? '' : 's') + '.</b> Shared by every inbox on the domain, so fix it once for all of them.</span>' +
      '<button class="link-btn" onclick="mbOpenDomain(' + mbArg(b.domain) + ')">Open domain' + icon('arrow-right') + '</button></div>';
  }

  const open = mbOpenTasks(b);
  const all = (b.weeks || []).flatMap(w => w.tasks || []).filter(t => t.key !== 'dns');
  const done = all.filter(t => t.done).length;
  html += '<div class="drawer-section"><div class="mb-sec-head"><h4>Checklist</h4><span class="muted">' + done + ' of ' + all.length + ' done</span></div>' +
    (open.length ? '<div class="mb-tasks">' + open.map(t => mbTask(t, t.cur ? 'this week' : (t.week ? 'week ' + t.week : 'setup'))).join('') + '</div>'
      : '<p class="drawer-note">' + (b.start_date ? 'Nothing open for this stage of the ramp.' : 'This inbox was added already warm, so there is nothing to catch up on.') + '</p>') +
    '<details class="mb-plan"><summary>Full plan</summary>' + (b.weeks || []).map(w =>
      '<div class="mb-plan-week"><div class="mb-plan-h"><b>' + (w.week === 0 ? 'Setup' : 'Week ' + w.week) + ': ' + escHtml(w.title) +
        '</b><span class="muted">' + escHtml(w.range || '') + '</span></div>' +
      (w.tasks || []).filter(t => t.key !== 'dns').map(t => mbTask(t, '')).join('') + '</div>').join('') + '</details></div>';

  html += '<div class="drawer-section"><h4><label for="mb-notes">Notes</label></h4><textarea class="form-input wu-notes" id="mb-notes" rows="3" ' +
    'placeholder="e.g. Google Postmaster verified Oct 2; forwarding set up for replies" onblur="mbSaveNotes(this)">' +
    escHtml(b.notes || '') + '</textarea><div class="wu-notes-hint" id="mb-notes-hint">Saves when you click away.</div></div>';
  return html;
}

function mbTask(t, tag) {
  const id = 'mbt-' + escHtml(t.key) + (tag ? '' : '-p');
  return '<label class="wu-task' + (t.done ? ' done' : '') + '" for="' + id + '">' +
    '<input type="checkbox" id="' + id + '" ' + (t.done ? 'checked ' : '') +
      'onchange="mbToggleTask(\'' + escHtml(t.key) + '\', this.checked)">' +
    '<span class="wu-task-l">' + escHtml(t.label) + '</span>' +
    (tag ? '<span class="mb-task-tag' + (tag !== 'this week' ? ' late' : '') + '">' + escHtml(tag) + '</span>' : '') + '</label>';
}

async function mbToggleTask(key, done) {
  const b = _mb.view && mbFind(_mb.view.email);
  if (!b) return;
  const task = (b.weeks || []).flatMap(w => w.tasks || []).find(t => t.key === key);
  if (!task) return;
  task.done = done;
  const res = await postJSON('/api/warmup/inboxes/' + mbEnc(b.email) + '/task', { key, done });
  if (!res.ok) {
    task.done = !done;
    showToast('Couldn\'t save that: ' + res.error, 'error');
  }
  _mb.key = JSON.stringify(_mb.data);
  mbOpenInbox(b.email, undefined, true);
  renderMbTable();
}

async function mbSaveNotes(ta) {
  const b = _mb.view && mbFind(_mb.view.email);
  if (!b || (b.notes || '') === ta.value) return;
  const hint = document.getElementById('mb-notes-hint');
  if (hint) hint.textContent = 'Saving…';
  const res = await postJSON('/api/warmup/inboxes/' + mbEnc(b.email), { notes: ta.value });
  if (res.ok) {
    b.notes = ta.value;
    _mb.key = JSON.stringify(_mb.data);
    if (hint) hint.textContent = 'Saved.';
  } else {
    if (hint) hint.textContent = 'Not saved: ' + res.error;
    showToast('Couldn\'t save notes: ' + res.error, 'error');
  }
}

const MB_DONE = {
  pause: 'Inbox paused. No cold email goes out from it until you resume.',
  resume: 'Inbox resumed. The bounce window starts fresh from now.',
};

async function mbAction(action, email) {
  const b = mbFind(email || (_mb.view && _mb.view.email));
  if (!b) return false;
  if (action === 'pause' && !(await confirmModal({
    title: 'Pause ' + b.email + '?',
    copy: 'Openers and follow-ups from this inbox wait until you resume (nothing is cancelled). Replies to people who wrote back still go out.',
    ok: 'Pause inbox',
  }))) return false;
  const res = await postJSON('/api/warmup/inboxes/' + mbEnc(b.email) + '/action', { action });
  if (res.ok) showToast(MB_DONE[action] || 'Done.', 'success');
  else showToast('Couldn\'t ' + action + ': ' + res.error, 'error');
  await loadMailboxes();
  if (!email && drawerOpen() && _mb.view && _mb.view.kind === 'inbox') mbOpenInbox(b.email);
  return res.ok;
}

// The original warm-up ramp: week bands, planned cap with sent laid over it,
// the daily-cap line and a Today marker.
function mbDrawRamp() {
  const wrap = document.getElementById('mb-ramp');
  const b = _mb.view && mbFind(_mb.view.email);
  if (!wrap || !b) return;
  const plan = b.plan || [];
  const W = wrap.clientWidth;
  if (!W || !plan.length) return;
  const n = plan.length, H = W < 560 ? 210 : 252;
  const target = Number(b.target_daily || 0);
  const max = Math.max(target, ...plan.map(p => Math.max(p.cap || 0, p.sent || 0)));
  const { top, ticks } = niceTicks(max * 1.08, 4);
  const padL = 14 + Math.max(2, String(top).length) * 7, padR = 6, padT = 26, padB = 30;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const slot = (x1 - x0) / n, bw = Math.max(3, Math.min(22, slot * 0.62));
  const cx = i => x0 + slot * (i + 0.5);
  const y = v => y1 - (v / top) * (y1 - y0);
  const todayIdx = b.day ? plan.findIndex(p => p.day === b.day) : -1;
  const curWeek = todayIdx >= 0 ? Math.floor(todayIdx / 7) : -1;

  let svg = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
    'aria-label="Send ramp: planned daily cap and emails actually sent">';
  for (let w = 0; w * 7 < n; w++) {
    const bx = x0 + w * 7 * slot, bwid = Math.min(7, n - w * 7) * slot;
    svg += '<rect class="rp-band' + (w % 2 ? ' alt' : '') + (w === curWeek ? ' cur' : '') + '" x="' + bx.toFixed(1) + '" y="' + (y0 - 20) +
      '" width="' + bwid.toFixed(1) + '" height="' + (y1 - y0 + 20) + '" rx="6"/>' +
      '<text class="rp-week' + (w === curWeek ? ' cur' : '') + '" x="' + (bx + 6).toFixed(1) + '" y="' + (y0 - 6) + '">W' + (w + 1) + '</text>';
  }
  svg += yAxis(ticks, y, x0, x1, false);
  svg += '<g>' + plan.map((p, i) => {
    let r = '<rect class="rp-cap' + (i === todayIdx ? ' today' : '') + (todayIdx >= 0 && i > todayIdx ? ' future' : '') +
      '" x="' + (cx(i) - bw / 2).toFixed(1) + '" y="' + y(p.cap || 0).toFixed(1) + '" width="' + bw.toFixed(1) +
      '" height="' + Math.max(0, y1 - y(p.cap || 0)).toFixed(1) + '" rx="2"/>';
    if (p.sent !== null && p.sent !== undefined && p.sent > 0) {
      r += '<rect class="rp-sent' + (p.sent > (p.cap || 0) ? ' over' : '') + '" x="' + (cx(i) - bw / 2).toFixed(1) + '" y="' + y(p.sent).toFixed(1) +
        '" width="' + bw.toFixed(1) + '" height="' + (y1 - y(p.sent)).toFixed(1) + '" rx="2"/>';
    }
    return r;
  }).join('') + '</g>';
  if (target) {
    svg += '<line class="rp-target" x1="' + x0 + '" x2="' + x1 + '" y1="' + y(target).toFixed(1) + '" y2="' + y(target).toFixed(1) + '"/>' +
      '<text class="rp-target-l" x="' + (x1 - 2) + '" y="' + (y(target) - 6).toFixed(1) + '" text-anchor="end">Daily cap ' + fmtN(target) + '/day</text>';
  }
  const xl = [];
  for (let i = 0; i < n; i += 14) xl.push(i);
  if (n - 1 - xl[xl.length - 1] >= 6) xl.push(n - 1);
  svg += '<g class="ch-x">' + xl.filter(i => todayIdx < 0 || Math.abs(i - todayIdx) * slot > 40).map(i =>
    '<text x="' + cx(i).toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle">' + svgEsc(shortDay(plan[i].date)) + '</text>').join('') + '</g>';
  if (todayIdx >= 0) {
    svg += '<line class="rp-today" x1="' + cx(todayIdx).toFixed(1) + '" x2="' + cx(todayIdx).toFixed(1) + '" y1="' + y0 + '" y2="' + (y1 + 4) + '"/>' +
      '<text class="rp-today-l" x="' + cx(todayIdx).toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle">Today</text>';
  }
  svg += '<line class="ch-base" x1="' + x0 + '" x2="' + x1 + '" y1="' + y1 + '" y2="' + y1 + '"/><g class="ch-hover"></g></svg>';
  wrap.innerHTML = svg;
  chartIntro(wrap, (b.email || '') + '|' + plan.map(p => p.cap + ':' + (p.sent ?? '')).join(','));
  chartHover(wrap, {
    xs: plan.map((_, i) => cx(i)), x0, x1, y0: y0 - 20, y1, band: slot * 0.92,
    html: i => {
      const p = plan[i];
      const when = i === todayIdx ? 'Today' : (todayIdx >= 0 && i > todayIdx) ? 'Planned' : '';
      return '<div class="tip-date">Day ' + p.day + ' &middot; ' + svgEsc(longDay(p.date)) + '</div>' +
        '<div class="tip-row"><span class="sw sw-cap"></span>Cap<b>' + fmtN(p.cap) + '</b></div>' +
        '<div class="tip-row"><span class="sw sw-sent-bar"></span>Sent<b>' + (p.sent === null || p.sent === undefined ? '–' : fmtN(p.sent)) + '</b></div>' +
        (when ? '<div class="tip-row sub">' + when + '</div>' : '');
    },
  });
}

// ── Inbox settings (in the drawer) ──

async function mbLoadSettings() {
  const data = await api('/api/settings/mailboxes');
  _mb.settings = data && !data.error ? data : null;
  return _mb.settings;
}

async function mbOpenSettings(email) {
  const s = await mbLoadSettings();
  const adding = !email;
  const live = email ? mbFind(email) : null;
  const b = s && email ? (s.inboxes || []).find(x => x.email === email) : null;
  _mb.view = { kind: 'inbox', email: email || null, mode: 'settings' };
  if (!s || (email && !b)) {
    openDrawer(email ? 'Inbox settings' : 'New inbox', email || 'Add inbox', '',
      '<div class="ev-note bad">' + icon('warning-circle') + '<span>Couldn\'t load inbox settings. Check the configuration and try again.</span></div>');
    mbDrawerActions(email ? '<button class="btn btn-secondary btn-sm" onclick="mbOpenInbox(' + mbArg(email) + ')">' + icon('caret-left') + 'Back</button>' : '');
    return;
  }
  const defaults = s.defaults || {};
  const v = (k, d = '') => (b && b[k] !== undefined && b[k] !== null ? b[k] : d);
  const input = (id, label, type, value, extra = '', hint = '') => '<div class="form-group"><label class="form-label" for="mbs-' + id + '">' + label +
    '</label><input class="form-input" id="mbs-' + id + '" type="' + type + '" value="' + escHtml(value) + '" ' + extra + '>' +
    (hint ? '<p class="mb-hint">' + hint + '</p>' : '') + '</div>';
  const status = live && live.status === 'paused' ? 'paused' : (b && b.enabled === false ? 'replies' : 'sending');
  const smtpHost = v('smtp_host') || defaults.smtp_host || '';
  const imapHost = v('imap_host') || defaults.imap_host || '';
  const ready = b && b.password_set && b.configured;
  const conn = b ? (ready ? toneBadge('good', 'Ready') : !b.password_set ? toneBadge('waiting', 'Password needed') : toneBadge('waiting', 'Server needed')) : '';
  const t = email && _mb.test[email];

  const html = '<form id="mbs-form" class="mb-settings" onsubmit="mbSaveSettings(event)" autocomplete="off">' +
    (adding ? '' : '<div class="drawer-section"><h4>Status</h4>' +
      '<div class="segmented mb-status" role="radiogroup" aria-label="Inbox status">' +
      [['sending', 'Sending', 'arrow-circle-right'], ['replies', 'Replies only', 'arrow-bend-up-left'], ['paused', 'Paused', 'pause-circle']].map(([k, l, ic]) =>
        '<button type="button" role="radio" aria-checked="' + (status === k) + '" class="' + (status === k ? 'on' : '') + '" data-status="' + k + '" ' +
        'onclick="mbPickStatus(this)">' + icon(ic) + l + '</button>').join('') + '</div>' +
      '<p class="mb-hint">Replies only and Paused both keep answering people who wrote back. Follow-ups wait; nothing is cancelled.</p></div>') +
    '<div class="drawer-section"><h4>Sender</h4><div class="form-row">' +
      input('email', 'Email address', 'email', v('email', ''), 'required' + (adding ? '' : ' readonly'),
        adding ? '' : 'Can\'t change: existing threads use it.') +
      input('name', 'Sender name', 'text', v('name', ''), 'placeholder="Uses your default sender name"') + '</div></div>' +
    '<div class="drawer-section"><h4>Connection</h4>' +
      (b ? facts([
        ['Provider', '<span class="mb-prov"><span>' + (s.provider === 'smtp' ? 'SMTP + IMAP' : escHtml(s.provider)) + '</span>' + conn + '</span>'],
        ['Sends via', smtpHost ? '<span class="mono">' + escHtml(smtpHost) + ':' + escHtml(v('smtp_port') || defaults.smtp_port || 587) + '</span>' : '<span class="muted">Not set</span>'],
        ['Reads replies', imapHost ? '<span class="mono">' + escHtml(imapHost) + ':' + escHtml(v('imap_port') || defaults.imap_port || 993) + '</span>' : '<span class="muted">Not set</span>'],
      ]) : '') +
      input('password', b && b.password_set ? 'Change password' : 'Password or app password', 'password', '',
        'autocomplete="new-password" placeholder="' + (b && b.password_set ? 'Saved. Leave blank to keep it.' : 'Can be added later') + '"',
        'For Google Workspace, create an app password under Security, 2-Step Verification.') +
      (b ? '<div class="mb-test"><button type="button" class="btn btn-secondary btn-sm" id="mbs-test" onclick="mbTestInbox()">' +
        icon('lightning') + 'Test connection</button><span class="mb-hint" id="mbs-test-msg">' + escHtml(t || '') + '</span></div>' : '') +
      '<details class="inbox-servers mb-servers"' + (!defaults.smtp_host || (b && !b.configured) ? ' open' : '') + '><summary>Server settings</summary>' +
        '<p class="mb-hint">Blank fields use the shared SMTP and IMAP settings.</p>' +
        '<div class="form-row">' + input('smtp-host', 'SMTP host', 'text', v('smtp_host'), 'placeholder="' + escHtml(defaults.smtp_host || 'smtp.example.com') + '"') +
          input('smtp-port', 'SMTP port', 'number', v('smtp_port') || '', 'min="1" max="65535" placeholder="' + (defaults.smtp_port || 587) + '"') + '</div>' +
        '<div class="form-row">' + input('imap-host', 'IMAP host', 'text', v('imap_host'), 'placeholder="' + escHtml(defaults.imap_host || 'imap.example.com') + '"') +
          input('imap-port', 'IMAP port', 'number', v('imap_port') || '', 'min="1" max="65535" placeholder="' + (defaults.imap_port || 993) + '"') + '</div>' +
        '<div class="form-row">' + input('username', 'SMTP login', 'text', v('username'), 'placeholder="Defaults to the inbox email"') +
          input('imap-username', 'IMAP login', 'text', v('imap_username'), 'placeholder="Defaults to the SMTP login"') + '</div>' +
        input('imap-password', b && b.imap_password_set ? 'Change IMAP password' : 'IMAP password', 'password', '',
          'autocomplete="new-password" placeholder="' + (b && b.imap_password_set ? 'Saved. Leave blank to keep it.' : 'Leave blank to use the SMTP password') + '"',
          'Only needed when the mailbox reads replies with a different password.') +
      '</details></div>' +
    '<div class="drawer-section"><h4>Sending</h4><div class="form-row">' +
      input('cap', 'Daily limit at full volume', 'number', v('daily_cap', 30), 'required min="0" step="1"',
        'All inboxes share the overall limit of ' + fmtN(s.max_daily_sends) + ' a day.') +
      input('start', 'Warm-up start date', 'date', b ? (b.warmup_start || '') : s.today, '',
        'Clear the date only if this inbox is already warmed up.') + '</div></div>' +
    (s.provider !== 'smtp' ? '<label class="inbox-checkbox"><input type="checkbox" id="mbs-activate" required>Switch email sending from ' +
      escHtml(s.provider) + ' to SMTP + IMAP</label>' : '') +
    '<p class="form-error" id="mbs-error" role="alert" hidden></p>' +
    '<div class="drawer-actions mb-settings-foot"><button type="submit" class="btn btn-primary btn-sm" id="mbs-save">' +
      (adding ? 'Add inbox' : 'Save changes') + '</button>' +
      '<button type="button" class="btn btn-secondary btn-sm" onclick="' + (adding ? 'closeDrawer()' : 'mbOpenInbox(' + mbArg(email) + ')') + '">Cancel</button>' +
      '<span class="mb-hint">Applies on Mercury\'s next run.</span></div>' +
  '</form>';
  openDrawer(adding ? 'New inbox' : 'Inbox settings', adding ? 'Add inbox' : email, '', html);
  mbDrawerActions(adding ? '' : '<button class="btn btn-secondary btn-sm" onclick="mbOpenInbox(' + mbArg(email) + ')">' + icon('caret-left') + 'Back</button>');
  const first = document.getElementById(adding ? 'mbs-email' : 'mbs-name');
  if (first) first.focus({ preventScroll: true });
}

function mbPickStatus(btn) {
  btn.parentElement.querySelectorAll('button').forEach(x => {
    const on = x === btn;
    x.classList.toggle('on', on);
    x.setAttribute('aria-checked', on);
  });
}

async function mbSaveSettings(event) {
  event.preventDefault();
  const button = document.getElementById('mbs-save');
  if (!button || button.disabled) return;
  const email = _mb.view && _mb.view.email;
  const val = id => { const el = document.getElementById('mbs-' + id); return el ? el.value : ''; };
  const picked = document.querySelector('#mbs-form .mb-status button.on');
  const status = picked ? picked.dataset.status : 'sending';
  const data = {
    email: val('email').trim(), name: val('name').trim(), password: val('password'),
    daily_cap: Number(val('cap')), warmup_start: val('start') || null,
    smtp_host: val('smtp-host').trim(), smtp_port: Number(val('smtp-port')) || 0,
    imap_host: val('imap-host').trim(), imap_port: Number(val('imap-port')) || 0,
    username: val('username').trim(), imap_username: val('imap-username').trim(),
    imap_password: val('imap-password'),
    enabled: status !== 'replies',
  };
  const activate = document.getElementById('mbs-activate');
  if (activate) data.activate_smtp = activate.checked;
  const err = document.getElementById('mbs-error');
  button.disabled = true;
  const result = await getJSON('/api/settings/mailboxes' + (email ? '/' + mbEnc(email) : ''), {
    method: email ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
  });
  button.disabled = false;
  if (!result.ok || !result.data || !result.data.success) {
    err.hidden = false;
    err.textContent = (result.data && result.data.message) || 'Could not save. Try again.';
    return;
  }
  const target = email || data.email.toLowerCase();
  const live = mbFind(target);
  const wasPaused = !!live && live.status === 'paused';
  const followUp = !email ? null
    : status === 'paused' && !wasPaused ? 'pause'
    : status !== 'paused' && wasPaused ? 'resume' : null;
  if (followUp) {
    const action = await postJSON('/api/warmup/inboxes/' + mbEnc(target) + '/action', { action: followUp });
    if (!action.ok) {
      showToast('Inbox settings saved, but the inbox could not be ' + (followUp === 'pause' ? 'paused' : 'resumed') +
        ': ' + action.error + '. Use the ' + (followUp === 'pause' ? 'Pause' : 'Resume') + ' button to try again.', 'error');
      _mb.key = '';
      await loadMailboxes();
      if (mbFind(target)) mbOpenInbox(target); else closeDrawer();
      return;
    }
  }
  showToast(result.data.restart_required
    ? 'Inbox saved. Stop and start Mercury in Controls to apply it to the running agent.'
    : 'Inbox saved. It applies on Mercury\'s next run.', 'success');
  _mb.key = '';
  await loadMailboxes();
  if (mbFind(target)) mbOpenInbox(target); else closeDrawer();
}

async function mbTestInbox() {
  const email = _mb.view && _mb.view.email;
  const btn = document.getElementById('mbs-test');
  const msg = document.getElementById('mbs-test-msg');
  if (!email || !btn) return;
  btn.disabled = true;
  if (msg) msg.textContent = 'Testing…';
  const res = await getJSON('/api/settings/mailboxes/' + mbEnc(email) + '/test', { method: 'POST' });
  btn.disabled = false;
  const text = (res.data && res.data.message) || 'Could not test the connection. Try again.';
  _mb.test[email] = text;
  if (msg) msg.textContent = text;
}

// ── Domain drawer: DNS once per domain, and its inboxes ──

const DNS_ICON = {
  pass: ['check-circle', 'Pass'], warn: ['warning', 'Needs attention'],
  fail: ['x-circle', 'Missing'], unknown: ['question', 'Couldn\'t check'],
};

function agoShort(d) {
  if (!d) return '';
  const min = Math.round((Date.now() - d) / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return min + ' min ago';
  if (min < 24 * 60) return Math.round(min / 60) + ' h ago';
  return relWhen(d);
}

function mbOpenDomain(domain, inPlace) {
  _mb.view = { kind: 'domain', domain };
  const boxes = mbList().filter(b => b.domain === domain);
  (inPlace ? updateDrawer : openDrawer)('Domain', domain, mbDomainSub(domain),
    '<div class="drawer-section"><div class="mb-sec-head"><h4>DNS records</h4>' +
      '<button class="link-btn" id="mb-dns-recheck" onclick="mbLoadDns(' + mbArg(domain) + ', true)">' + icon('arrow-clockwise') + 'Re-check</button></div>' +
      '<p class="drawer-note">Add these at your domain registrar. One fix covers every inbox on ' + escHtml(domain) + '.</p>' +
      '<div class="mb-dns-list" id="mb-dns-list"><div class="loading-note">Checking DNS…</div></div></div>' +
    '<div class="drawer-section"><div class="mb-sec-head"><h4>Inboxes on this domain</h4><span class="muted mono">' +
      fmtN(boxes.reduce((s, b) => s + Number(b.sent_today || 0), 0)) + '/' + fmtN(boxes.reduce((s, b) => s + Number(b.today_cap || 0), 0)) + ' today</span></div>' +
      '<div class="ev-list">' + boxes.map(b => {
        const st = mbStage(b);
        return '<button class="ev-row" onclick="mbOpenInbox(' + mbArg(b.email) + ')"><span class="ev-kind">' + icon('envelope-simple') + '</span>' +
          '<span class="ev-main"><b class="mono">' + escHtml(b.email) + '</b><small>' + escHtml(mbNext(b)[0]) + '</small></span>' +
          '<span class="ev-side">' + toneBadge(st.tone, st.label) + '<small class="mono">' + fmtN(b.sent_today || 0) + '/' + fmtN(b.today_cap || 0) + ' today</small></span></button>';
      }).join('') + '</div></div>');
  mbDrawerActions('');
  if (_mb.dns[domain]) mbRenderDns(domain);
  mbLoadDns(domain, false);
}

function mbDomainSub(domain) {
  const d = mbDns(domain);
  const n = mbList().filter(b => b.domain === domain).length;
  const pass = d ? (d.checks || []).filter(c => c.status === 'pass').length : 0;
  return (!d ? toneBadge('idle', 'Not checked yet')
    : d.all_pass ? toneBadge('good', 'All DNS records pass')
    : mbDnsFails(domain) ? toneBadge('bad', pass + ' of 4 records pass')
    : toneBadge('waiting', pass + ' of 4 pass, the rest couldn\'t be checked')) +
    '<span class="mb-sub-t">' + n + ' inbox' + (n === 1 ? '' : 'es') + '</span>';
}

async function mbLoadDns(domain, force) {
  if (_mb.dnsLoading[domain]) return;
  _mb.dnsLoading[domain] = true;
  const btn = document.getElementById('mb-dns-recheck');
  if (btn && force) btn.disabled = true;
  if (!force && _mb.dns[domain] && !_mb.dns[domain].error) { _mb.dnsLoading[domain] = false; return; }
  const res = await getJSON('/api/warmup/dns?domain=' + encodeURIComponent(domain));
  _mb.dnsLoading[domain] = false;
  if (btn) btn.disabled = false;
  _mb.dns[domain] = res.ok && res.data && Array.isArray(res.data.checks) ? res.data : { error: res };
  if (!_mb.dns[domain].error && _mb.data) {
    const checks = _mb.dns[domain].checks;
    (_mb.data.domains ||= {})[domain] = {
      checks: checks.map(c => ({ key: c.key, status: c.status })), checked_at: _mb.dns[domain].checked_at,
      all_pass: ['mx', 'spf', 'dkim', 'dmarc'].every(k => (checks.find(c => c.key === k) || {}).status === 'pass'),
    };
    renderMailboxes();
    mbNavFlag();
  }
  if (_mb.view && _mb.view.kind === 'domain' && _mb.view.domain === domain) {
    mbRenderDns(domain);
    document.getElementById('drawer-sub').innerHTML = mbDomainSub(domain);
  }
}

function mbRenderDns(domain) {
  const el = document.getElementById('mb-dns-list');
  const d = _mb.dns[domain];
  if (!el || !d) return;
  if (d.error) {
    el.innerHTML = '<div class="chart-note inline">' + icon('info') + '<span>' +
      (d.error.status === 404 ? 'DNS checks aren\'t available on this server yet.' : 'Couldn\'t run the DNS check right now.') + '</span></div>';
    return;
  }
  el.innerHTML = d.checks.map(c => {
    const m = DNS_ICON[c.status] || DNS_ICON.unknown;
    return '<div class="dns-row s-' + escHtml(c.status) + '">' +
      '<span class="dns-ic">' + icon(m[0]) + '</span>' +
      '<div class="dns-main">' +
        '<div class="dns-top"><b>' + escHtml(c.label || String(c.key).toUpperCase()) + '</b><span class="dns-st">' + m[1] + '</span></div>' +
        (c.detail ? '<div class="dns-detail">' + escHtml(c.detail) + '</div>' : '') +
        (c.record ? '<div class="dns-rec"><code title="' + escHtml(c.record) + '">' + escHtml(c.record) + '</code>' +
          '<button class="btn-square xs ghost" title="Copy record" aria-label="Copy ' + escHtml(c.label || c.key) + ' record" ' +
          'data-copy="' + escHtml(c.record) + '" onclick="mbCopy(this)">' + icon('copy') + '</button></div>' : '') +
      '</div></div>';
  }).join('') +
    (d.checked_at ? '<p class="dns-checked mb-dns-when">Checked ' + escHtml(agoShort(parseUTC(d.checked_at))) + '</p>' : '');
}

async function mbCopy(btn) {
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
  const cells = days.map((d, i) =>
    '<i class="hm-c l' + level(d.sent) + '" style="--w:' + Math.floor(i / 7) + '" data-d="' + d.date + '" data-n="' + d.sent +
    '" data-r="' + (d.replies || 0) + '"></i>').join('');

  grid.innerHTML =
    '<div class="hm" style="--weeks:' + weeks + '">' +
      '<div class="hm-months">' + months + '</div>' +
      '<div class="hm-days"><span></span><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span></div>' +
      '<div class="hm-grid" role="img" aria-label="' + data.total_sent + ' emails sent in the last year">' + cells + '</div>' +
    '</div>';

  // Narrow screens scroll; start at the most recent week, like GitHub.
  grid.scrollLeft = grid.scrollWidth;
  chartIntro(grid, _hmSig);

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
  // The tooltip lives on <body>: the grid scrolls sideways on small screens,
  // and a scroll container would clip anything that pokes out of it.
  const tip = document.createElement('div');
  tip.className = 'hm-tip';
  tip.hidden = true;
  document.body.appendChild(tip);
  const hide = () => { tip.hidden = true; };
  wrap.addEventListener('mouseover', e => {
    const c = e.target.closest('.hm-grid .hm-c');
    if (!c) return;
    const n = Number(c.dataset.n), r = Number(c.dataset.r);
    tip.innerHTML = '<b>' + (n ? n + ' email' + (n === 1 ? '' : 's') + ' sent' : 'No emails sent') + '</b>' +
      (r ? '<span>' + r + ' repl' + (r === 1 ? 'y' : 'ies') + '</span>' : '') +
      '<small>' + hmDate(c.dataset.d, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }) + '</small>';
    tip.hidden = false;
    const cr = c.getBoundingClientRect();
    const half = tip.offsetWidth / 2;
    const x = Math.max(half + 8, Math.min(cr.left + cr.width / 2, window.innerWidth - half - 8));
    let y = cr.top - tip.offsetHeight - 8;
    if (y < 8) y = cr.bottom + 8;          // no room above: show below
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  wrap.addEventListener('mouseleave', hide);
  wrap.addEventListener('scroll', hide, { passive: true });
  document.addEventListener('scroll', hide, { passive: true, capture: true });
  document.addEventListener('visibilitychange', hide);
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
    case 'mailboxes': if (!mbBusy()) loadMailboxes(true); break;
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
