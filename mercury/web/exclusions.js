/* Exclusions: addresses and domains Mercury never emails, company holds,
   and the emails they stopped. Uses the shared helpers in app.js. */

const _ex = { kind: 'email', q: '', view: 'active', rules: [], timer: null };

const POLICY_LABEL = {
  excluded: 'Excluded', blocked: 'Blocked', company_hold: 'Company on hold',
  company_daily_limit: 'Company limit', company_active_limit: 'Company limit',
  company_unknown: 'Company unknown', ooo_pause: 'Out of office',
};

// A queued email's policy verdict as a badge plus its reason, or '' when it
// can go out. 'company_unknown' is informational, so it is quiet by default.
function policyNote(p, withUnknown) {
  if (!p || p.code === 'ok' || (p.code === 'company_unknown' && !withUnknown)) return '';
  return '<span class="ex-why">' + toneBadge(p.tone, POLICY_LABEL[p.code] || 'Held') +
    '<span>' + escHtml(p.reason) + (p.action ? ' ' + escHtml(p.action) : '') + '</span></span>';
}

async function loadExclusions() {
  const q = encodeURIComponent(_ex.q);
  const [rules, holds, outbox] = await Promise.all([
    api('/api/exclusions?q=' + q + (_ex.view === 'lifted' ? '&removed=true' : '')),
    api('/api/company-holds'),
    api('/api/outbox'),
  ]);
  const listEl = document.getElementById('ex-rules');
  if (!rules) { listEl.innerHTML = '<div class="card">' + offlineState() + '</div>'; return; }
  _ex.rules = rules.rules || [];
  renderExPolicy(rules.policy || {});
  renderExHolds((holds && holds.holds) || []);
  const blocked = (outbox && outbox.blocked) || [];
  renderExBlocked(blocked);
  navCount('nav-exclusions', blocked.length);
  renderExRules();
}

function renderExPolicy(p) {
  const el = document.getElementById('ex-policy');
  const limit = n => n
    ? '<span class="kpi-value tight">' + n + '</span>'
    : '<span class="kpi-value tight zero">0<small>no limit</small></span>';
  const kpi = (label, value, foot) =>
    '<div class="kpi static"><span class="kpi-label">' + label + '</span>' + value +
    '<span class="kpi-foot">' + foot + '</span></div>';
  const cap = p.capability || {};
  el.innerHTML = '<div class="kpis kpis-3">' +
    kpi('New contacts per company, per day', limit(p.max_new_contacts_per_company_per_day),
      'First emails in a rolling 24 hours') +
    kpi('Contacts in a sequence, per company', limit(p.max_active_contacts_per_company),
      'A paused sequence keeps its place') +
    kpi('Hold a company when someone replies',
      '<span class="kpi-value tight' + (p.pause_company_on_reply ? '' : ' zero') + '">' +
        (p.pause_company_on_reply ? 'On' : 'Off') + '</span>',
      'Auto-replies and bounces never count') +
    '</div>' +
    (cap.note ? '<section class="panel"><div class="panel-body ex-note">' + icon('info') +
      '<span>' + escHtml(cap.note) + '</span></div></section>' : '');
}

