/* Outbox: every outgoing email waits here for a yes.

   Four views. To review is a desk of one-at-a-time decisions: the queue,
   the email itself (editable in place), and why Mercury wrote it. Showing
   every draft stacked invites an "approve all" reflex, which is exactly the
   review the approval ladder exists to prevent, so one email fills the
   pane. Scheduled, Sent today and Didn't send are tables.

   Uses the shared helpers in app.js (api, postJSON, icon, toneBadge,
   regeneratePrompt, openDrawer, demoOpen, fromLabel, ...). */

// Phosphor glyphs only this screen uses, from the same @phosphor-icons/core
// 2.1.1 set as icons.js.
Object.assign(PH, {
  "checks": "<path d=\"M149.61,85.71l-89.6,88a8,8,0,0,1-11.22,0L10.39,136a8,8,0,1,1,11.22-11.41L54.4,156.79l84-82.5a8,8,0,1,1,11.22,11.42Zm96.1-11.32a8,8,0,0,0-11.32-.1l-84,82.5-18.83-18.5a8,8,0,0,0-11.21,11.42l24.43,24a8,8,0,0,0,11.22,0l89.6-88A8,8,0,0,0,245.71,74.39Z\"/>",
  "tag": "<path d=\"M243.31,136,144,36.69A15.86,15.86,0,0,0,132.69,32H40a8,8,0,0,0-8,8v92.69A15.86,15.86,0,0,0,36.69,144L136,243.31a16,16,0,0,0,22.63,0l84.68-84.68a16,16,0,0,0,0-22.63Zm-96,96L48,132.69V48h84.69L232,147.31ZM96,84A12,12,0,1,1,84,72,12,12,0,0,1,96,84Z\"/>",
  "list-checks": "<path d=\"M224,128a8,8,0,0,1-8,8H128a8,8,0,0,1,0-16h88A8,8,0,0,1,224,128ZM128,72h88a8,8,0,0,0,0-16H128a8,8,0,0,0,0,16Zm88,112H128a8,8,0,0,0,0,16h88a8,8,0,0,0,0-16ZM82.34,42.34,56,68.69,45.66,58.34A8,8,0,0,0,34.34,69.66l16,16a8,8,0,0,0,11.32,0l32-32A8,8,0,0,0,82.34,42.34Zm0,64L56,132.69,45.66,122.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32,0l32-32a8,8,0,0,0-11.32-11.32Zm0,64L56,196.69,45.66,186.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32,0l32-32a8,8,0,0,0-11.32-11.32Z\"/>",
  "chat-centered-text": "<path d=\"M216,40H40A16,16,0,0,0,24,56V184a16,16,0,0,0,16,16h60.43l13.68,23.94a16,16,0,0,0,27.78,0L155.57,200H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40Zm0,144H155.57a16,16,0,0,0-13.89,8.06L128,216l-13.68-23.94A16,16,0,0,0,100.43,184H40V56H216Zm-56-72a8,8,0,0,1-8,8H104a8,8,0,0,1,0-16h48A8,8,0,0,1,160,112Zm32-32a8,8,0,0,1-8,8H72a8,8,0,0,1,0-16H184A8,8,0,0,1,192,80Zm-32,64a8,8,0,0,1-8,8H104a8,8,0,0,1,0-16h48A8,8,0,0,1,160,144Z\"/>",
});

const _ob = {
  view: 'review',      // review | scheduled | sent | failed
  data: null,          // last /api/outbox payload
  mb: null, send: null,
  items: [],           // To review, in queue order: flagged first, then ready
  i: 0,
  focusId: '',         // keep this email selected across a reload
  edits: {},           // id -> {subject, body}: unsaved text, kept across re-renders
  stale: {},           // id -> true: saving failed because the email changed
  busy: '',            // id of the email an action is running on
  mobileOpen: false,   // phone: one email full screen
};

const OB_VIEWS = [
  ['review', 'To review', 'Review'], ['scheduled', 'Scheduled', 'Scheduled'],
  ['sent', 'Sent today', 'Sent'], ['failed', 'Didn’t send', 'Didn’t send'],
];
const obPhone = window.matchMedia('(max-width: 700px)');
const obEsc = s => escHtml(String(s ?? ''));
const obNeedsLook = it => !!it.needs_flag_approval;
const obCur = () => _ob.items[_ob.i] || null;
const obFind = id => _ob.items.find(x => x.id === id);

// The rule mercury/draft_rules.py counts by: merge tags count as one word,
// and a token counts when it has a letter or digit.
function obCountWords(text) {
  return String(text || '').replace(/\{\{[^{}]*\}\}/g, ' tag ').split(/\s+/)
    .filter(t => /[\p{L}\p{N}]/u.test(t)).length;
}

// ── Times (stored as naive UTC, shown in local time) ──

function obTime(s) {
  const d = parseUTC(s);
  return d ? d.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'}) : '';
}
function obDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
function obDaysFromToday(s) {
  const d = parseUTC(s);
  return d ? Math.round((obDay(d) - obDay(new Date())) / 864e5) : null;
}
function obShortDate(s) {
  const d = parseUTC(s);
  return d ? d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) : '';
}
function obWhen(s) {
  const d = parseUTC(s);
  return d ? d.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : '';
}
// Queue times: the clock for today, the day otherwise.
function obQueueTime(s) { return obDaysFromToday(s) === 0 ? obTime(s) : obShortDate(s); }
function obDayLabel(s) {
  const n = obDaysFromToday(s), d = parseUTC(s);
  if (n === null) return 'Not scheduled';
  if (n < 0) return 'Due now';
  if (n === 0) return 'Today';
  const date = d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
  if (n === 1) return 'Tomorrow, ' + date;
  return d.toLocaleDateString('en-US', {weekday: 'short'}) + ', ' + date;
}

// ── Who, from what ──

function obName(it) { return (it.contact && it.contact.name) || it.to_email || ''; }
function obFirst(it) { return (it.contact && it.contact.first_name) || ''; }
function obCompany(it) { return (it.contact && it.contact.company) || ''; }
function obStepWord(it) { return it.kind === 'reply' ? 'reply' : 'step ' + Number(it.step || 1); }
function obTotal(it) { return it.sequence ? it.sequence.total : Number(it.step || 1); }
function obOfferKey(it) { return (it.offer && it.offer.key) || it.offer_key || ''; }
function obEdit(it) { return _ob.edits[it.id] || null; }
function obText(it) {
  const e = obEdit(it);
  return {subject: e ? e.subject : (it.subject || ''), body: e ? e.body : (it.body || '')};
}
function obDirty(it) {
  const e = obEdit(it);
  return !!e && (e.subject.trim() !== (it.subject || '').trim() || e.body.trim() !== (it.body || '').trim());
}

