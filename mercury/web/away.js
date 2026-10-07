/* Away: contacts whose cold sequence is paused by an out-of-office reply.
   Shown in the Outbox. Uses the shared helpers in app.js. */

const _away = { pauses: [], timezone: 'UTC' };

const AUTO_KIND_LABEL = {
  out_of_office: 'Out of office', receipt: 'Read or delivery receipt',
  acknowledgement: 'Automatic acknowledgement',
};

// "2026-10-20" (a local calendar day) as "Tue, Oct 20", never shifted by the
// browser's own timezone.
function awayDay(ymdStr) {
  if (!ymdStr) return '';
  const [y, m, d] = ymdStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  return dt.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric',
    year: y === new Date().getFullYear() ? undefined : 'numeric'});
}

function awayBadge(p) {
  return p.review_state === 'scheduled'
    ? toneBadge('waiting', 'Away')
    : toneBadge('note', 'Needs a return date');
}

function awayName(p) { return p.prospect_name || p.prospect_email || 'This contact'; }

async function loadAway() {
  const el = document.getElementById('outbox-away');
  if (!el) return;
  const data = await api('/api/pauses');
  if (!data) { el.innerHTML = ''; return; }
  _away.pauses = data.pauses || [];
  _away.timezone = data.timezone || 'UTC';
  const cap = data.capability || {};
  if (!_away.pauses.length) {
    el.innerHTML = cap.note
      ? '<section class="panel"><div class="panel-body ex-note">' + icon('info') +
          '<span>' + escHtml(cap.note) + '</span></div></section>'
      : '';
    return;
  }
  el.innerHTML = '<div class="card"><h2>Away</h2>' +
    '<p class="lede">They sent an out-of-office reply, so their follow-ups wait. On the day they are back ' +
    'the next step goes out under the usual caps and pacing, and later steps keep their gaps. ' +
    'Mercury never guesses a date it cannot read.</p>' +
    '<div class="table-card"><table><thead><tr><th>Contact</th><th>Back</th><th>State</th>' +
    '<th>From their reply</th><th class="num">Queued</th><th></th></tr></thead><tbody>' +
    _away.pauses.map((p, i) => '<tr class="ex-row" onclick="awayOpen(' + i + ')">' +
      '<td>' + escHtml(awayName(p)) +
        (p.prospect_name ? '<div class="muted">' + escHtml(p.prospect_email) + '</div>' : '') + '</td>' +
      '<td>' + (p.back_on ? escHtml(awayDay(p.back_on)) : '<span class="muted">Not set</span>') + '</td>' +
      '<td>' + awayBadge(p) + (p.review_text
        ? '<div class="muted">' + escHtml(p.review_text) + '</div>' : '') + '</td>' +
      '<td class="muted">' + (p.return_text ? '"' + escHtml(p.return_text) + '"' : 'No date given') + '</td>' +
      '<td class="num">' + Number(p.queued || 0) + '</td>' +
      '<td class="ex-act"><button class="link-btn" onclick="event.stopPropagation();awayOpen(' + i +
        ')">' + (p.review_state === 'scheduled' ? 'Details' : 'Set date') + '</button></td>' +
    '</tr>').join('') +
    '</tbody></table></div></div>';
}

