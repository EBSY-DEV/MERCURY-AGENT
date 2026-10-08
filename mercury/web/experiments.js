/* Experiments: compare two versions of outreach on real replies.

   Every number comes from /api/experiments (mercury/experiments.py computes
   the counts, intervals and decision); this file only lays them out. Uses the
   shared helpers in app.js: toneBadge, icon, confirmModal, openDrawer, the
   chart helpers (niceTicks, yAxis, chartHover, smoothPath, observeWidth). */

Object.assign(PH, {
  "flask": "<path d=\"M221.69,199.77,160,96.92V40h8a8,8,0,0,0,0-16H88a8,8,0,0,0,0,16h8V96.92L34.31,199.77A16,16,0,0,0,48,224H208a16,16,0,0,0,13.72-24.23ZM110.86,103.25A7.93,7.93,0,0,0,112,99.14V40h32V99.14a7.93,7.93,0,0,0,1.14,4.11L183.36,167c-12,2.37-29.07,1.37-51.75-10.11-15.91-8.05-31.05-12.32-45.22-12.81ZM48,208l28.54-47.58c14.25-1.74,30.31,1.85,47.82,10.72,19,9.61,35,12.88,48,12.88a69.89,69.89,0,0,0,19.55-2.7L208,208Z\"/>",
  "smiley": "<path d=\"M128,24A104,104,0,1,0,232,128,104.11,104.11,0,0,0,128,24Zm0,192a88,88,0,1,1,88-88A88.1,88.1,0,0,1,128,216ZM80,108a12,12,0,1,1,12,12A12,12,0,0,1,80,108Zm96,0a12,12,0,1,1-12-12A12,12,0,0,1,176,108Zm-1.07,48c-10.29,17.79-27.4,28-46.93,28s-36.63-10.2-46.92-28a8,8,0,1,1,13.84-8c7.47,12.91,19.21,20,33.08,20s25.61-7.1,33.07-20a8,8,0,0,1,13.86,8Z\"/>",
  "chat-circle": "<path d=\"M128,24A104,104,0,0,0,36.18,176.88L24.83,210.93a16,16,0,0,0,20.24,20.24l34.05-11.35A104,104,0,1,0,128,24Zm0,192a87.87,87.87,0,0,1-44.06-11.81,8,8,0,0,0-6.54-.67L40,216,52.47,178.6a8,8,0,0,0-.66-6.54A88,88,0,1,1,128,216Z\"/>",
  "hand-palm": "<path d=\"M188,88a27.75,27.75,0,0,0-12,2.71V60a28,28,0,0,0-41.36-24.6A28,28,0,0,0,80,44v6.71A27.75,27.75,0,0,0,68,48,28,28,0,0,0,40,76v76a88,88,0,0,0,176,0V116A28,28,0,0,0,188,88Zm12,64a72,72,0,0,1-144,0V76a12,12,0,0,1,24,0v44a8,8,0,0,0,16,0V44a12,12,0,0,1,24,0v68a8,8,0,0,0,16,0V60a12,12,0,0,1,24,0v68.67A48.08,48.08,0,0,0,120,176a8,8,0,0,0,16,0,32,32,0,0,1,32-32,8,8,0,0,0,8-8V116a12,12,0,0,1,24,0Z\"/>",
  "chart-bar": "<path d=\"M224,200h-8V40a8,8,0,0,0-8-8H152a8,8,0,0,0-8,8V80H96a8,8,0,0,0-8,8v40H48a8,8,0,0,0-8,8v64H32a8,8,0,0,0,0,16H224a8,8,0,0,0,0-16ZM160,48h40V200H160ZM104,96h40V200H104ZM56,144H88v56H56Z\"/>",
});

const _xp = {
  list: null, options: null, sel: '', detail: null, seq: 0, mode: 'rate',
  busy: false, sparks: {}, form: null,
};

const XP_STATUS_TONE = { running: 'active', draft: 'idle', paused: 'waiting', completed: 'good' };
const XP_RESULT_TONE = {
  not_started: 'idle', insufficient_data: 'waiting', check_deliverability: 'bad',
  no_clear_difference: 'note', a_ahead: 'good', b_ahead: 'good',
};
const XP_MINUS = '−';

const xpPct = v => (v === null || v === undefined || !isFinite(v)) ? '–' : (v * 100).toFixed(1) + '%';
const xpSigned = (n, digits) => (n < 0 ? XP_MINUS : '+') + Math.abs(n).toFixed(digits === undefined ? 1 : digits);
const xpPts = d => (d === null || d === undefined || !isFinite(d)) ? '–' : xpSigned(d * 100) + ' pts';
const xpStatus = (status, label) => toneBadge(XP_STATUS_TONE[status] || 'idle', label || status);
const xpResult = r => toneBadge(XP_RESULT_TONE[r.code] || 'idle', r.label);
const xpDay = iso => iso ? shortDay(String(iso).slice(0, 10)) : '';
const xpCap = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;

// ── Load ──

async function loadExperiments() {
  const body = document.getElementById('xp-body');
  if (!body) return;
  const seq = ++_xp.seq;
  const res = await getJSON('/api/experiments');
  if (seq !== _xp.seq) return;
  if (!res.ok || !res.data || !Array.isArray(res.data.experiments)) {
    body.innerHTML = unavailableState(res, 'flask', 'Experiments');
    return;
  }
  _xp.list = res.data.experiments;
  _xp.options = res.data.options || {};
  if (!_xp.list.length) {
    _xp.sel = ''; _xp.detail = null;
    body.innerHTML = '<div class="panel">' + emptyState('flask', 'No experiments yet',
      'An experiment compares two versions of your outreach, such as two opening angles, on real replies. ' +
      'Pick one thing to change and Mercury does the rest.') + '</div>';
    return;
  }
  if (!_xp.list.some(r => r.id === _xp.sel)) {
    const first = _xp.list.find(r => r.status === 'running') || _xp.list[0];
    _xp.sel = first.id;
  }
  const detail = await getJSON('/api/experiments/' + encodeURIComponent(_xp.sel));
  if (seq !== _xp.seq) return;
  _xp.detail = detail.ok && detail.data && detail.data.experiment ? detail.data : null;
  body.innerHTML = '<div id="xp-list"></div><div id="xp-detail"></div>';
  renderXpList();
  renderXpDetail(detail);
}