function obOfferReason(o) {
  if (!o) return '';
  if (o.is_default) return 'default, no rule matched';
  return String(o.reason || '').replace(/^\S+ rule matched: /, 'matched ');
}

// "Search rank for roof repair: 14 (observed 2026-10-04)" -> text + day.
function obFact(line) {
  const m = /^(.*?)(?: \(observed (\d{4}-\d{2}-\d{2})\))?$/.exec(String(line));
  const when = m && m[2] ? obShortDate(m[2] + 'T12:00:00') : '';
  return '<li>' + obEsc(m ? m[1] : line) + (when ? ' <span class="muted">' + obEsc(when) + '</span>' : '') + '</li>';
}

// ── Loading ──

async function loadOutbox() {
  const [data, mbox, sending] = await Promise.all([
    api('/api/outbox'), api('/api/mailboxes'), api('/api/sending/status')]);
  _mailboxes = mbox && !mbox.error ? mbox : null;
  _ob.mb = _mailboxes;
  _ob.send = sending && !sending.error && sending.holds ? sending : null;
  if (typeof loadAway === 'function') loadAway();
  const content = document.getElementById('outbox-content');
  if (!data) {
    _ob.data = null;
    document.getElementById('outbox-views').innerHTML = '';
    document.getElementById('outbox-banner').innerHTML = '';
    content.innerHTML = offlineState();
    obRenderMobile();
    return;
  }
  _ob.data = data;
  _outbox = data;

  const pending = data.pending || [];
  const keep = _ob.focusId || (obCur() || {}).id;
  _ob.items = pending.filter(obNeedsLook).concat(pending.filter(it => !obNeedsLook(it)));
  const at = _ob.items.findIndex(x => x.id === keep);
  _ob.i = at >= 0 ? at : Math.min(_ob.i, Math.max(0, _ob.items.length - 1));
  _ob.focusId = '';
  for (const id of Object.keys(_ob.edits)) if (!obFind(id)) { delete _ob.edits[id]; delete _ob.stale[id]; }
  if (!_ob.items.length) _ob.mobileOpen = false;
  navCount('nav-outbox', pending.length);
  navCount('nav-exclusions', (data.blocked || []).length);

  const send = _ob.send;
  document.getElementById('outbox-actions').innerHTML = send && send.paused
    ? '<button class="btn btn-secondary" onclick="sendingToggle(\'resume\')">' + icon('play') + 'Resume sending</button>'
    : '<button class="btn btn-secondary" onclick="sendingToggle(\'pause\')">' + icon('pause') + 'Pause all sending</button>';
  document.getElementById('outbox-pause-mobile').innerHTML = send && send.paused
    ? '<button class="btn-square" onclick="sendingToggle(\'resume\')" title="Resume sending" aria-label="Resume sending">' + icon('play') + '</button>'
    : '<button class="btn-square" onclick="sendingToggle(\'pause\')" title="Pause all sending" aria-label="Pause all sending">' + icon('pause') + '</button>';

  // A banner only when something holds mail back, or the mailbox config
  // can't be read: a permanent bar for a thing that isn't happening is noise.
  document.getElementById('outbox-banner').innerHTML = (send ? renderSendingHolds(send) : '') +
    (mbox && mbox.error ? '<section class="panel"><div class="panel-head"><div><h3 class="icon-title">' +
      icon('warning-circle') + 'Mailbox settings unreadable</h3><p>' + obEsc(mbox.error) + '</p></div></div></section>' : '');
  obRender();
}

function obRender() {
  if (!_ob.data) return;
  document.getElementById('outbox-views').innerHTML = obViewsRow();
  const el = document.getElementById('outbox-content');
  el.innerHTML = _ob.view === 'review' ? obReview()
    : _ob.view === 'scheduled' ? obScheduled()
    : _ob.view === 'sent' ? obSent() : obFailed();
  obAfterRender();
  obRenderMobile();
}

function obSetView(v) {
  _ob.view = v;
  if (v !== 'review') _ob.mobileOpen = false;
  obRender();
}

// ── Views row: the segmented control and today's capacity ──

function obCounts() {
  const d = _ob.data;
  const failed = (d.failed || []).length + (d.blocked || []).length;
  const alarm = (d.blocked || []).length > 0 || (d.failed || []).some(r => r.status === 'failed');
  return {review: (d.pending || []).length, scheduled: (d.approved || []).length,
          sent: (d.sent_today || []).length + (d.sending || []).length, failed, alarm};
}

function obNextSend() {
  if (_ob.send && _ob.send.blocked) return '';  // nothing leaves while sending is held
  const now = Date.now();
  const due = (_ob.data.approved || [])
    .filter(r => !(r.demo && r.demo.held) && parseUTC(r.send_at))
    .map(r => parseUTC(r.send_at)).filter(d => d.getTime() >= now - 60000)
    .sort((a, b) => a - b)[0];
  if (!due) return '';
  return obDaysFromToday(due.toISOString()) === 0
    ? due.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'})
    : due.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
}

function obViewsRow() {
  const c = obCounts();
  const seg = '<div class="ob-views" role="tablist" aria-label="Outbox views">' + OB_VIEWS.map(([key, label, short]) => {
    const n = c[key];
    const bad = key === 'failed' && n > 0 && c.alarm;
    return '<button role="tab" aria-selected="' + (_ob.view === key) + '"' + (_ob.view === key ? ' class="on"' : '') +
      ' onclick="obSetView(\'' + key + '\')"><span class="ob-long">' + label + '</span><span class="ob-short">' + short + '</span>' +
      (n ? '<span class="ob-n' + (bad ? ' bad' : '') + (key === 'scheduled' || key === 'sent' ? ' ob-n-wide' : '') + '">' + n + '</span>' : '') +
      '</button>';
  }).join('') + '</div>';
  const mb = _ob.mb;
  const next = obNextSend();
  const cap = mb && mb.capacity_today
    ? '<div class="ob-capacity">' + icon('paper-plane-tilt') + '<span><b class="num">' + fmtN(mb.sent_24h) + '</b> <span class="ob-slash">/</span> <b class="num">' +
        fmtN(mb.capacity_today) + '</b> sent today' + (next ? ' &middot; next' + '<span class="ob-long"> send</span> ' + obEsc(next) : '') + '</span>' +
        '<button class="link-btn ob-mb-link" onclick="goTab(\'mailboxes\')">Mailboxes' + icon('arrow-right') + '</button></div>'
    : '';
  return seg + cap;
}

// ── To review: queue, email, why ──