function renderExHolds(holds) {
  const el = document.getElementById('ex-holds');
  if (!holds.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="card"><h2>Companies on hold</h2>' +
    '<p class="lede">Cold mail to their colleagues waits until you resume. Replies to people who wrote back still go.</p>' +
    '<div class="table-card"><table><thead><tr><th>Company</th><th>Why</th><th>Since</th>' +
    '<th class="num">Queued</th><th></th></tr></thead><tbody>' +
    holds.map(h => '<tr><td>' + escHtml(h.company_name || h.company_domain || h.company_id) + '</td>' +
      '<td>' + toneBadge('waiting', 'On hold') + ' <span class="muted">' + escHtml(h.reason_text) + '</span></td>' +
      '<td class="muted">' + formatDate(h.created_at) + '</td>' +
      '<td class="num">' + h.queued + '</td>' +
      '<td class="ex-act"><button class="btn btn-secondary btn-sm" data-id="' + escAttr(h.id) + '" data-name="' +
        escAttr(h.company_name || h.company_domain || 'this company') + '" onclick="exResume(this)">Resume</button></td></tr>').join('') +
    '</tbody></table></div></div>';
}

function renderExBlocked(items) {
  const el = document.getElementById('ex-blocked');
  if (!items.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="card"><h2>Blocked emails</h2>' +
    '<p class="lede">Queued before their address or domain was excluded. Lifting a rule does not send them: ' +
    'send one back to review and decide again.</p>' +
    '<div class="table-card"><table><thead><tr><th>To</th><th>Subject</th><th>Why</th><th></th></tr></thead><tbody>' +
    items.map(r => '<tr><td>' + escHtml(r.to_email) + '</td><td>' + escHtml(r.subject) + '</td>' +
      '<td>' + policyNote(r.policy) + '</td>' +
      '<td class="ex-act"><button class="btn btn-secondary btn-sm" onclick="exRequeue(\'' + escAttr(r.id) +
        '\')">Send back to review</button> <button class="btn btn-secondary btn-sm" onclick="exDiscard(\'' +
        escAttr(r.id) + '\',' + Number(r.revision) + ')">Discard</button></td></tr>').join('') +
    '</tbody></table></div></div>';
}

function renderExRules() {
  const el = document.getElementById('ex-rules');
  document.querySelectorAll('#ex-view button').forEach(b => {
    const on = b.dataset.v === _ex.view;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', on);
  });
  const rules = _ex.rules;
  if (!rules.length) {
    el.innerHTML = '<div class="card">' + (_ex.q
      ? emptyState('magnifying-glass', 'No match', 'No exclusion matches that search.')
      : _ex.view === 'lifted'
        ? emptyState('clock-counter-clockwise', 'Nothing lifted yet', 'Rules you remove stay here with who lifted them and why.')
        : emptyState('prohibit', 'No exclusions yet',
            'Opt-outs and bounces land here on their own. Add customers, partners or competitors you never want emailed.')) +
      '</div>';
    return;
  }
  const lifted = _ex.view === 'lifted';
  el.innerHTML = '<div class="table-card"><table><thead><tr><th>Rule</th><th>Source</th><th>Reason</th>' +
    '<th>' + (lifted ? 'Lifted' : 'Added') + '</th><th></th></tr></thead><tbody>' +
    rules.map((r, i) => '<tr class="ex-row" onclick="exOpen(' + i + ')">' +
      '<td><span class="mono">' + escHtml(r.value) + '</span>' +
        (r.include_subdomains ? ' <span class="muted">and subdomains</span>' : '') + '</td>' +
      '<td>' + (r.protected ? toneBadge('bad', 'Opted out') : escHtml(capFirst(r.label))) + '</td>' +
      '<td class="muted">' + escHtml(lifted ? (r.removed_note || '') : (r.reason || '')) + '</td>' +
      '<td class="muted">' + formatDate(lifted ? r.removed_at : r.created_at) + '</td>' +
      '<td class="ex-act"><button class="link-btn" onclick="event.stopPropagation();exOpen(' + i + ')">Details</button></td>' +
      '</tr>').join('') +
    '</tbody></table></div>';
}

function capFirst(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }

function exSearch(v) {
  _ex.q = (v || '').trim();
  clearTimeout(_ex.timer);
  _ex.timer = setTimeout(loadExclusions, 200);
}

function exSetView(v) { _ex.view = v; loadExclusions(); }

function exSetKind(k) {
  _ex.kind = k;
  document.querySelectorAll('#ex-kind button').forEach(b => {
    const on = b.dataset.k === k;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', on);
  });
  const value = document.getElementById('ex-value');
  value.placeholder = k === 'email' ? 'jane@acme.com' : 'acme.com';
  document.getElementById('ex-sub-wrap').hidden = k !== 'domain';
}

async function exAdd(ev) {
  if (ev) ev.preventDefault();
  const value = document.getElementById('ex-value').value.trim();
  if (!value) { showToast('Type an address or a domain.', 'error'); return; }
  const res = await impSend('/api/exclusions', {
    kind: _ex.kind, value,
    reason: document.getElementById('ex-reason').value.trim(),
    include_subdomains: _ex.kind === 'domain' && document.getElementById('ex-subdomains').checked,
  });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  const r = res.data;
  showToast((r.created ? 'Excluded ' : 'Already excluded: ') + r.description +
    (r.blocked ? '. ' + r.blocked + ' queued email' + (r.blocked === 1 ? ' is' : 's are') + ' now blocked.' : '.'),
    'success');
  document.getElementById('ex-value').value = '';
  document.getElementById('ex-reason').value = '';
  loadExclusions();
}

async function exOpen(i) {
  const rule = _ex.rules[i];
  if (!rule) return;
  const res = await impSend('/api/exclusions/' + encodeURIComponent(rule.id));
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  const r = res.data;
  const events = (r.events || []).map(e =>
    '<div><dt>' + formatDate(e.created_at) + '</dt><dd>' + escHtml(capFirst(e.action)) +
      (e.actor ? ' by ' + escHtml(e.actor) : '') + (e.note ? '<div class="muted">' + escHtml(e.note) + '</div>' : '') +
    '</dd></div>').join('');
  let remove = '';
  if (!r.removed_at) {
    remove = '<div class="drawer-section"><h4>' + (r.protected ? 'Lift this opt-out' : 'Lift this rule') + '</h4>' +
      (r.protected
        ? '<p class="drawer-note">This person asked not to be contacted. Lift it only if they asked to hear from you again.</p>'
        : '<p class="drawer-note">Mail it blocked stays blocked until you send it back to review. Other rules for the same address stay.</p>') +
      '<div class="form-group" style="margin-top:12px"><label class="form-label" for="ex-note">Why' +
        (r.protected ? '' : ' (optional)') + '</label>' +
        '<input class="form-input" id="ex-note" autocomplete="off" maxlength="300" placeholder="' +
        (r.protected ? 'They wrote asking to hear from us' : 'Added by mistake') + '"></div>' +
      (r.protected ? '<label class="check ex-check"><input type="checkbox" id="ex-confirm">They asked to hear from us again</label>' : '') +
      '<div class="drawer-actions"><button class="btn btn-secondary" onclick="exRemove(\'' + escAttr(r.id) + '\',' +
        (r.protected ? 'true' : 'false') + ')">' + icon('trash') + (r.protected ? 'Lift opt-out' : 'Remove rule') + '</button></div></div>';
  }
  openDrawer(r.kind === 'email' ? 'Email exclusion' : 'Domain exclusion', r.value,
    r.removed_at ? toneBadge('idle', 'Lifted') : (r.protected ? toneBadge('bad', 'Opted out') : toneBadge('active', 'Active')),
    facts([
      ['Matches', escHtml(r.description)],
      ['Source', escHtml(capFirst(r.label))],
      ['Reason', escHtml(r.reason || '—')],
      ['Added', formatDate(r.created_at) + (r.created_by ? ' by ' + escHtml(r.created_by) : '')],
    ].concat(r.removed_at ? [['Lifted', formatDate(r.removed_at) + (r.removed_by ? ' by ' + escHtml(r.removed_by) : '')]] : [])) +
    '<div class="drawer-section"><h4>History</h4><dl class="facts">' + events + '</dl></div>' + remove);
}

async function exRemove(id, isOptOut) {
  const note = (document.getElementById('ex-note') || {}).value || '';
  const confirmed = !!(document.getElementById('ex-confirm') || {}).checked;
  if (isOptOut && (!note.trim() || !confirmed)) {
    showToast('Write why, and tick that they asked to hear from you again.', 'error');
    return;
  }
  const res = await impSend('/api/exclusions/' + encodeURIComponent(id) + '/remove',
    { note: note.trim(), confirm_opt_out: confirmed });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  const still = res.data.still_excluded_by || [];
  showToast(still.length ? 'Lifted. Still excluded by: ' + still.map(s => s.description).join('; ') + '.'
                         : 'Lifted.', 'success');
  closeDrawer();
  loadExclusions();
}

async function exRequeue(id) {
  const res = await impSend('/api/outbox/' + encodeURIComponent(id) + '/requeue', {});
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  showToast('Back in review. It waits for your approval in the Outbox.', 'success');
  loadExclusions();
}

async function exDiscard(id, revision) {
  const ok = await confirmModal({
    title: 'Discard this blocked email?',
    copy: 'This email and any remaining steps in its sequence are rejected. The exclusion stays in place.',
    ok: 'Discard' });
  if (!ok) return;
  const res = await impSend('/api/outbox/' + encodeURIComponent(id) + '/reject', {revision});
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  showToast(res.data.rejected ? 'Discarded. The exclusion stays in place.' : 'This email was already handled.', 'success');
  if (currentTab === 'outbox') loadOutbox();
  else loadExclusions();
}

async function exResume(btn) {
  const id = btn.dataset.id, name = btn.dataset.name;
  const ok = await confirmModal({
    title: 'Resume cold mail to ' + name + '?',
    copy: 'Approved emails to their colleagues go out on schedule again. Drafts still wait for your review.',
    ok: 'Resume' });
  if (!ok) return;
  const res = await impSend('/api/company-holds/' + encodeURIComponent(id) + '/release',
    { note: 'Resumed from the dashboard' });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  showToast('Resumed.', 'success');
  if (currentTab === 'exclusions') loadExclusions();
}

async function exHoldCompany(btn) {
  const companyId = btn.dataset.id, name = btn.dataset.name;
  const ok = await confirmModal({
    title: 'Pause cold mail to ' + name + '?',
    copy: 'Nothing new goes to anyone there until you resume it in Exclusions. Replies to people who wrote back still go.',
    ok: 'Pause' });
  if (!ok) return;
  const res = await impSend('/api/company-holds', { company_id: companyId, note: '' });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  showToast(res.data.created ? 'Paused. Resume it in Exclusions.' : 'Already on hold.', 'success');
}

async function exImport(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  const buf = new Uint8Array(await file.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  const res = await impSend('/api/exclusions/import', { content_b64: btoa(bin), reason: 'Imported from ' + file.name.slice(0, 120) });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  const d = res.data;
  showToast('Added ' + d.added + '. ' + d.already_excluded + ' already excluded' +
    (d.invalid_count ? ', ' + d.invalid_count + ' invalid.' : '.'), d.invalid_count ? 'error' : 'success');
  loadExclusions();
}