async function expSelect(id) {
  if (!id || id === _xp.sel || _xp.busy) return;
  _xp.sel = id;
  renderXpList();
  const seq = ++_xp.seq;
  const detail = await getJSON('/api/experiments/' + encodeURIComponent(id));
  if (seq !== _xp.seq) return;
  _xp.detail = detail.ok && detail.data && detail.data.experiment ? detail.data : null;
  renderXpDetail(detail);
}

// ── The table ──

function xpRates(r) {
  if (r.rate.A === null && r.rate.B === null) return '<span class="muted">–</span>';
  return xpPct(r.rate.A) + ' / ' + xpPct(r.rate.B);
}

function renderXpList() {
  const el = document.getElementById('xp-list');
  if (!el) return;
  const rows = _xp.list;
  const pick = r => 'onclick="expSelect(\'' + escAttr(r.id) + '\')" onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();expSelect(\'' +
    escAttr(r.id) + '\')}" tabindex="0" role="button" aria-pressed="' + (r.id === _xp.sel) + '"';
  el.innerHTML =
    '<div class="table-card xp-table"><table><thead><tr>' +
      '<th>Experiment</th><th>Status</th><th class="xp-col-testing">Testing</th><th class="num">Enrolled</th><th class="num">Mature</th>' +
      '<th class="num">Positive replies, A / B</th><th>Result</th></tr></thead><tbody>' +
    rows.map(r => '<tr class="xp-row' + (r.id === _xp.sel ? ' sel' : '') + '" ' + pick(r) + '>' +
      '<td class="xp-name">' + escHtml(r.name) + '<span class="xp-kind">' + escHtml(r.variable_label) + '</span></td>' +
      '<td>' + xpStatus(r.status, r.status_label) + '</td>' +
      '<td class="xp-col-testing">' + escHtml(r.variable_label) + '</td>' +
      '<td class="num">' + fmtN(r.enrolled.total) + '</td>' +
      '<td class="num">' + fmtN(r.mature.A + r.mature.B) + '</td>' +
      '<td class="num">' + xpRates(r) + '</td>' +
      '<td>' + xpResult(r.result) + '</td></tr>').join('') +
    '</tbody></table></div>' +
    '<div class="xp-cards">' + rows.map(r =>
      '<div class="xp-card' + (r.id === _xp.sel ? ' sel' : '') + '" ' + pick(r) + '>' +
        '<div class="xp-card-top"><b>' + escHtml(r.name) + '</b>' + xpStatus(r.status, r.status_label) + '</div>' +
        '<div class="xp-card-sub">' + escHtml(r.variable_label) + '</div>' +
        '<div class="xp-card-grid">' +
          '<span>Enrolled<b class="num">' + fmtN(r.enrolled.total) + '</b></span>' +
          '<span>Mature<b class="num">' + fmtN(r.mature.A + r.mature.B) + '</b></span>' +
          '<span>Positive, A / B<b class="num">' + xpRates(r) + '</b></span></div>' +
        '<div>' + xpResult(r.result) + '</div></div>').join('') + '</div>';
}

// ── The detail ──

function renderXpDetail(res) {
  const el = document.getElementById('xp-detail');
  if (!el) return;
  const d = _xp.detail;
  if (!d) {
    el.innerHTML = '<div class="panel">' + unavailableState(res, 'flask', 'This experiment') + '</div>';
    return;
  }
  const e = d.experiment, r = d.results;
  const [a, b] = r.arms;
  el.innerHTML =
    xpHeadHtml(e) + xpHealthHtml(r) + xpSampleHtml(e, r, a, b) + xpMetricsHtml(r, a, b) +
    '<div class="xp-charts">' + xpWeeksHtml(r, a, b) + xpDiffHtml(r) + '</div>' + xpArmsHtml(e, a, b);
  xpDrawAll();
}

function xpDrawAll() {
  document.querySelectorAll('#xp-detail [data-spark]').forEach(el => { xpDrawSpark(el); observeWidth(el, () => xpDrawSpark(el)); });
  const weeks = document.getElementById('xp-weeks-chart');
  if (weeks) { xpDrawWeeks(); observeWidth(weeks, xpDrawWeeks); }
  const diff = document.getElementById('xp-diff-chart');
  if (diff) { xpDrawDiff(); observeWidth(diff, xpDrawDiff); }
}

// Head: name, status, hypothesis, setup, and the controls the API allows.

function xpHeadHtml(e) {
  const c = e.controls || {}, fx = c.effects || {};
  // Each control carries its full effect as a tooltip and as its accessible description.
  const effect = { pause: fx.pause, resume: fx.pause, hold: fx.hold, release: fx.hold, complete: fx.complete };
  const btn = (action, ic, label, cls) => {
    const tip = effect[action] || '';
    return '<button class="btn ' + (cls || 'btn-secondary') + ' btn-sm" data-act="' + action + '"' +
      (tip ? ' title="' + escAttr(tip) + '" aria-describedby="xp-fx-' + action + '"' : '') +
      ' onclick="expAct(\'' + action + '\')">' + icon(ic) + label + '</button>' +
      (tip ? '<span class="sr-only" id="xp-fx-' + action + '">' + escHtml(tip) + '</span>' : '');
  };
  const buttons = [
    c.can_start ? btn('start', 'play', 'Start enrolling', 'btn-primary') : '',
    e.status === 'draft' && c.can_edit ? '<button class="btn btn-secondary btn-sm" onclick="expEdit()">' + icon('sliders-horizontal') + 'Edit</button>' : '',
    c.can_pause ? btn('pause', 'pause', 'Pause enrollment') : '',
    c.can_resume ? btn('resume', 'play', 'Resume enrollment') : '',
    c.can_hold ? btn('hold', 'hand-palm', 'Hold unsent mail') : '',
    c.can_release ? btn('release', 'play', 'Release held mail') : '',
    c.can_complete ? btn('complete', 'check-circle', 'Complete') : '',
  ].join('');
  const hint = (c.can_pause || c.can_resume || c.can_hold || c.can_release)
    ? 'Pausing stops new prospects. Holding also keeps approved emails from sending.' : '';
  const held = e.hold_mail
    ? '<span class="xp-held">' + toneBadge('waiting', 'Unsent mail held') + '</span>' : '';
  return '<section class="panel xp-head">' +
    '<div class="xp-head-main">' +
      '<div class="xp-title"><h3>' + escHtml(e.name) + '</h3>' + xpStatus(e.status, e.status_label) + held +
        '<span class="xp-rev">revision ' + e.revision + '</span></div>' +
      (e.hypothesis ? '<p class="xp-hyp">Hypothesis: ' + escHtml(e.hypothesis) + '</p>' : '') +
      '<p class="xp-setup">' + escHtml(e.setup_line) + '</p>' +
      (e.hold_mail && e.hold_reason ? '<p class="xp-setup">Held because: ' + escHtml(e.hold_reason) + '</p>' : '') +
    '</div>' +
    (buttons ? '<div class="xp-head-side"><div class="xp-actions">' + buttons + '</div>' +
      (hint ? '<p class="xp-hint">' + hint + '</p>' : '') + '</div>' : '') +
    '</section>';
}