function obReview() {
  if (!_ob.items.length) {
    return '<div class="card">' + emptyState('tray', 'Nothing to review',
      'Every draft Mercury writes lands here first. Approve one and it sends on schedule.') + '</div>';
  }
  if (obPhone.matches) return obQueue(true);
  const it = obCur();
  return '<section class="ob-desk" aria-label="Review">' + obQueue(false) +
    '<div class="ob-pane" id="ob-pane">' + obPane(it) + '</div>' +
    '<aside class="ob-rail" aria-label="Why this email">' + obRail(it) + '</aside></section>';
}

function obQueue(phone) {
  const look = _ob.items.filter(obNeedsLook), ready = _ob.items.length - look.length;
  const blocked = _ob.send && _ob.send.blocked;
  const approveAll = ready && !blocked
    ? '<button class="btn ' + (phone ? 'btn-primary' : 'btn-secondary btn-sm') + '" onclick="obApproveAll()">' +
        icon('checks') + 'Approve ' + ready + ' ready</button>' : '';
  const row = (it, n) => {
    const flag = obNeedsLook(it);
    const wc = Number(it.word_count || 0), lim = Number(it.word_limit || 0);
    const line = flag
      ? '<span class="ob-q-flag">' + icon('warning-circle') + obEsc(lim && wc > lim ? wc + ' of ' + lim + ' words'
          : (it.flag_details || []).map(f => f.label).join(', ') || 'Flagged') + '</span>'
      : '<span class="ob-q-sub">' + obEsc(outSubject(it) || '(no subject)') + '</span>';
    return '<button class="ob-q-item' + (n === _ob.i && !phone ? ' active' : '') + '" data-n="' + n + '" onclick="obGo(' + n + (phone ? ', true' : '') + ')">' +
      '<span class="ob-q-top"><b>' + obEsc(obName(it)) + '</b><span class="ob-q-time">' + obEsc(obQueueTime(it.send_at)) + '</span></span>' +
      '<span class="ob-q-mid">' + obEsc([obCompany(it), obStepWord(it)].filter(Boolean).join(' · ')) + '</span>' +
      '<span class="ob-q-bot">' + line + '<span class="ob-q-offer">' + obEsc(obOfferKey(it)) + '</span></span></button>';
  };
  const group = (label, list, offset) => list.length
    ? '<div class="ob-q-group">' + label + ' <span class="num">' + list.length + '</span></div>' +
      list.map((it, k) => row(it, offset + k)).join('') : '';
  const list = group('Needs a look', look, 0) + group('Ready', _ob.items.slice(look.length), look.length);
  if (phone) {
    return '<section class="ob-queue phone" aria-label="To review">' + list + '</section>' +
      (approveAll ? '<div class="ob-phone-bar">' + approveAll + '</div>' : '');
  }
  return '<section class="ob-queue" aria-label="To review"><div class="ob-q-head"><b>' + (_ob.i + 1) + ' of ' + _ob.items.length + '</b>' +
    approveAll + '</div><div class="ob-q-list" id="ob-q-list">' + list + '</div></section>';
}

function obFlagBanner(it) {
  const flags = it.flag_details || [];
  if (!flags.length) return '';
  const wc = Number(it.word_count || 0), lim = Number(it.word_limit || 0);
  const over = flags.some(f => f.code === 'over_word_limit');
  const title = over && lim ? wc + ' words, step ' + Number(it.step || 1) + ' allows ' + lim
    : flags.map(f => f.label).join(', ');
  const copy = over
    ? 'Greeting and sign-off count. Mercury tried one shorter rewrite and it still ran long, so it waits here instead of sending.'
    : 'Mercury does not send a flagged draft on its own. Edit it, regenerate it, or approve it anyway.';
  const accepted = it.flags_accepted_by && !it.needs_flag_approval;
  return '<div class="ob-flag" role="note">' + icon('warning-circle') +
    '<div><b>' + obEsc(title) + '</b><p>' + obEsc(accepted ? 'Approved anyway by ' + it.flags_accepted_by + '.' : copy) + '</p></div>' +
    (over && lim ? '<button class="btn btn-secondary btn-sm" onclick="obRewriteShorter(\'' + escAttr(it.id) + '\')">' +
      icon('sparkle') + 'Rewrite shorter</button>' : '') + '</div>';
}

function obNotes(it) {
  const pol = policyNote(it.policy, true);
  // The follow-up note lives in the rail's Sequence section; only the
  // "blocked, needs your approval again" note stays up here.
  return (it.requires_manual_review ? followupNote(it) : '') + threadNote(it) + demoNote(it) + (pol ? '<div class="desk-note">' + pol + '</div>' : '') +
    (_ob.stale[it.id] ? '<div class="desk-note hold">' + toneBadge('waiting', 'Changed') +
      '<span>This email changed while you were editing. Your text is kept here; saving replaces the newer version.</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="obDiscardEdits(\'' + escAttr(it.id) + '\')">Show the newer version</button></div>' : '');
}

function obPersona(it) {
  const p = it.writing_persona;
  if (!p) return '';
  return '<button class="ob-persona" onclick="showGenerationHistory(\'' + escAttr(it.id) + '\')" title="' +
    escAttr(p.name + ' v' + Number(p.revision) + '. View email and generation history') + '">' + icon('sparkle') + obEsc(p.name) + '</button>';
}

function obCountLine(it, text) {
  const wc = obCountWords(text.body), lim = Number(it.word_limit || 0);
  return '<span class="ob-count' + (lim && wc > lim ? ' over' : '') + '" id="ob-count">' +
    (lim ? wc + ' / ' + lim + ' words' : wc + ' words') + '</span>';
}

function obPrimaryLabel(it, text) {
  // "Approve anyway" while the draft carries a flag a person must accept.
  // A live edit that fixes the only flag (the length) reads "Approve" again.
  const flags = it.flags || [];
  if (!flags.length) return 'Approve';
  const lim = Number(it.word_limit || 0);
  if (obDirty(it) && flags.every(f => f === 'over_word_limit') && lim && obCountWords(text.body) <= lim) return 'Approve';
  return 'Approve anyway';
}