async function awayOpen(i) {
  const p = _away.pauses[i];
  if (!p) return;
  const res = await impSend('/api/pauses/' + encodeURIComponent(p.id));
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  const d = res.data;
  const tz = d.display_timezone || _away.timezone;
  const today = ymd(new Date());
  const messages = (d.messages || []).map(m =>
    '<div><dt>' + formatDate(m.received_at) + '</dt><dd>' +
      escHtml(AUTO_KIND_LABEL[m.kind] || m.kind) +
      (m.excerpt ? '<div class="muted">' + escHtml(m.excerpt.slice(0, 280)) + '</div>' : '') +
    '</dd></div>').join('');
  const scheduled = d.review_state === 'scheduled';
  openDrawer('Out of office', awayName(d), awayBadge(d),
    facts([
      ['Email', escHtml(d.prospect_email || '')],
      ['Company', escHtml(d.company || 'Not known')],
      ['Back', scheduled && d.back_on
        ? escHtml(awayDay(d.back_on)) + ' <span class="muted">at 9:00, ' + escHtml(tz) + '</span>'
        : '<span class="muted">' + escHtml(d.review_text || 'Not set') + '</span>'],
      ['Their reply said', d.return_text ? '"' + escHtml(d.return_text) + '"' : 'No date'],
      ['Set by', d.manual_override ? 'You' + (d.override_by ? ' (' + escHtml(d.override_by) + ')' : '')
        : 'Read from their reply' + (scheduled ? ' <span class="num">' + Math.round((d.confidence || 0) * 100) +
          '%</span> sure' : '')],
      ['Paused since', formatDate(d.created_at)],
      ['Queued follow-ups', '<span class="num">' + Number(d.queued || 0) + '</span>'],
    ]) +
    '<div class="drawer-section"><h4>' + (scheduled ? 'Correct the return date' : 'Set the return date') + '</h4>' +
      '<p class="drawer-note">Their sequence picks up that day at 9:00 in ' + escHtml(tz) +
        '. Approvals stay as they are: a draft still waits for your review.</p>' +
      '<div class="form-group" style="margin-top:12px"><label class="form-label" for="away-date">Back on</label>' +
        '<input class="form-input" type="date" id="away-date" min="' + today + '" value="' +
          escAttr(d.back_on || '') + '"></div>' +
      '<div class="form-group"><label class="form-label" for="away-note">Why (optional)</label>' +
        '<input class="form-input" id="away-note" autocomplete="off" maxlength="300" ' +
          'placeholder="They wrote back with a new date"></div>' +
      '<div class="drawer-actions"><button class="btn btn-primary" onclick="awaySetDate(\'' +
        escAttr(d.id) + '\')">Save date</button></div></div>' +
    '<div class="drawer-section"><h4>Resume now</h4>' +
      '<p class="drawer-note">The next follow-up becomes due right away and goes out under the usual caps, ' +
        'pacing and quiet hours. Later steps keep their gaps.</p>' +
      '<div class="drawer-actions"><button class="btn btn-secondary" data-id="' + escAttr(d.id) +
        '" data-name="' + escAttr(awayName(d)) + '" onclick="awayResume(this)">' + icon('play') +
        'Resume now</button></div></div>' +
    (messages ? '<div class="drawer-section"><h4>Automatic replies</h4><dl class="facts">' +
      messages + '</dl></div>' : ''));
}

async function awaySetDate(id) {
  const day = (document.getElementById('away-date') || {}).value || '';
  if (!day) { showToast('Pick the day they are back.', 'error'); return; }
  const note = ((document.getElementById('away-note') || {}).value || '').trim();
  const res = await impSend('/api/pauses/' + encodeURIComponent(id) + '/return-date', { date: day, note });
  if (!res.ok) { showToast(res.error.message, 'error'); return; }
  showToast('Saved. Their sequence picks up ' + awayDay(res.data.back_on) + '.', 'success');
  closeDrawer();
  loadAway();
}

async function awayResume(btn) {
  const ok = await confirmModal({
    title: 'Resume ' + btn.dataset.name + ' now?',
    copy: 'Their next follow-up becomes due now. Approved emails go out on the next send; drafts still wait for your review.',
    ok: 'Resume' });
  if (!ok) return;
  const res = await impSend('/api/pauses/' + encodeURIComponent(btn.dataset.id) + '/resume',
    { note: 'Resumed from the dashboard' });
  if (!res.ok) { showToast(res.error.message, 'error'); closeDrawer(); loadAway(); return; }
  showToast('Resumed.', 'success');
  closeDrawer();
  if (currentTab === 'outbox') loadOutbox();
}