const XP_DONE = {
  start: 'Enrollment is open.', pause: 'Enrollment paused.', resume: 'Enrollment is open again.',
  hold: 'Unsent mail is held.', release: 'Held mail goes out on its schedule.', complete: 'Experiment completed.',
};

async function expAct(action) {
  const d = _xp.detail;
  if (!d || _xp.busy) return;
  const e = d.experiment;
  let body = {};
  if (action === 'complete') {
    const ok = await confirmModal({
      title: 'Complete this experiment?',
      copy: (e.controls.effects || {}).complete || 'Completing ends enrollment for good.',
      ok: 'Complete',
    });
    if (!ok) return;
    body = { confirm: true };
  }
  _xp.busy = true;
  document.querySelectorAll('#xp-detail .xp-actions button').forEach(b => { b.disabled = true; });
  const res = await postJSON('/api/experiments/' + encodeURIComponent(e.id) + '/' + action, body);
  _xp.busy = false;
  showToast(res.ok ? XP_DONE[action] : 'Could not ' + action + ': ' + res.error, res.ok ? 'success' : 'error');
  loadExperiments();
}

// Health warnings point to the deliverability checks, never at the copy.

function xpHealthHtml(r) {
  const warnings = (r.health && r.health.warnings) || [];
  return warnings.map(w => {
    const tab = (w.action && w.action.tab) || 'mailboxes';
    return '<section class="panel xp-warn" role="alert"><span class="xp-warn-ic">' + icon('warning-circle') + '</span>' +
      '<p>' + escHtml(w.message) + '</p>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(\'' + escAttr(tab) + '\')">' +
        escHtml((w.action && w.action.label) || 'Check deliverability') + icon('arrow-right') + '</button></section>';
  }).join('');
}

// Sample callout: how far each arm is from the minimum, in 20 blocks.

function xpBlocks(arm, needed) {
  const N = 20, cap = Math.max(1, needed);
  const mature = Math.min(N, Math.round(Math.min(arm.mature, cap) * N / cap));
  const open = Math.max(0, Math.min(N, Math.round(Math.min(arm.mature + arm.pending, cap) * N / cap)) - mature);
  let html = '';
  for (let i = 0; i < N; i++) html += '<i class="' + (i < mature ? 'm' : i < mature + open ? 'o' : '') + '"></i>';
  return html;
}

function xpSampleHtml(e, r, a, b) {
  const dec = r.decision, needed = dec.mature_needed;
  let title, copy = dec.line;
  if (dec.code === 'not_started') {
    title = 'Nothing is enrolled yet';
  } else if (dec.mature_min < needed) {
    title = fmtN(dec.mature_min) + ' of ' + fmtN(needed) + ' mature prospects in each arm';
  } else {
    title = fmtN(a.mature) + ' and ' + fmtN(b.mature) + ' mature prospects in arms A and B';
  }
  if (dec.code === 'insufficient_data' && dec.earliest_decision_at) {
    const when = shortDay(dec.earliest_decision_at.slice(0, 10));
    copy = 'Mercury will not name a winner or stop an arm before both arms reach ' + fmtN(needed) + '. ' +
      (dec.duration_met ? '' : 'It also waits until the experiment has run ' + r.min_duration_days + ' days. ') +
      (dec.earliest_estimated ? 'At the current pace that is around ' + when + '.' : 'The earliest that can happen is ' + when + '.');
  }
  const row = (key, arm) => '<div class="xp-prow"><span class="xp-pk">Arm ' + key + '</span>' +
    '<span class="xp-blocks" role="img" aria-label="Arm ' + key + ': ' + arm.mature + ' of ' + needed + ' mature, ' +
      arm.pending + ' with the window still open">' + xpBlocks(arm, needed) + '</span>' +
    '<span class="xp-pn">' + fmtN(arm.mature) + ' / ' + fmtN(needed) + '</span></div>';
  return '<section class="panel xp-sample"><div class="xp-sample-text">' +
      '<div class="xp-state">' + xpResult(dec) + '</div>' +
      '<h3>' + escHtml(title) + '</h3><p>' + escHtml(copy) + '</p></div>' +
    '<div class="xp-progress">' + row('A', a) + row('B', b) +
      '<div class="xp-legend"><span><i class="xp-sq m"></i>Mature</span>' +
      '<span><i class="xp-sq o"></i>Window still open</span><span><i class="xp-sq"></i>Not enrolled yet</span></div>' +
    '</div></section>';
}

// Metric strip: four cells, each with its definition, a figure and a sparkline.