function obPane(it) {
  const t = obText(it), id = escAttr(it.id), busy = _ob.busy === it.id;
  const c = it.contact || {};
  const meta = [
    it.kind === 'reply' ? icon('arrow-bend-up-left') + 'Reply'
      : icon('list-bullets') + 'Step ' + Number(it.step || 1) + ' of ' + obTotal(it),
    icon('clock') + 'Sends ' + obEsc(obWhen(it.send_at)),
    it.manually_edited ? icon('pencil-simple') + 'Edited before sending' : '',
  ].filter(Boolean).map(x => '<span>' + x + '</span>').join('');
  return '<div class="ob-env">' +
      '<div class="ob-env-row"><span class="ob-env-k">To</span><span class="ob-env-v">' +
        (c.name ? '<b>' + obEsc(c.name) + '</b>' : '') + '<span class="ob-mono">' + obEsc(it.to_email) + '</span></span>' +
        (c.email_status ? badge(c.email_status) : '') + '</div>' +
      '<div class="ob-env-row"><span class="ob-env-k">From</span><span class="ob-env-v"><span class="ob-mono' +
        (it.from_mailbox || it.mailbox ? '' : ' muted') + '">' + obEsc(fromLabel(it)) + '</span></span>' + obPersona(it) + '</div>' +
      '<div class="ob-env-row ob-subject-row"><label class="ob-env-k" for="ob-subject">Subject</label>' +
        '<input class="ob-subject" id="ob-subject" value="' + escAttr(t.subject) + '" placeholder="Subject" autocomplete="off" oninput="obInput()">' +
        '<span class="ob-pencil" aria-hidden="true">' + icon('pencil-simple') + '</span></div>' +
      '<div class="ob-meta">' + meta + '<button class="link-btn ob-link ob-why-btn" onclick="obWhy()">' + icon('info') +
        'Why this email</button></div>' + obNotes(it) +
    '</div>' +
    '<div class="ob-body-wrap">' + obFlagBanner(it) +
      '<label class="sr-only" for="ob-body">Body</label>' +
      '<textarea class="ob-body" id="ob-body" spellcheck="true" oninput="obInput()">' + obEsc(t.body) + '</textarea>' +
    '</div>' +
    '<div class="ob-countrow"><span>Click the text to edit. Greeting and sign-off count toward the limit.</span>' + obCountLine(it, t) + '</div>' +
    '<div class="ob-actions">' +
      '<button class="btn btn-primary" id="ob-approve" onclick="obApprove(\'' + id + '\')"' + (busy ? ' disabled' : '') + '>' +
        icon('check') + '<span id="ob-approve-label">' + obPrimaryLabel(it, t) + '</span><kbd>A</kbd></button>' +
      '<button class="btn btn-secondary" onclick="obReject(\'' + id + '\')"' + (busy ? ' disabled' : '') + ' title="Reject (R)">' + icon('x') + 'Reject</button>' +
      '<button class="btn btn-secondary" id="ob-regen" onclick="obRegenerate(\'' + id + '\')"' + (busy ? ' disabled' : '') + '>' +
        icon('sparkle') + (busy === 'regen' ? 'Writing…' : 'Regenerate') + '</button>' +
      '<button class="btn btn-secondary" id="ob-save" onclick="obSave(\'' + id + '\')"' + (obDirty(it) ? '' : ' hidden') + '>Save edits</button>' +
      '<span class="ob-actions-end">' +
        '<button class="btn-square" onclick="obGo(_ob.i - 1)" title="Previous (K)" aria-label="Previous email"' + (_ob.i ? '' : ' disabled') + '>' + icon('caret-up') + '</button>' +
        '<button class="btn-square" onclick="obGo(_ob.i + 1)" title="Next (J)" aria-label="Next email"' + (_ob.i < _ob.items.length - 1 ? '' : ' disabled') + '>' + icon('caret-down') + '</button>' +
      '</span>' +
    '</div>';
}

function obWhyItem(ic, label, value) {
  return '<div class="ob-why-item">' + icon(ic) + '<div><span class="ob-why-k">' + label + '</span>' + value + '</div></div>';
}

function obSeqNote(it) {
  const seq = it.sequence;
  const name = obFirst(it) || 'they';
  if (it.kind === 'reply') return 'A reply in an open conversation. It sends once you approve it.';
  if (!seq) return '';
  const later = seq.steps.filter(s => Number(s.step) > Number(it.step) &&
    ['pending_review', 'approved', 'blocked'].includes(s.status)).map(s => s.step);
  const stop = name === 'they' ? 'They stop on their own if the contact replies.' : 'They stop on their own if ' + name + ' replies.';
  if (!later.length) return Number(it.step) > 1 ? 'Nothing is queued after this one.' : '';
  const steps = (later.length === 1 ? 'step ' : 'steps ') + later.slice(0, -1).join(', ') + (later.length > 1 ? ' and ' : '') + later[later.length - 1];
  return autoFollowups(it) ? 'Approving this email approves ' + steps + ' too. ' + stop
    : 'Each follow-up waits for your review, ' + steps + ' included. ' + stop;
}

function obRail(it) {
  const o = it.offer, p = it.pain, b = it.brief;
  const why = [];
  why.push(obWhyItem('tag', 'Offer', o
    ? '<p><span class="ob-mono">' + obEsc(o.key) + '</span>' + (obOfferReason(o) ? ' &middot; ' + obEsc(obOfferReason(o)) : '') + '</p>'
    : '<p class="muted">' + (it.kind === 'reply' ? 'A reply, written from the conversation' : 'No offer on this email') + '</p>'));
  if (it.kind !== 'reply') {
    why.push(obWhyItem('lightning', 'Pain', p
      ? '<p>' + (p.words ? '“' + obEsc(p.words.replace(/^["“]|["”]$/g, '')) + '” ' : '') +
          '<span class="ob-mono">' + obEsc(p.code) + '</span>' + (p.status && p.status !== 'confirmed'
          ? ' ' + toneBadge(p.status === 'rejected' || p.status === 'missing' ? 'bad' : 'waiting', p.status === 'missing' ? 'Removed' : statusMeta(p.status).label) : '') + '</p>'
      : '<p class="muted">None confirmed for this business</p>'));
  }
  if (b && b.facts && b.facts.length) why.push(obWhyItem('list-checks', 'Facts used', '<ul>' + b.facts.map(obFact).join('') + '</ul>'));
  if (b && b.asks_for) why.push(obWhyItem('chat-centered-text', 'Asks for', '<p>' + obEsc(b.asks_for) + '</p>'));
  if (b && b.kept_out && b.kept_out.length) why.push(obWhyItem('prohibit', 'Kept out', '<ul>' + b.kept_out.map(r => '<li>' + obEsc(r) + '</li>').join('') + '</ul>'));

  let seqHtml = '';
  if (it.sequence) {
    const total = it.sequence.total;
    seqHtml = '<div class="ob-seq">' + it.sequence.steps.map(s => {
      const mine = s.id === it.id;
      const word = mine ? 'this email' : Number(s.step) === 1 ? 'first email'
        : Number(s.step) === total && total > 2 ? 'last note' : 'follow-up';
      const tone = mine ? 'active' : s.status === 'sent' ? 'good'
        : ['pending_review', 'approved', 'blocked', 'sending'].includes(s.status) ? 'waiting'
        : s.status === 'failed' ? 'bad' : 'idle';
      const when = s.status === 'sent' ? obShortDate(s.sent_at) : mine ? obWhen(s.send_at) : obShortDate(s.send_at);
      const state = ['cancelled', 'rejected', 'failed'].includes(s.status) ? ' (' + statusMeta(s.status).label.toLowerCase() + ')' : '';
      return '<div class="ob-seq-row">' + toneBadge(tone, 'Step ' + s.step + ' · ' + word + state) +
        '<span class="ob-seq-when">' + obEsc(when) + '</span></div>';
    }).join('') + '</div>';
  }
  const note = obSeqNote(it);
  const c = it.contact;
  return '<section class="ob-rail-sec"><div class="ob-rail-head"><span>Why this email</span>' +
      (it.generation_id ? '<button class="link-btn ob-link" onclick="showGenerationHistory(\'' + escAttr(it.id) + '\')">Full prompt</button>' : '') +
      '</div>' + why.join('') + '</section>' +
    (seqHtml || note ? '<section class="ob-rail-sec"><div class="ob-rail-head"><span>Sequence</span></div>' + seqHtml +
      (note ? '<p class="ob-rail-note">' + obEsc(note) + '</p>' : '') + '</section>' : '') +
    (c ? '<section class="ob-rail-sec"><div class="ob-rail-head"><span>Contact</span>' +
        '<button class="link-btn ob-link" onclick="openProspectDrawer(\'' + escAttr(c.prospect_id) + '\')">Open</button></div>' +
        '<p class="ob-contact">' + obEsc([c.name, c.title].filter(Boolean).join(', ')) + '</p>' +
        '<p class="ob-contact muted">' + obEsc([c.company, c.location].filter(Boolean).join(' · ')) + '</p></section>' : '');
}

// The rail on narrower screens: the same content in the drawer.
function obWhy() {
  const it = obCur();
  if (!it) return;
  openDrawer('Why this email', obName(it), obEsc(outSubject(it) || ''), '<div class="ob-rail in-drawer">' + obRail(it) + '</div>');
}

// After the desk is in the DOM: size the body to its text, keep the
// selected queue item in view.
function obAfterRender() {
  const body = document.getElementById('ob-body');
  if (body) obAutosize(body);
  const active = document.querySelector('.ob-q-item.active');
  if (active) active.scrollIntoView({block: 'nearest'});
}

function obAutosize(el) {
  el.style.height = 'auto';
  el.style.height = Math.max(el.scrollHeight + 2, 96) + 'px';
}

function obInput() {
  const it = obCur();
  const s = document.getElementById('ob-subject'), b = document.getElementById('ob-body');
  if (!it || !s || !b) return;
  _ob.edits[it.id] = {subject: s.value, body: b.value};
  if (!obDirty(it)) delete _ob.edits[it.id];
  const t = obText(it);
  const count = document.getElementById('ob-count');
  if (count) count.outerHTML = obCountLine(it, t);
  const save = document.getElementById('ob-save');
  if (save) save.hidden = !obDirty(it);
  const label = document.getElementById('ob-approve-label');
  if (label) label.textContent = obPrimaryLabel(it, t);
  obAutosize(b);
}

function obDiscardEdits(id) {
  delete _ob.edits[id];
  delete _ob.stale[id];
  obRender();
}

function obGo(n, open) {
  if (n < 0 || n >= _ob.items.length) return;
  _ob.i = n;
  if (open) _ob.mobileOpen = true;
  if (obPhone.matches) { obRenderMobile(); return; }
  const pane = document.getElementById('ob-pane');
  if (!pane) { obRender(); return; }
  const it = obCur();
  pane.innerHTML = obPane(it);
  document.querySelector('.ob-rail').innerHTML = obRail(it);
  document.querySelectorAll('.ob-q-item').forEach(el => el.classList.toggle('active', Number(el.dataset.n) === n));
  const head = document.querySelector('.ob-q-head b');
  if (head) head.textContent = (n + 1) + ' of ' + _ob.items.length;
  obAfterRender();
  if (drawerOpen() && document.getElementById('drawer-kicker').textContent === 'Why this email') obWhy();
}

// ── Phone: the queue is the page; one email opens full screen ──

function obRenderMobile() {
  const el = document.getElementById('outbox-mobile');
  if (!el) return;
  const it = obCur();
  const open = obPhone.matches && _ob.mobileOpen && _ob.view === 'review' && it && currentTab === 'outbox';
  document.body.classList.toggle('ob-mobile-open', !!open);
  if (!open) { el.hidden = true; el.innerHTML = ''; return; }
  const t = obText(it), c = it.contact || {}, id = escAttr(it.id), busy = _ob.busy === it.id;
  const o = obOfferKey(it), p = it.pain, facts = ((it.brief || {}).facts || []).length;
  const summary = [o, p && p.code, facts ? facts + (facts === 1 ? ' fact' : ' facts') : ''].filter(Boolean).join(' · ');
  el.hidden = false;
  el.innerHTML =
    '<header class="ob-m-head"><button class="link-btn ob-m-back" onclick="obCloseMobile()">' + icon('caret-left') + 'Outbox</button>' +
      '<span class="ob-m-pos num">' + (_ob.i + 1) + ' of ' + _ob.items.length + '</span>' +
      '<button class="btn-square" onclick="obGo(_ob.i - 1)" aria-label="Previous email"' + (_ob.i ? '' : ' disabled') + '>' + icon('caret-up') + '</button>' +
      '<button class="btn-square" onclick="obGo(_ob.i + 1)" aria-label="Next email"' + (_ob.i < _ob.items.length - 1 ? '' : ' disabled') + '>' + icon('caret-down') + '</button></header>' +
    '<div class="ob-m-scroll">' +
      '<div class="ob-m-who"><div class="ob-m-name"><b>' + obEsc(obName(it)) + '</b>' + (c.email_status ? badge(c.email_status) : '') + '</div>' +
        '<p>' + obEsc([obCompany(it), it.kind === 'reply' ? 'reply' : 'step ' + it.step + ' of ' + obTotal(it)].filter(Boolean).join(' · ')) + '</p>' +
        '<p class="ob-mono">from ' + obEsc(fromLabel(it)) + ' &middot; ' + obEsc(obWhen(it.send_at)) + '</p>' + obNotes(it) + '</div>' +
      '<div class="ob-m-mail">' + obFlagBanner(it) +
        '<div class="ob-subject-row"><label class="sr-only" for="ob-subject">Subject</label>' +
          '<input class="ob-subject" id="ob-subject" value="' + escAttr(t.subject) + '" autocomplete="off" oninput="obInput()">' +
          '<span class="ob-pencil" aria-hidden="true">' + icon('pencil-simple') + '</span></div>' +
        '<label class="sr-only" for="ob-body">Body</label>' +
        '<textarea class="ob-body" id="ob-body" oninput="obInput()">' + obEsc(t.body) + '</textarea>' +
        '<div class="ob-m-count">' + obCountLine(it, t) + '</div>' +
        '<button class="btn btn-secondary btn-sm" id="ob-save" onclick="obSave(\'' + id + '\')"' + (obDirty(it) ? '' : ' hidden') + '>Save edits</button>' +
      '</div>' +
      '<button class="ob-m-why" onclick="obWhy()">' + icon('tag') + '<span><b>Why this email</b><small>' + obEsc(summary || 'No offer brief') + '</small></span>' + icon('caret-right') + '</button>' +
    '</div>' +
    '<footer class="ob-m-bar">' +
      '<button class="btn btn-secondary" onclick="obReject(\'' + id + '\')"' + (busy ? ' disabled' : '') + '>' + icon('x') + 'Reject</button>' +
      '<button class="btn-square" id="ob-regen" onclick="obRegenerate(\'' + id + '\')" title="Regenerate" aria-label="Regenerate"' + (busy ? ' disabled' : '') + '>' + icon('sparkle') + '</button>' +
      '<button class="btn btn-primary" id="ob-approve" onclick="obApprove(\'' + id + '\')"' + (busy ? ' disabled' : '') + '>' + icon('check') +
        '<span id="ob-approve-label">' + obPrimaryLabel(it, t) + '</span></button>' +
    '</footer>';
  const body = document.getElementById('ob-body');
  if (body) obAutosize(body);
}