function xpMetricsHtml(r, a, b) {
  const defs = r.definitions || {};
  const ser = r.series || [];
  const rate = (row, key, field) => row[key].mature ? row[key][field] / row[key].mature : null;
  const both = (field) => ({
    A: ser.map(row => rate(row, 'A', field)),
    B: ser.map(row => rate(row, 'B', field)),
  });
  const matureAll = a.mature + b.mature;
  const bouncedAll = a.bounce.count + b.bounce.count;
  const optAll = a.opt_out.count + b.opt_out.count;
  const diff = (x, y) => (x === null || y === null) ? null : y - x;
  const stops = r.health && r.health.limits ? r.health.limits.sending_stops_at : null;
  _xp.sparks = {
    positive: { lines: [{ cls: 'a', v: both('positive').A }, { cls: 'b', v: both('positive').B, area: true }] },
    any: { lines: [{ cls: 'a', v: both('replied').A }, { cls: 'b', v: both('replied').B, area: true }] },
    bounce: { lines: [{ cls: 'good', v: ser.map(row => (row.A.mature + row.B.mature)
      ? (row.A.bounced + row.B.bounced) / (row.A.mature + row.B.mature) : null) }] },
    optout: { lines: [{ cls: 'a', v: ser.map(row => row.A.opted_out + row.B.opted_out), step: true }] },
  };
  const cell = (key, ic, label, tip, value, sub) =>
    '<div class="xp-metric"><div class="xp-mh"><span class="xp-ml">' + icon(ic) + label + '</span>' +
      '<span class="xp-info" tabindex="0" role="img" aria-label="' + escAttr(label + ': ' + tip) + '" data-tip="' + escAttr(tip) + '">' + icon('info') + '</span></div>' +
    '<div class="xp-mv"><b class="xp-v' + (value === '–' ? ' zero' : '') + '">' + value + '</b><span class="xp-sub">' + sub + '</span></div>' +
    '<div class="xp-spark" data-spark="' + key + '" role="img" aria-label="' + escAttr(label + ' over time') + '"></div></div>';
  const pair = (x, y) => 'B ' + xpPct(y) + ' · A ' + xpPct(x);
  return '<section class="xp-metrics" aria-label="Results so far">' +
    cell('positive', 'smiley', 'Positive reply rate', 'Shown as B minus A in percentage points. ' + (defs.positive || ''),
      xpPts(diff(a.positive.rate, b.positive.rate)), pair(a.positive.rate, b.positive.rate)) +
    cell('any', 'chat-circle', 'Any human reply', 'Shown as B minus A in percentage points. ' + (defs.replied || ''),
      xpPts(diff(a.any_reply.rate, b.any_reply.rate)), pair(a.any_reply.rate, b.any_reply.rate)) +
    cell('bounce', 'arrow-bend-up-left', 'Bounce rate', (defs.bounced || '') +
      (stops ? ' Mercury holds all sending if the overall bounce rate passes ' + Math.round(stops * 1000) / 10 + '%.' : ''),
      matureAll ? xpPct(bouncedAll / matureAll) : '–', stops ? 'holds at ' + Math.round(stops * 1000) / 10 + '%' : 'both arms') +
    cell('optout', 'prohibit', 'Opted out', defs.opted_out || '', fmtN(optAll),
      'B ' + fmtN(b.opt_out.count) + ' · A ' + fmtN(a.opt_out.count)) +
    '</section>';
}

function xpDrawSpark(el) {
  const spec = _xp.sparks[el.dataset.spark];
  const W = el.clientWidth;
  if (!spec || !W) return;
  const n = spec.lines[0].v.length;
  const H = 48, padX = 2, padY = 5;
  if (n < 2) {
    el.innerHTML = '<div class="xp-spark-empty">Appears as prospects mature</div>';
    return;
  }
  const all = spec.lines.flatMap(l => l.v).filter(v => v !== null && v !== undefined);
  if (!all.length) { el.innerHTML = '<div class="xp-spark-empty">Appears as prospects mature</div>'; return; }
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 1e-9) { lo -= 0.5 * (hi || 1) * 0.2; hi += 0.5 * (hi || 1) * 0.2; }
  const span = hi - lo;
  const x = i => padX + i * (W - 2 * padX) / (n - 1);
  const y = v => H - padY - (v - lo) / span * (H - 2 * padY);
  const id = 'xp-sf-' + el.dataset.spark;
  let svg = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true">' +
    '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="tr-stop-a"/><stop offset="1" class="tr-stop-b"/></linearGradient></defs>';
  spec.lines.forEach(l => {
    const pts = l.v.map((v, i) => v === null || v === undefined ? null : [x(i), y(v)]).filter(Boolean);
    if (pts.length < 2) return;
    let path;
    if (l.step) {
      path = 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1) + pts.slice(1).map((p, i) =>
        'H' + p[0].toFixed(1) + 'V' + p[1].toFixed(1)).join('');
    } else {
      path = smoothPath(pts);
    }
    if (l.area) {
      svg += '<path d="' + path + 'L' + pts[pts.length - 1][0].toFixed(1) + ',' + H + 'L' + pts[0][0].toFixed(1) + ',' + H + 'Z" fill="url(#' + id + ')"/>';
    }
    svg += '<path class="xp-line ' + l.cls + '" d="' + path + '"/>';
  });
  el.innerHTML = svg + '</svg>';
}

// Weekly bars: grouped A and B per week of first email.

function xpWeeksHtml(r, a, b) {
  const positive = a.positive.count + b.positive.count;
  const mature = a.mature + b.mature;
  return '<section class="panel xp-weeks"><div class="xp-ch">' +
      '<span class="xp-ct">' + icon('chart-bar') + 'Positive replies by week of first email</span>' +
      '<div class="segmented" role="group" aria-label="Show as" id="xp-mode">' +
        '<button class="' + (_xp.mode === 'rate' ? 'on' : '') + '" aria-pressed="' + (_xp.mode === 'rate') + '" onclick="expMode(\'rate\')">Rate</button>' +
        '<button class="' + (_xp.mode === 'count' ? 'on' : '') + '" aria-pressed="' + (_xp.mode === 'count') + '" onclick="expMode(\'count\')">Count</button>' +
      '</div></div>' +
    '<div class="xp-big"><b>' + fmtN(positive) + '</b><span>positive repl' + (positive === 1 ? 'y' : 'ies') + ' from ' + fmtN(mature) + ' mature prospects</span></div>' +
    '<div class="chart-wrap xp-chart" id="xp-weeks-chart"></div>' +
    '<div class="panel-foot xp-foot"><span class="xp-legend">' +
      '<span><i class="xp-sq a"></i>Arm A</span><span><i class="xp-sq b"></i>Arm B</span>' +
      '<span><i class="xp-sq p"></i>Window still open</span></span>' +
      '<span>Each prospect counts once, ' + r.window_days + ' days after their first email.</span></div></section>';
}