function obCloseMobile() {
  _ob.mobileOpen = false;
  obRender();
}

obPhone.addEventListener('change', () => { if (currentTab === 'outbox' && _ob.data) obRender(); });

// ── Scheduled, Sent today, Didn't send ──

function obToCell(r) {
  const name = r.contact && r.contact.name;
  return name ? '<div>' + obEsc(name) + '</div><div class="ob-mono muted">' + obEsc(r.to_email) + '</div>'
    : '<span class="ob-mono">' + obEsc(r.to_email) + '</span>';
}

function obMenuBtn(r, bucket) {
  return '<button class="btn-square ghost ob-row-menu" onclick="event.stopPropagation(); obRowMenu(\'' + escAttr(r.id) + '\', \'' + bucket +
    '\', this)" aria-label="More for ' + escAttr(obName(r)) + '" title="More">' + icon('dots-three') + '</button>';
}

function obTable(cols, groups, foot) {
  const head = '<thead><tr>' + cols.map(c => '<th' + (c.cls ? ' class="' + c.cls + '"' : '') + '>' + c.label + '</th>').join('') + '</tr></thead>';
  const body = groups.filter(g => g.rows.length).map(g =>
    (g.label ? '<tr class="ob-group"><td colspan="' + cols.length + '"><b>' + obEsc(g.label) + '</b> <span class="num">' + g.rows.length + '</span></td></tr>' : '') +
    g.rows.map(r => '<tr>' + cols.map(c => '<td' + (c.cls ? ' class="' + c.cls + '"' : '') + '>' + c.cell(r) + '</td>').join('') + '</tr>').join('')).join('');
  return '<div class="table-card ob-table"><table>' + head + '<tbody>' + body + '</tbody></table>' +
    (foot ? '<div class="ob-table-foot">' + foot + '</div>' : '') + '</div>';
}

function obScheduled() {
  const rows = (_ob.data.approved || []).slice().sort((a, b) => String(a.send_at).localeCompare(String(b.send_at)));
  if (!rows.length) return '<div class="card">' + emptyState('calendar-blank', 'Nothing scheduled',
    'Approved emails wait here until their send time. Review the drafts under To review.') + '</div>';
  const groups = [];
  for (const r of rows) {
    const label = obDayLabel(r.send_at);
    const g = groups[groups.length - 1];
    if (g && g.label === label) g.rows.push(r); else groups.push({label, rows: [r]});
  }
  const held = r => r.demo && r.demo.held;
  const cols = [
    {label: 'When', cls: 'ob-when', cell: r => '<span class="ob-mono">' + obEsc(obTime(r.send_at)) + '</span>'},
    {label: 'To', cell: obToCell},
    {label: 'Subject', cell: r => '<div>' + obEsc(outSubject(r)) + '</div>' +
      (held(r) ? '<div title="' + escAttr(r.demo.reason || '') + '">' + toneBadge(DEMO_FIXABLE.has(r.demo.code) ? 'waiting' : 'bad',
        DEMO_FIXABLE.has(r.demo.code) ? 'Held until the demo is ready' : 'Held: ' + (r.demo.reason || 'demo not set up')) + '</div>' : '') +
      (policyNote(r.policy) ? '<div>' + policyNote(r.policy) + '</div>' : '')},
    {label: 'Step', cls: 'ob-num', cell: r => r.kind === 'reply' ? '<span class="muted">reply</span>' : '<span class="num">' + Number(r.step) + '</span>'},
    {label: 'Offer', cell: r => '<span class="ob-mono">' + obEsc(obOfferKey(r)) + '</span>'},
    {label: 'From', cls: 'ob-from', cell: r => fromCell(r)},
    {label: '', cls: 'ob-menu-cell', cell: r => obMenuBtn(r, 'approved')},
  ];
  const foot = _ob.mb && _ob.mb.spread_sends
    ? 'Times are spread out like a person would send them. Follow-ups stop on their own when someone replies.'
    : 'Each email goes out at its time, on the next run after it. Follow-ups stop on their own when someone replies.';
  return obTable(cols, groups, foot);
}

function obSent() {
  const d = _ob.data;
  const sending = d.sending || [], sent = d.sent_today || [];
  if (!sending.length && !sent.length) return '<div class="card">' + emptyState('paper-plane-tilt', 'Nothing sent in the last 24 hours',
    'Approved emails go out at their scheduled time. They show up here once they leave.') + '</div>';
  const cols = [
    {label: 'When', cls: 'ob-when', cell: r => '<span class="ob-mono">' + obEsc(r.status === 'sending' ? obTime(r.updated_at) : obTime(r.sent_at)) + '</span>'},
    {label: 'To', cell: obToCell},
    {label: 'Subject', cell: r => obEsc(outSubject(r))},
    {label: 'Step', cls: 'ob-num', cell: r => r.kind === 'reply' ? '<span class="muted">reply</span>' : '<span class="num">' + Number(r.step) + '</span>'},
    {label: 'Offer', cell: r => '<span class="ob-mono">' + obEsc(obOfferKey(r)) + '</span>'},
    {label: 'From', cell: r => '<span class="ob-mono muted">' + obEsc(r.from_mailbox || r.mailbox || fromLabel(r)) + '</span>'},
    {label: '', cls: 'ob-menu-cell', cell: r => obMenuBtn(r, 'sent')},
  ];
  return obTable(cols, [{label: 'Sending now', rows: sending}, {label: sending.length ? 'Sent' : '', rows: sent}],
    'The last 24 hours, the same window the daily caps count.');
}