function expMode(mode) {
  if (_xp.mode === mode) return;
  _xp.mode = mode;
  document.querySelectorAll('#xp-mode button').forEach(b => {
    const on = b.textContent.toLowerCase() === mode;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on);
  });
  xpDrawWeeks();
}

function xpRoundTop(x, y, w, h) {
  const r = Math.min(3, w / 2, h);
  return 'M' + x.toFixed(1) + ',' + (y + h).toFixed(1) + 'V' + (y + r).toFixed(1) +
    'Q' + x.toFixed(1) + ',' + y.toFixed(1) + ' ' + (x + r).toFixed(1) + ',' + y.toFixed(1) +
    'H' + (x + w - r).toFixed(1) + 'Q' + (x + w).toFixed(1) + ',' + y.toFixed(1) + ' ' + (x + w).toFixed(1) + ',' + (y + r).toFixed(1) +
    'V' + (y + h).toFixed(1) + 'Z';
}

function xpDrawWeeks() {
  const wrap = document.getElementById('xp-weeks-chart');
  const d = _xp.detail;
  if (!wrap || !d) return;
  const W = wrap.clientWidth;
  if (!W) return;
  const weeks = d.results.by_week || [];
  if (!weeks.length) {
    wrap.innerHTML = '<div class="chart-note">' + icon('info') + '<span>Bars appear once the first emails go out.</span></div>';
    return;
  }
  const rate = _xp.mode === 'rate';
  const val = (w, k) => rate ? (w[k].positive_rate === null ? 0 : w[k].positive_rate * 100) : w[k].positive;
  const max = Math.max(0, ...weeks.flatMap(w => [val(w, 'A'), val(w, 'B')]));
  const { top, ticks } = niceTicks(rate ? Math.max(max, 1) : max, 4);
  const H = W < 520 ? 230 : 250;
  const padL = 14 + String(top).length * 7 + (rate ? 8 : 0), padR = 10, padT = 34, padB = 30;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const n = weeks.length, gw = (x1 - x0) / n;
  const bw = Math.max(8, Math.min(36, gw * 0.3)), gap = 4;
  const y = v => y1 - v / top * (y1 - y0);
  const xs = weeks.map((_, i) => x0 + gw * (i + 0.5));
  const narrow = gw < 96;
  let axis = yAxis(ticks, y, x0, x1);
  if (rate) axis = axis.replace(/(<text[^>]*>)([\d,]+)(<\/text>)/g, '$1$2%$3');
  let svg = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Positive replies by week of first email, arm A and arm B">' + axis;
  weeks.forEach((w, i) => {
    [['A', xs[i] - gap / 2 - bw], ['B', xs[i] + gap / 2]].forEach(([k, bx]) => {
      const open = w.open;
      let v = val(w, k);
      if (open && !w[k].mature) v = top * 0.14;            // nothing counted yet: a short placeholder
      const h = Math.max(0, y1 - y(v));
      if (h < 0.5) return;
      svg += '<path class="xp-bar ' + (open ? 'p' : k.toLowerCase()) + '" d="' + xpRoundTop(bx, y1 - h, bw, h) + '"/>';
    });
  });
  svg += '<line class="ch-base" x1="' + x0 + '" x2="' + x1 + '" y1="' + y1 + '" y2="' + y1 + '"/><g class="xp-wk">' +
    weeks.map((w, i) => '<text x="' + xs[i].toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle">' +
      svgEsc(narrow ? shortDay(w.week) : (w.open ? shortDay(w.week) + ' · open' : 'Week of ' + shortDay(w.week))) + '</text>').join('') +
    '</g><g class="ch-hover"></g></svg>';
  wrap.innerHTML = svg;
  chartHover(wrap, {
    xs, x0, x1, y0: y0 - 8, y1, band: gw * 0.86,
    html: i => {
      const w = weeks[i];
      const line = k => '<div class="tip-row"><span class="sw xp-sw ' + k.toLowerCase() + '"></span>Arm ' + k + '<b>' +
        (w[k].mature ? w[k].positive + ' of ' + w[k].mature + ' · ' + xpPct(w[k].positive_rate) : 'None mature yet') + '</b></div>';
      return '<div class="tip-date">Week of ' + svgEsc(shortDay(w.week)) + '</div>' + line('A') + line('B') +
        (w.open ? '<div class="tip-row sub">Window still open for ' + (w.A.pending + w.B.pending) + ' prospects</div>' : '');
    },
  });
}

// Difference, B minus A: the estimate and its 95% interval on a number line.

function xpDiffNote(r) {
  const iv = r.comparison.interval, code = r.decision.code;
  if (!iv) return 'Appears once both arms have mature prospects.';
  if (code === 'check_deliverability') return '95% interval. Check deliverability before reading this as a copy result.';
  if (iv.low <= 0 && iv.high >= 0) return '95% interval. It crosses zero, so this could still be no difference.';
  const above = iv.low > 0;
  if (code === 'b_ahead' || code === 'a_ahead') {
    return '95% interval. It stays ' + (above ? 'above' : 'below') + ' zero, so ' + (above ? 'B' : 'A') + ' is ahead.';
  }
  return '95% interval. It stays ' + (above ? 'above' : 'below') + ' zero so far, but Mercury waits for enough data before it names a winner.';
}

function xpDiffHtml(r) {
  const c = r.comparison;
  const metric = String(c.metric_label || 'positive reply rate').toLowerCase().replace(/-/g, ' ');
  return '<section class="panel xp-diff"><div class="xp-ch"><span class="xp-ct">' + icon('arrows-left-right') + 'Difference, B minus A</span>' +
      '<span class="xp-info" tabindex="0" role="img" aria-label="How the interval is worked out" data-tip="' + escAttr((c.method || '') + '. Shown in percentage points.') + '">' + icon('info') + '</span></div>' +
    '<div class="xp-big"><b>' + (c.difference === null ? '–' : xpPts(c.difference)) + '</b><span>' + escHtml(metric) + '</span></div>' +
    '<div class="chart-wrap xp-chart" id="xp-diff-chart"></div>' +
    '<div class="panel-foot xp-foot"><span>' + escHtml(xpDiffNote(r)) + '</span></div></section>';
}