// Why a queued email stopped, for the codes Mercury writes itself.
function obReason(r) {
  const e = String(r.error || '');
  const moved = /^moved_to_(\w+)$/.exec(e);
  if (e === 'stop_on_reply') return 'They replied, so the sequence stopped.';
  if (moved) return 'The contact moved to ' + moved[1].replace(/_/g, ' ') + ' in the pipeline.';
  if (e === 'bounced') return 'An earlier email bounced.';
  return e;
}

function obFailed() {
  const d = _ob.data;
  const blocked = d.blocked || [], failed = d.failed || [];
  if (!blocked.length && !failed.length) return '<div class="card">' + emptyState('check-circle', 'Nothing failed',
    'Emails that bounce, fail to send, or are stopped by a reply or an exclusion show up here with the reason.') + '</div>';
  const cols = [
    {label: 'When', cls: 'ob-when', cell: r => '<span class="ob-mono">' + obEsc(obWhen(r.updated_at)) + '</span>'},
    {label: 'To', cell: obToCell},
    {label: 'Subject', cell: r => obEsc(outSubject(r))},
    {label: 'Status', cell: r => badge(r.status)},
    {label: 'Reason', cell: r => r.status === 'blocked' ? policyNote(r.policy, true)
      : '<span class="muted">' + obEsc(obReason(r)) + '</span>'},
    {label: '', cls: 'ob-menu-cell', cell: r => r.status === 'blocked'
      ? '<span class="ob-row-actions"><button class="btn btn-secondary btn-sm" onclick="exRequeue(\'' + escAttr(r.id) + '\').then(loadOutbox)">Send back to review</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="exDiscard(\'' + escAttr(r.id) + '\',' + Number(r.revision) + ')">Discard</button></span>'
      : obMenuBtn(r, 'failed')},
  ];
  return obTable(cols, [{label: 'Blocked by an exclusion', rows: blocked},
                        {label: blocked.length ? 'Failed or stopped' : '', rows: failed}],
    'The most recent 25. A stopped follow-up usually means the contact replied.');
}

// Row menu: the shared popup menu (#move-menu), with this table's actions.
function obRowMenu(id, bucket, anchor) {
  const menu = document.getElementById('move-menu');
  if (!menu) return;
  if (!menu.hidden && _pipe.menuFor === 'ob:' + id) { closeMoveMenu(true); return; }
  const r = (_ob.data[bucket] || []).concat(_ob.data.sent_today || [], _ob.data.sending || []).find(x => x.id === id);
  if (!r) return;
  const items = [];
  if (r.demo && r.demo.held && DEMO_FIXABLE.has(r.demo.code)) items.push(['demo', 'Mark demo ready', '']);
  items.push(['history', 'Email history', r.writing_persona ? r.writing_persona.name + ' v' + r.writing_persona.revision : '']);
  if (r.contact) items.push(['contact', 'Open contact', '']);
  if (bucket === 'approved') items.push(['reject', 'Reject', 'cancels later steps']);
  menu.innerHTML = items.map(([k, label, small]) => '<button role="menuitem" data-to="' + k + '"><span>' + label + '</span>' +
    (small ? '<small>' + obEsc(small) + '</small>' : '') + '</button>').join('');
  menu.hidden = false;
  _pipe.menuFor = 'ob:' + id;
  _pipe.menuAnchor = anchor;
  const rect = anchor.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  let top = rect.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top - h - 4);
  menu.style.top = top + 'px';
  menu.style.left = Math.max(8, Math.min(rect.right - w, window.innerWidth - w - 8)) + 'px';
  menu.onclick = e => {
    const b = e.target.closest('[data-to]');
    if (!b) return;
    closeMoveMenu(true);
    if (b.dataset.to === 'demo') demoOpen(id);
    else if (b.dataset.to === 'history') showGenerationHistory(id);
    else if (b.dataset.to === 'contact') openProspectDrawer(r.contact.prospect_id);
    else if (b.dataset.to === 'reject') obRejectScheduled(r);
  };
  menu.onkeydown = e => {
    const els = [...menu.querySelectorAll('[data-to]')];
    const i = els.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); (els[i + 1] || els[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (els[i - 1] || els[els.length - 1]).focus(); }
    else if (e.key === 'Tab') closeMoveMenu();
  };
  const first = menu.querySelector('[data-to]');
  if (first) first.focus({preventScroll: true});
}

async function obRejectScheduled(r) {
  if (!await confirmModal({title: 'Reject this scheduled email?',
    copy: 'It will not send, and neither will the later steps of its sequence.', ok: 'Reject'})) return;
  const res = await postJSON('/api/outbox/' + encodeURIComponent(r.id) + '/reject', {revision: r.revision});
  showToast(res.ok ? 'Rejected.' : res.data && res.data.code === 'stale_revision'
    ? 'This email changed since you loaded it. Look at it again.' : 'Couldn’t reject it: ' + res.error, res.ok ? 'success' : 'error');
  loadOutbox();
}

// ── Decisions ──

async function obPut(path, body) {
  try {
    const r = await fetch(path, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let data = null;
    try { data = await r.json(); } catch { /* empty */ }
    return {ok: r.ok && !(data && data.success === false), status: r.status, data};
  } catch {
    return {ok: false, status: 0, data: null};
  }
}

// Move past an email that just left the queue: the next one takes its place.
function obAdvanceFrom(id) {
  const n = _ob.items.findIndex(x => x.id === id);
  const next = _ob.items[n + 1] || _ob.items[n - 1];
  _ob.focusId = next ? next.id : '';
}

async function obSave(id, quiet) {
  const it = obFind(id);
  if (!it) return false;
  if (!obDirty(it)) { if (!quiet) showToast('Nothing changed.', 'success'); return true; }
  const e = obEdit(it), subject = e.subject.trim(), body = e.body.trim();
  if (!subject || !body) { showToast('Subject and body can’t be empty.', 'error'); return false; }
  const res = await obPut('/api/outbox/' + encodeURIComponent(id), {subject, body, revision: it.revision});
  if (res.ok && res.data) {
    const d = res.data;
    Object.assign(it, {subject, body, revision: d.revision, status: d.status, manually_edited: 1,
      word_count: d.word_count, word_limit: d.word_limit, flags: d.flags, flag_details: d.flag_details,
      needs_flag_approval: d.needs_flag_approval, flags_accepted_by: ''});
    it.wire_subject = it.wire_subject && it.wire_subject !== it.subject ? it.wire_subject : subject;
    delete _ob.edits[id];
    delete _ob.stale[id];
    if (!quiet) { showToast('Edits saved.', 'success'); _ob.focusId = id; await loadOutbox(); }
    return true;
  }
  if (res.data && res.data.code === 'stale_revision') {
    // Keep what they typed; the reload brings the newer version underneath.
    _ob.stale[id] = true;
    _ob.focusId = id;
    showToast('This email changed since you opened it. Your edits are kept; look at the newer version first.', 'error');
    await loadOutbox();
    return false;
  }
  showToast('Couldn’t save the edits' + (res.data && res.data.message ? ': ' + res.data.message : '.'), 'error');
  return false;
}

async function obApprove(id) {
  const it = obFind(id);
  if (!it || _ob.busy) return;
  // Approving sends what is on screen, so unsaved edits go first.
  if (obDirty(it) && !(await obSave(id, true))) return;
  const flagged = (it.flags || []).length > 0;
  _ob.busy = id;
  const res = await postJSON('/api/outbox/' + encodeURIComponent(id) + '/approve',
    flagged ? {revision: it.revision, approve_flagged: true} : {revision: it.revision});
  _ob.busy = '';
  const data = res.data || {};
  if (res.ok) {
    const fu = Number(data.followups_approved || 0);
    showToast((flagged ? 'Approved anyway' : 'Approved') + (fu ? ', with ' + fu + ' follow-up' + (fu === 1 ? '' : 's') +
      '. They send on schedule.' : '. It sends on schedule.'), 'success');
    obAdvanceFrom(id);
  } else if (data.code === 'flagged') {
    _ob.focusId = id;
    showToast('This draft is flagged. Look at it, then choose Approve anyway.', 'error');
  } else {
    _ob.focusId = id;
    showToast(data.code === 'stale_revision' ? 'This email changed since you opened it. Review it again.'
      : data.success === false && !data.code ? 'It is no longer waiting for review.' : 'Approve failed: ' + res.error, 'error');
  }
  await loadOutbox();
}

async function obReject(id) {
  const it = obFind(id);
  if (!it || _ob.busy) return;
  _ob.busy = id;
  const res = await postJSON('/api/outbox/' + encodeURIComponent(id) + '/reject', {revision: it.revision});
  _ob.busy = '';
  const data = res.data || {};
  if (res.ok) {
    const later = Math.max(0, Number(data.rejected || 1) - 1);
    showToast(later ? 'Rejected, with ' + later + ' later step' + (later === 1 ? '' : 's') + ' of the sequence.' : 'Rejected.', 'success');
    delete _ob.edits[id];
    obAdvanceFrom(id);
  } else {
    _ob.focusId = id;
    showToast(data.code === 'stale_revision' ? 'This email changed since you opened it. Review it again.' : 'Reject failed: ' + res.error, 'error');
  }
  await loadOutbox();
}

async function obRegenerate(id, preset) {
  const it = obFind(id);
  if (!it || _ob.busy) return;
  const instruction = preset !== undefined ? preset : await regeneratePrompt(obFirst(it) || obName(it));
  if (instruction === null) return;
  if (obDirty(it) && !await confirmModal({title: 'Replace your edits?',
    copy: 'Regenerating writes a new draft from scratch. Your unsaved changes to this one are dropped.', ok: 'Regenerate'})) return;
  _ob.busy = id;
  obRefreshCurrent('regen');
  showToast('Rewriting. This can take up to a minute.', 'success');
  const res = await postJSON('/api/outbox/' + encodeURIComponent(id) + '/regenerate',
    {instruction: instruction.trim(), revision: it.revision});
  _ob.busy = '';
  if (res.ok) {
    // The full row again: the new draft has its own generation, brief and count.
    const fresh = await api('/api/outbox/' + encodeURIComponent(id));
    Object.assign(it, fresh && fresh.id ? fresh : res.data);
    delete _ob.edits[id];
    delete _ob.stale[id];
    showToast('New draft ready. Review it before approving.', 'success');
    obRender();
  } else {
    const data = res.data || {};
    showToast(data.code === 'stale_revision' ? 'This email changed since you opened it. Review it again.'
      : 'Rewrite failed: ' + res.error, 'error');
    _ob.focusId = id;
    await loadOutbox();
  }
}

function obRewriteShorter(id) {
  const it = obFind(id);
  if (!it) return;
  obRegenerate(id, 'Shorter: at most ' + Number(it.word_limit) + ' words including greeting and sign-off.');
}

// Re-render the current email only, keeping any unsaved text.
function obRefreshCurrent(state) {
  const it = obCur();
  if (!it) return;
  if (obPhone.matches) { obRenderMobile(); }
  else {
    const pane = document.getElementById('ob-pane');
    if (pane) { pane.innerHTML = obPane(it); obAfterRender(); }
  }
  const regen = document.getElementById('ob-regen');
  if (state === 'regen' && regen) {
    regen.disabled = true;
    if (!obPhone.matches) regen.innerHTML = icon('sparkle') + 'Writing…';
  }
}

async function obApproveAll() {
  // Exactly the ready emails on screen, at the revisions shown. A flagged
  // draft is never carried through, and anything newer stays in review.
  const items = _ob.items.filter(x => !obNeedsLook(x)).map(x => ({id: x.id, revision: x.revision}));
  if (!items.length) { showToast('Nothing ready to approve.', 'success'); return; }
  const dirty = _ob.items.filter(obDirty).length;
  if (dirty && !await confirmModal({title: 'Approve without your edits?',
    copy: dirty + (dirty === 1 ? ' email has' : ' emails have') + ' unsaved edits. Approve all sends the saved drafts. Save first to keep your changes.',
    ok: 'Approve ' + items.length})) return;
  const res = await postJSON('/api/outbox/approve-all', {items});
  const data = res.data || {};
  if (res.ok) showToast('Approved ' + data.approved + ' email' + (data.approved === 1 ? '' : 's') + '.' + (data.failed
    ? ' ' + data.failed + ' changed since you loaded them and stay in review.' : ''), 'success');
  else showToast('Approve all failed: ' + res.error, 'error');
  for (const x of items) delete _ob.edits[x.id];
  await loadOutbox();
}

// Keyboard review: J/K move, A approves (or approves anyway), R rejects.
// Never while typing, or while a dialog, sheet, drawer or menu is up.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || _ob.view !== 'review' || e.metaKey || e.ctrlKey || e.altKey) return;
  if (promptOpen() || modalOpen() || drawerOpen()) return;
  const menu = document.getElementById('move-menu');
  if (menu && !menu.hidden) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
  const cur = obCur();
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); obGo(_ob.i + 1); }
  else if (k === 'k') { e.preventDefault(); obGo(_ob.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); obApprove(cur.id); }
  else if (k === 'r' && cur) { e.preventDefault(); obReject(cur.id); }
});

// Unsaved text survives a refresh of the list, but not leaving the page.
window.addEventListener('beforeunload', e => {
  if (Object.keys(_ob.edits).length) { e.preventDefault(); e.returnValue = ''; }
});