function xpDrawDiff() {
  const wrap = document.getElementById('xp-diff-chart');
  const d = _xp.detail;
  if (!wrap || !d) return;
  const W = wrap.clientWidth;
  if (W < 120) return;                               // not laid out yet; the observer redraws
  const c = d.results.comparison;
  if (!c.interval || c.difference === null) {
    wrap.innerHTML = '<div class="chart-note">' + icon('info') + '<span>No interval yet. Both arms need at least one mature prospect.</span></div>';
    return;
  }
  const lo = c.interval.low * 100, hi = c.interval.high * 100, est = c.difference * 100;
  const span = Math.max(hi, 0) - Math.min(lo, 0);
  const step = [1, 2, 5, 10, 20, 25, 50, 100].find(s => span / s <= 5) || 100;
  let dmin = Math.floor(Math.min(lo, 0) / step) * step, dmax = Math.ceil(Math.max(hi, 0) / step) * step;
  if (lo - dmin < step * 0.1) dmin -= step;
  if (dmax - hi < step * 0.1) dmax += step;
  const H = 230, padL = 18, padR = 22, y0 = 14, y1 = H - 32, mid = (y0 + y1) / 2 + 6;
  const x0 = padL, x1 = W - padR;
  const x = v => x0 + (v - dmin) / (dmax - dmin) * (x1 - x0);
  const ticks = [];
  for (let v = dmin; v <= dmax + 1e-9; v += step) ticks.push(v);
  const label = v => v === 0 ? '0' : xpSigned(v, 0);
  const chipText = xpSigned(est) + ' (' + xpSigned(lo) + ' to ' + xpSigned(hi) + ')';
  const cw = Math.max(0, Math.min(chipText.length * 6.3 + 20, x1 - x0));
  const cx = Math.max(x0, Math.min(x(est) - cw / 2, x1 - cw));
  let svg = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' +
    escAttr('Difference in ' + (c.metric_label || 'rate') + ', B minus A: ' + chipText + ' points, 95% interval') + '">' +
    '<g class="ch-grid">' + ticks.map(v => '<line x1="' + x(v).toFixed(1) + '" x2="' + x(v).toFixed(1) + '" y1="' + y0 + '" y2="' + y1 + '"/>').join('') + '</g>' +
    '<line class="xp-zero" x1="' + x(0).toFixed(1) + '" x2="' + x(0).toFixed(1) + '" y1="' + y0 + '" y2="' + y1 + '"/>' +
    '<rect class="xp-band" x="' + x(lo).toFixed(1) + '" y="' + (mid - 12) + '" width="' + Math.max(2, x(hi) - x(lo)).toFixed(1) + '" height="24" rx="8"/>' +
    '<line class="xp-est" x1="' + x(est).toFixed(1) + '" x2="' + x(est).toFixed(1) + '" y1="' + (mid - 17) + '" y2="' + (mid + 17) + '"/>' +
    '<circle class="xp-dot" cx="' + x(est).toFixed(1) + '" cy="' + mid + '" r="4.5"/>' +
    '<rect class="xp-chip" x="' + cx.toFixed(1) + '" y="' + (mid - 52) + '" width="' + cw.toFixed(1) + '" height="24" rx="6"/>' +
    '<text class="xp-chip-t" x="' + (cx + cw / 2).toFixed(1) + '" y="' + (mid - 36) + '" text-anchor="middle">' + svgEsc(chipText) + '</text>' +
    '<text class="xp-nodiff" x="' + (x(0) + 6).toFixed(1) + '" y="' + (mid + 42) + '">no difference</text>' +
    '<g class="xp-ticks">' + ticks.map(v => '<text x="' + x(v).toFixed(1) + '" y="' + (H - 10) + '" text-anchor="middle">' + label(v) + '</text>').join('') + '</g></svg>';
  wrap.innerHTML = svg;
}

// Arms: what each arm does and how many people are in it.

function xpArmsHtml(e, a, b) {
  const card = (key, res, arm) =>
    '<div class="xp-arm"><div class="xp-arm-top"><span class="xp-akey"><i class="xp-sq ' + key.toLowerCase() + '"></i>Arm ' + key + '</span>' +
      '<b>' + escHtml(res.name || arm.name) + '</b>' +
      '<span class="xp-arm-n">' + fmtN(res.contacted) + ' contacted · ' + fmtN(res.pending) + ' pending · ' + fmtN(res.raw.replied) + ' replied</span></div>' +
    '<p>' + (arm.instruction ? escHtml(arm.instruction) : 'Same instruction as the other arm.') + ' ' +
      '<span class="xp-persona">Persona: ' + escHtml(arm.persona_name) + (arm.persona_revision ? ' v' + arm.persona_revision : '') + '</span></p></div>';
  return '<div class="xp-arms">' + card('A', a, e.arms[0]) + card('B', b, e.arms[1]) + '</div>';
}

// ── New experiment / edit draft: one drawer ──

function xpDefaultsModel() {
  const o = _xp.options || {}, df = o.defaults || {};
  const until = new Date(); until.setDate(until.getDate() + 14);
  return {
    id: '', version: null, name: '', hypothesis: '', variable: 'opening_angle',
    arms: [{ name: '', instruction: '', persona_id: '' }, { name: '', instruction: '', persona_id: '' }],
    persona: '', cohortKey: 'all', allocation_a: df.allocation_a || 50,
    until: ymd(until), window: df.response_window_days || 14, min: df.min_per_arm || 50,
    metric: df.primary_metric || 'positive_reply_rate', customCohort: null,
  };
}

function xpModelFrom(e) {
  const o = _xp.options || {};
  const m = xpDefaultsModel();
  m.id = e.id; m.version = e.version; m.name = e.name; m.hypothesis = e.hypothesis || '';
  m.variable = e.variable;
  m.arms = e.arms.map(a => ({ name: a.name || '', instruction: a.instruction || '', persona_id: a.persona_id || '' }));
  m.persona = e.arms[0].persona_id || '';
  m.allocation_a = e.allocation_a; m.until = (e.enroll_until || '').slice(0, 10);
  m.window = e.response_window_days; m.min = e.min_per_arm; m.metric = e.primary_metric;
  const same = (x, y) => JSON.stringify(x) === JSON.stringify(y);
  const hit = (o.cohorts || []).find(c => same(c.cohort, e.cohort));
  if (hit) m.cohortKey = hit.key;
  else { m.cohortKey = 'current'; m.customCohort = { label: e.cohort_description, cohort: e.cohort }; }
  return m;
}

function xpOptions(list, current) {
  return list.map(([value, label]) => '<option value="' + escAttr(value) + '"' + (String(value) === String(current) ? ' selected' : '') + '>' + escHtml(label) + '</option>').join('');
}

function xpFormHtml(m) {
  const o = _xp.options || {};
  const personas = (o.personas || [{ id: '', name: 'Default voice' }]).map(p =>
    [p.id, p.name + (p.revision ? ' v' + p.revision : '')]);
  const cohorts = (o.cohorts || []).map(c => [c.key, xpCap(c.label) + (c.count !== null && c.count !== undefined ? ' · ' + fmtN(c.count) + ' new' : '')]);
  if (m.customCohort) cohorts.push(['current', xpCap(m.customCohort.label)]);
  const splits = [10, 20, 30, 40, 50, 60, 70, 80, 90];
  if (!splits.includes(m.allocation_a)) splits.push(m.allocation_a);
  const windows = [7, 10, 14, 21, 28, 30, 45, 60, 90];
  if (!windows.includes(m.window)) windows.push(m.window);
  const metrics = (o.primary_metrics || [{ key: 'positive_reply_rate', label: 'Positive reply rate' }]).map(x => [x.key, x.label.replace(/-/g, ' ')]);
  const persona = m.variable === 'persona';
  const field = (id, label, control) => '<div class="form-group"><label class="form-label" for="' + id + '">' + label + '</label>' + control + '</div>';
  const sel = (id, opts, cur) => '<select class="form-input" id="' + id + '">' + xpOptions(opts, cur) + '</select>';
  const hints = m.variable === 'subject_line'
    ? ['Use a subject of three words or fewer.', 'Use a subject that names one detail of their business.']
    : ['Open with one fact about the business.', 'Open with one question about the business.'];
  const arm = (i, key) => {
    const a = m.arms[i], k = key.toLowerCase();
    return '<div class="xp-armform"><div class="xp-armform-head"><span class="xp-akey"><i class="xp-sq ' + k + '"></i>Arm ' + key + '</span>' +
      '<input class="form-input" id="xf-' + k + '-name" maxlength="60" placeholder="' + (i ? 'Name this version' : 'Name the current approach') + '" value="' + escAttr(a.name) + '" aria-label="Arm ' + key + ' name"></div>' +
      (persona
        ? field('xf-' + k + '-persona', 'Persona', sel('xf-' + k + '-persona', personas, a.persona_id))
        : field('xf-' + k + '-ins', 'Instruction', '<textarea class="form-input" id="xf-' + k + '-ins" rows="2" maxlength="2000" placeholder="' + escAttr(hints[i]) + '">' + escHtml(a.instruction) + '</textarea>')) +
      '</div>';
  };
  const seg = [['opening_angle', 'Opening angle'], ['subject_line', 'Subject line'], ['persona', 'Persona']];
  return '<form class="xp-form" id="xf" onsubmit="return false" autocomplete="off"><div class="xp-form-body">' +
    '<div class="drawer-section"><h4>What you are testing</h4>' +
      field('xf-name', 'Name', '<input class="form-input" id="xf-name" maxlength="80" placeholder="Observation opener vs question opener" value="' + escAttr(m.name) + '">') +
      field('xf-hyp', 'Hypothesis', '<textarea class="form-input" id="xf-hyp" rows="2" maxlength="1000" placeholder="What you expect to happen, and why">' + escHtml(m.hypothesis) + '</textarea>') +
      '<div class="form-group"><span class="form-label" id="xf-var-l">The one thing that changes</span>' +
        '<div class="segmented" role="radiogroup" aria-labelledby="xf-var-l">' + seg.map(([v, l]) =>
          '<button type="button" role="radio" aria-checked="' + (m.variable === v) + '" class="' + (m.variable === v ? 'on' : '') + '" onclick="xpPickVariable(\'' + v + '\')">' + l + '</button>').join('') + '</div></div></div>' +
    '<div class="drawer-section"><h4>Arms</h4>' +
      (persona ? '' : field('xf-persona', 'Persona, the same in both arms', sel('xf-persona', personas, m.persona))) +
      arm(0, 'A') + arm(1, 'B') + '</div>' +
    '<div class="drawer-section"><h4>Who and how long</h4>' +
      field('xf-cohort', 'Eligible prospects', sel('xf-cohort', cohorts, m.cohortKey)) +
      '<div class="form-row">' +
        field('xf-split', 'Split', sel('xf-split', splits.sort((p, q) => p - q).map(v => [v, v + ' / ' + (100 - v)]), m.allocation_a)) +
        field('xf-until', 'Enroll until', '<input class="form-input" id="xf-until" type="date" value="' + escAttr(m.until) + '">') + '</div>' +
      '<div class="form-row">' +
        field('xf-window', 'Count replies for', sel('xf-window', windows.sort((p, q) => p - q).map(v => [v, v + ' days']), m.window)) +
        field('xf-min', 'Minimum per arm', '<input class="form-input" id="xf-min" type="number" min="1" max="100000" step="1" value="' + escAttr(m.min) + '">') + '</div>' +
      field('xf-metric', 'Decide on', sel('xf-metric', metrics, m.metric)) + '</div>' +
    '<p class="xp-freeze">Once the first prospect is enrolled the arms are frozen. Changing an instruction or persona starts a new revision, so past results keep the version that produced them.</p>' +
    '<div id="xf-preview"></div></div>' +
    '<div class="xp-form-foot"><p class="form-error" id="xf-error" role="alert" hidden></p><div class="xp-foot-btns">' +
      '<button type="button" class="btn btn-primary btn-sm" id="xf-start" onclick="expSubmit(true)">Start enrolling</button>' +
      '<button type="button" class="btn btn-secondary btn-sm" id="xf-preview-btn" onclick="expPreview()">Preview both arms</button>' +
      '<span class="xp-spacer"></span>' +
      '<button type="button" class="btn btn-secondary btn-sm" id="xf-save" onclick="expSubmit(false)">Save as draft</button></div></div></form>';
}

function xpShowForm(m, fresh) {
  _xp.form = m;
  const kicker = m.id ? 'Edit draft' : 'New experiment';
  const title = m.id ? (m.name || 'Untitled experiment') : 'Two versions, one change';
  const sub = 'Pick one thing to change. Everything else stays the same in both arms.';
  const html = xpFormHtml(m);
  if (fresh) openDrawer(kicker, title, sub, html); else updateDrawer(kicker, title, sub, html);
  if (fresh) { const n = document.getElementById('xf-name'); if (n) n.focus({ preventScroll: true }); }
}

function expNew() {
  if (!_xp.options) { loadExperiments().then(() => xpShowForm(xpDefaultsModel(), true)); return; }
  xpShowForm(xpDefaultsModel(), true);
}

function expEdit() {
  const d = _xp.detail;
  if (!d || d.experiment.status !== 'draft') return;
  xpShowForm(xpModelFrom(d.experiment), true);
}

// Read the form into a model, so a re-render (variable change) keeps what was typed.
function xpReadForm() {
  const m = Object.assign({}, _xp.form);
  const v = id => { const el = document.getElementById(id); return el ? el.value : null; };
  const persona = m.variable === 'persona';
  m.name = v('xf-name') ?? m.name; m.hypothesis = v('xf-hyp') ?? m.hypothesis;
  m.arms = ['a', 'b'].map((k, i) => ({
    name: v('xf-' + k + '-name') ?? m.arms[i].name,
    instruction: persona ? m.arms[i].instruction : (v('xf-' + k + '-ins') ?? m.arms[i].instruction),
    persona_id: persona ? (v('xf-' + k + '-persona') ?? m.arms[i].persona_id) : m.arms[i].persona_id,
  }));
  if (!persona) m.persona = v('xf-persona') ?? m.persona;
  m.cohortKey = v('xf-cohort') ?? m.cohortKey;
  m.allocation_a = Number(v('xf-split') ?? m.allocation_a);
  m.until = v('xf-until') ?? m.until;
  m.window = Number(v('xf-window') ?? m.window);
  m.min = Number(v('xf-min') ?? m.min);
  m.metric = v('xf-metric') ?? m.metric;
  return m;
}

function xpPickVariable(variable) {
  const m = xpReadForm();
  if (m.variable === variable) return;
  m.variable = variable;
  xpShowForm(m, false);
}

function xpPayload(m) {
  const shared = m.variable !== 'persona';
  const cohort = m.cohortKey === 'current' ? (m.customCohort || {}).cohort
    : ((((_xp.options || {}).cohorts || []).find(c => c.key === m.cohortKey)) || { cohort: {} }).cohort;
  return {
    name: m.name.trim(), hypothesis: m.hypothesis.trim(), variable: m.variable,
    arms: m.arms.map((a, i) => ({
      key: 'AB'[i], name: a.name.trim(),
      instruction: shared ? a.instruction.trim() : '',
      persona_id: shared ? m.persona : a.persona_id,
    })),
    cohort: cohort || {}, allocation_a: m.allocation_a, enroll_until: m.until || null,
    response_window_days: m.window, min_per_arm: m.min, primary_metric: m.metric,
  };
}

function xpFormError(message) {
  const el = document.getElementById('xf-error');
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || '';
}

function xpFormBusy(on) {
  ['xf-start', 'xf-save', 'xf-preview-btn'].forEach(id => { const b = document.getElementById(id); if (b) b.disabled = on; });
}

async function expPreview() {
  const m = xpReadForm();
  _xp.form = m;
  xpFormError('');
  xpFormBusy(true);
  const res = await postJSON('/api/experiments/preview', { definition: xpPayload(m) });
  xpFormBusy(false);
  const el = document.getElementById('xf-preview');
  if (!el) return;
  if (!res.ok) { xpFormError(res.error); return; }
  const p = res.data;
  el.innerHTML = '<div class="drawer-section xp-prev"><h4>Preview</h4>' +
    '<p class="xp-prev-line"><b class="num">' + fmtN(p.eligible) + '</b> prospects are eligible now. Expected split: ' +
      'A <b class="num">' + fmtN(p.expected.A) + '</b> · B <b class="num">' + fmtN(p.expected.B) + '</b>.</p>' +
    (p.warnings || []).map(w => '<div class="ev-note">' + icon('warning-circle') + '<span>' + escHtml(w.message) + '</span></div>').join('') +
    p.arms.map(a => '<div class="xp-prev-arm"><div class="xp-prev-head"><span class="xp-akey"><i class="xp-sq ' + a.key.toLowerCase() + '"></i>Arm ' + a.key + '</span><b>' + escHtml(a.name) + '</b>' +
      '<span class="muted">' + escHtml(a.persona.name) + ' v' + a.persona.revision + '</span></div>' +
      '<pre class="xp-pre">' + escHtml((a.voice_block || '').trim()) + '</pre></div>').join('') +
    '<p class="drawer-note">' + escHtml(p.note || '') + '</p></div>';
  el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function expSubmit(start) {
  const m = xpReadForm();
  _xp.form = m;
  xpFormError('');
  xpFormBusy(true);
  const payload = xpPayload(m);
  const res = m.id
    ? await xpJson('PATCH', '/api/experiments/' + encodeURIComponent(m.id), Object.assign({ expected_version: m.version }, payload))
    : await postJSON('/api/experiments', payload);
  if (!res.ok) { xpFormBusy(false); xpFormError(res.error); return; }
  const id = res.data.experiment.id;
  let started = true;
  if (start) {
    const go = await postJSON('/api/experiments/' + encodeURIComponent(id) + '/start');
    if (!go.ok) { started = false; showToast('Saved as a draft, but could not start: ' + go.error, 'error'); }
  }
  xpFormBusy(false);
  if (started) showToast(start ? 'Experiment started. Enrollment is open.' : 'Draft saved.', 'success');
  closeDrawer();
  _xp.sel = id;
  loadExperiments();
}

async function xpJson(method, path, body) {
  try {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let data = null;
    try { data = await r.json(); } catch { /* empty body */ }
    const ok = r.ok && !(data && data.success === false);
    return { ok, status: r.status, data, error: ok ? '' : ((data && (data.message || data.detail)) || 'request failed (' + r.status + ')') };
  } catch {
    return { ok: false, status: 0, data: null, error: 'can\'t reach the dashboard server' };
  }
}
