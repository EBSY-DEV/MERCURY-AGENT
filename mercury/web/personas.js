// Voice & Personas: edit personas, compare versions, inspect prompts, set the default.
let _voiceData = null, _voiceView = 'profiles', _personaEdit = null;
let _voiceDirty = false, _voiceBusy = false, _voiceRequest = 0, _historyRequest = 0;
let _voiceVersions = {}, _voiceCompare = null, _voiceContacts = null, _avatarChoice = null;
let _voicePrompt = {persona: '', version: '', contact: '', instruction: ''}, _voicePromptTimer = null;

const VOICE_LIMITS = {name: 80, description: 500, tone: 1000, instructions: 8000, examples: 8000};
const VOICE_WRITING = ['tone', 'instructions', 'examples'];
const VOICE_SECTION_ICON = {template: 'file-text', knowledge: 'books', persona: 'sparkle', email: 'address-book', format: 'brackets-curly'};

function personaImage(p, extra = '') {
  return '<img class="persona-avatar ' + extra + '" src="' + escAttr(p.avatar_url || '/static/avatars/' + p.avatar_seed + '.svg') + '" alt="">';
}

function personaChip(item) {
  const p = item.writing_persona;
  return '<button class="persona-chip" onclick="showGenerationHistory(\'' + escHtml(item.id) + '\')" title="View email and generation history">' +
    (p ? personaImage(p, 'tiny') : icon('question')) +
    '<span>' + (p ? escHtml(p.name) + ' · v' + Number(p.revision) : 'Unknown persona') + '</span></button>';
}

const voicePersona = id => _voiceData.personas.find(p => p.id === id);
const voiceSaved = () => (_personaEdit && _personaEdit.id ? voicePersona(_personaEdit.id) : null);
const voiceNum = n => Number(n || 0).toLocaleString('en-US');
const voiceRate = (replies, sent) => (sent ? (replies / sent * 100).toFixed(1) + '%' : 'None yet');

async function loadPersonas(selectedId) {
  if (_voiceData && _voiceDirty && !selectedId) return;
  const request = ++_voiceRequest;
  const res = await getJSON('/api/personas');
  if (request !== _voiceRequest) return;
  const loading = document.getElementById('voice-loading');
  if (!res.ok) {
    loading.hidden = false;
    loading.innerHTML = unavailableState(res, 'sparkle', 'Personas');
    return;
  }
  _voiceData = res.data;
  _voiceVersions = {};
  loading.hidden = true;
  const id = selectedId || (_personaEdit && _personaEdit.id) || _voiceData.default_id;
  _personaEdit = {...(voicePersona(id) || _voiceData.personas[0])};
  _voiceDirty = false;
  renderPersonaList(); renderPersonaEditor(); renderPersonaRail();
  voiceView(_voiceView);
}

function voiceView(view) {
  if (!_voiceData) return;
  _voiceView = view;
  document.querySelectorAll('[data-voice-view]').forEach(b => {
    b.classList.toggle('on', b.dataset.voiceView === view);
    b.setAttribute('aria-pressed', String(b.dataset.voiceView === view));
  });
  const inLayout = view === 'profiles' || view === 'versions';
  document.getElementById('voice-layout').hidden = !inLayout;
  document.getElementById('voice-actions').hidden = view !== 'profiles';
  for (const v of ['profiles', 'versions', 'prompt', 'config']) document.getElementById('voice-' + v).hidden = v !== view;
  if (view === 'versions') renderVersions();
  if (view === 'prompt') renderVoicePrompt();
  if (view === 'config') renderVoiceConfig();
}

// ── Persona list ──

function renderPersonaList() {
  const all = _voiceData.personas, active = all.filter(p => !p.archived).length;
  const rows = (_personaEdit && !_personaEdit.id ? [_personaEdit] : []).concat(all);
  document.getElementById('persona-list').innerHTML =
    '<div class="panel-head"><div><h3>Personas</h3><p>' + active + ' in use' +
      (all.length > active ? ', ' + (all.length - active) + ' archived' : '') + '</p></div></div>' +
    '<div class="persona-rows">' + rows.map(p => {
      const selected = p === _personaEdit || p.id === _personaEdit.id, drafted = (_voiceData.totals[p.id] || {}).drafted;
      return '<button class="persona-row' + (selected ? ' selected' : '') + (p.archived ? ' archived' : '') + '"' +
        (p.id ? ' onclick="selectPersona(\'' + escAttr(p.id) + '\')"' : '') + ' aria-pressed="' + selected + '">' + personaImage(p) +
        '<span class="persona-row-main"><span class="persona-row-title"><strong>' + escHtml(p.name || 'New persona') + '</strong>' +
          (p.id ? '<span class="voice-rev">v' + p.revision + '</span>' : '') + '</span>' +
        '<p>' + escHtml(p.description || p.tone || 'Not saved yet') + '</p><span class="persona-meta">' +
          (p.id === _voiceData.default_id ? toneBadge('good', 'Default') : '') +
          (p.archived ? '<span>' + icon('archive') + 'Archived</span>'
            : p.id ? '<span>' + icon('envelope-simple') + (drafted ? voiceNum(drafted) + ' emails' : 'No emails yet') + '</span>' : '') +
        '</span></span></button>';
    }).join('') + '</div>';
}

async function voiceKeepEdits(copy) {
  if (!_voiceDirty) return true;
  if (!await confirmModal({title: 'Discard unsaved changes?', copy: copy || 'This persona has changes you haven’t saved.', ok: 'Discard changes'})) return false;
  _voiceDirty = false;
  return true;
}

async function selectPersona(id) {
  if (_voiceBusy || (_personaEdit && id === _personaEdit.id)) return;
  if (!await voiceKeepEdits()) return;
  _personaEdit = {...voicePersona(id)};
  renderPersonaList(); renderPersonaEditor(); renderPersonaRail();
  if (_voiceView === 'versions') renderVersions();
}

async function newPersona(copy = false) {
  if (!_voiceData || _voiceBusy || !await voiceKeepEdits()) return;
  const source = copy ? {..._personaEdit} : {description: '', tone: '', instructions: '', examples: ''};
  const seeds = _voiceData.avatars.map(a => a.seed);
  _personaEdit = {...source, id: '', version_id: '', revision: 0, archived: false,
    name: copy ? source.name + ' copy' : '', avatar_seed: copy ? source.avatar_seed : seeds[Math.floor(Math.random() * seeds.length)]};
  delete _personaEdit.avatar_url;
  _voiceDirty = copy;
  renderPersonaList(); renderPersonaEditor(); renderPersonaRail(); voiceView('profiles');
  document.getElementById('voice-name').focus();
}

// ── Editor ──

function voiceField(id, label, help, value, rows, placeholder) {
  const attrs = ' class="form-input" id="voice-' + id + '" maxlength="' + VOICE_LIMITS[id] + '" placeholder="' + escAttr(placeholder) + '"';
  return '<div class="voice-field"><div class="voice-field-head"><label class="form-label" for="voice-' + id + '">' + label +
    '</label><span class="voice-count" id="voice-count-' + id + '"></span></div>' +
    (rows ? '<textarea' + attrs + ' rows="' + rows + '">' + escHtml(value || '') + '</textarea>' : '<input' + attrs + ' value="' + escAttr(value || '') + '">') +
    '<p class="voice-help">' + help + '</p></div>';
}

function voiceFormValues() {
  const values = {};
  for (const key of ['name', 'description', ...VOICE_WRITING]) {
    const el = document.getElementById('voice-' + key);
    values[key] = el ? el.value : (_personaEdit[key] || '');
  }
  return values;
}

function voiceChanges() {
  const saved = voiceSaved();
  if (!saved) return {writing: true, label: true};
  const values = voiceFormValues(), same = key => (values[key] || '').trim() === (saved[key] || '').trim();
  return {writing: !VOICE_WRITING.every(same),
    label: !['name', 'description'].every(same) || _personaEdit.avatar_seed !== saved.avatar_seed};
}

function renderPersonaEditor() {
  const p = _personaEdit, existing = !!p.id, isDefault = existing && p.id === _voiceData.default_id;
  const off = p.archived ? ' disabled' : '';
  document.getElementById('persona-editor').innerHTML =
    '<form class="panel voice-editor" id="voice-form" onsubmit="savePersona(event)" oninput="voiceInput()">' +
    '<div class="voice-identity"><div class="voice-avatar-block"><span id="voice-avatar">' + personaImage(p, 'large') + '</span>' +
      '<button type="button" class="link-btn" onclick="openAvatarPicker()"' + off + '>' + icon('shuffle') + 'Change</button></div>' +
      '<div class="voice-identity-main"><div class="voice-identity-row">' +
        '<input class="form-input" id="voice-name" aria-label="Persona name" placeholder="Persona name" maxlength="80" value="' + escAttr(p.name) + '"' + off + '>' +
        (existing ? '<span class="voice-rev">v' + p.revision + '</span>' : '') +
        (isDefault ? toneBadge('good', 'Default for new drafts') : '') + (p.archived ? toneBadge('idle', 'Archived') : '') +
        '<span class="voice-identity-actions">' +
          (existing && !isDefault && !p.archived ? '<button type="button" class="btn btn-secondary btn-sm" onclick="personaAction(\'default\')">Set as default</button>' : '') +
          (existing && !isDefault ? '<button type="button" class="btn btn-secondary btn-sm" onclick="personaAction(\'archive\')">' + icon('archive') + (p.archived ? 'Restore' : 'Archive') + '</button>' : '') +
        '</span></div>' +
        '<input class="form-input" id="voice-description" aria-label="Description" maxlength="500" placeholder="When should Mercury use this voice?" value="' + escAttr(p.description) + '"' + off + '>' +
        '<p class="voice-help">Name, description and avatar are labels. Changing them does not create a new version.</p></div></div>' +
    '<fieldset class="voice-form"' + off + ' style="border:0;margin:0">' +
      '<div class="voice-section-label"><h4>How it writes</h4><p>' + (existing
        ? 'Saving changes here creates v' + (p.revision + 1) + '. Drafts already written keep the version they used.'
        : 'These become version 1.') + '</p></div>' +
      voiceField('tone', 'Tone', 'One line. Mercury repeats it at the top of every writing prompt.', p.tone, 0, 'Warm, respectful, direct. Short sentences and familiar words.') +
      voiceField('instructions', 'Writing preferences', 'Rules for this voice. They apply inside Mercury’s shared email rules and never override them.', p.instructions, 6, 'How it opens, how long its sentences are, how it asks the question.') +
      voiceField('examples', 'Style examples', 'Paste one or two emails that sound right. Mercury matches the voice, never the facts.', p.examples, 6, 'An email you have sent that sounds like you.') +
    '</fieldset><div class="panel-foot" id="voice-foot"></div></form>';
  updateVoiceCounts(); renderVoiceFoot();
}

function voiceInput() {
  Object.assign(_personaEdit, voiceFormValues());
  const changes = voiceChanges();
  _voiceDirty = changes.writing || changes.label;
  updateVoiceCounts(); renderVoiceFoot();
}

function updateVoiceCounts() {
  for (const key of VOICE_WRITING) {
    const el = document.getElementById('voice-' + key), count = document.getElementById('voice-count-' + key);
    if (!el || !count) continue;
    count.textContent = voiceNum(el.value.length) + ' / ' + voiceNum(VOICE_LIMITS[key]);
    count.classList.toggle('over', el.value.length >= VOICE_LIMITS[key]);
  }
}

function renderVoiceFoot() {
  const p = _personaEdit, foot = document.getElementById('voice-foot');
  if (!foot) return;
  if (p.archived) { foot.innerHTML = toneBadge('idle', 'Archived. Restore it to edit.'); return; }
  const changes = voiceChanges(), changed = changes.writing || changes.label;
  let state, label = 'Save changes';
  if (!p.id) { state = toneBadge('note', 'A new persona starts at v1.'); label = 'Create persona'; }
  else if (changes.writing) { state = toneBadge('waiting', 'Unsaved changes to how it writes.'); label = 'Save as v' + (p.revision + 1); }
  else if (changes.label) state = toneBadge('waiting', 'Unsaved label changes. The writing stays v' + p.revision + '.');
  else state = toneBadge('good', 'Saved. New drafts use v' + p.revision + '.');
  foot.innerHTML = '<span class="voice-foot-state">' + state + '<span id="voice-save-error" class="voice-error" role="alert" hidden></span></span>' +
    '<span class="voice-foot-buttons">' + (p.id && changed ? '<button type="button" class="btn btn-secondary btn-sm" onclick="discardPersona()">Discard</button>' : '') +
    '<button class="btn btn-primary btn-sm" id="voice-save" type="submit"' + (p.id && !changed ? ' disabled' : '') + '>' + label + '</button></span>';
}

function discardPersona() {
  const saved = voiceSaved();
  if (!saved) return;
  _personaEdit = {...saved}; _voiceDirty = false;
  renderPersonaList(); renderPersonaEditor(); renderPersonaRail();
}

async function savePersona(event) {
  event.preventDefault();
  if (_voiceBusy) return;
  const values = voiceFormValues(), data = {avatar_seed: _personaEdit.avatar_seed, expected_revision: _personaEdit.revision || null};
  for (const key in values) data[key] = values[key].trim();
  const error = document.getElementById('voice-save-error');
  if (!data.name || !data.tone) { error.textContent = 'Add a name and a tone before saving.'; error.hidden = false; return; }
  const writing = voiceChanges().writing;
  _voiceBusy = true;
  const button = document.getElementById('voice-save');
  button.disabled = true; button.textContent = 'Saving…';
  const path = _personaEdit.id ? '/api/personas/' + encodeURIComponent(_personaEdit.id) + '/save' : '/api/personas';
  const res = await postJSON(path, data);
  _voiceBusy = false;
  if (!res.ok) { renderVoiceFoot(); const e = document.getElementById('voice-save-error'); e.textContent = res.error; e.hidden = false; return; }
  _voiceDirty = false;
  showToast(!_personaEdit.id ? 'Persona created.' : writing ? 'Saved as v' + (_personaEdit.revision + 1) + '.' : 'Persona saved.', 'success');
  await loadPersonas(res.data.id);
}

async function personaAction(action) {
  if (_voiceBusy) return;
  if (_voiceDirty) { showToast('Save or discard your changes first.', 'error'); return; }
  _voiceBusy = true;
  const res = await postJSON('/api/personas/' + encodeURIComponent(_personaEdit.id) + '/' + action,
    action === 'archive' ? {archived: !_personaEdit.archived} : undefined);
  _voiceBusy = false;
  if (!res.ok) { showToast(res.error, 'error'); return; }
  showToast(action === 'default' ? 'New drafts now use ' + _personaEdit.name + '.' : 'Persona updated.', 'success');
  await loadPersonas(_personaEdit.id);
}

// ── Avatar picker ──

function openAvatarPicker() {
  if (!_personaEdit || _personaEdit.archived) return;
  _avatarChoice = _personaEdit.avatar_seed;
  document.getElementById('avatar-modal-title').textContent = 'Choose an avatar' + (_personaEdit.name ? ' for ' + _personaEdit.name : '');
  renderAvatarPicker();
  const modal = document.getElementById('avatar-modal');
  modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
  setTimeout(() => document.querySelector('#avatar-picker .selected')?.focus(), 0);
}

function renderAvatarPicker() {
  const saved = (voiceSaved() || {}).avatar_seed;
  document.getElementById('avatar-picker').innerHTML = _voiceData.avatars.map((a, i) =>
    '<button type="button" class="avatar-choice' + (a.seed === _avatarChoice ? ' selected' : '') + (a.seed === saved ? ' current' : '') +
    '" onclick="chooseAvatar(\'' + escAttr(a.seed) + '\')" ondblclick="closeAvatarPicker(true)" aria-label="Avatar ' + (i + 1) +
    (a.seed === saved ? ', current' : '') + '" aria-pressed="' + (a.seed === _avatarChoice) + '"><img src="' + escAttr(a.url) + '" alt=""></button>').join('');
}

function chooseAvatar(seed) { _avatarChoice = seed; renderAvatarPicker(); document.querySelector('#avatar-picker .selected')?.focus(); }

function shuffleAvatarChoice() {
  const seeds = _voiceData.avatars.map(a => a.seed).filter(s => s !== _avatarChoice);
  chooseAvatar(seeds[Math.floor(Math.random() * seeds.length)]);
}

function closeAvatarPicker(apply) {
  const modal = document.getElementById('avatar-modal');
  if (!modal.classList.contains('open')) return;
  modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true');
  if (apply && _avatarChoice && _avatarChoice !== _personaEdit.avatar_seed) {
    _personaEdit.avatar_seed = _avatarChoice; delete _personaEdit.avatar_url;
    document.getElementById('voice-avatar').innerHTML = personaImage(_personaEdit, 'large');
    voiceInput();
  }
  document.querySelector('.voice-avatar-block .link-btn')?.focus();
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('avatar-modal')?.classList.contains('open')) closeAvatarPicker(false);
});

// ── Rail ──

async function voiceVersions(id, force = false) {
  if (!force && _voiceVersions[id]) return _voiceVersions[id];
  const res = await getJSON('/api/personas/' + encodeURIComponent(id) + '/versions');
  if (!res.ok) return null;
  return (_voiceVersions[id] = res.data.versions);
}

async function renderPersonaRail() {
  const p = _personaEdit, rail = document.getElementById('persona-rail');
  const tryPanel = '<section class="panel"><div class="panel-head"><div><h3>' + icon('eye') + ' Hear it before you save</h3><p>' + (p.id
      ? 'Write a sample email to a real contact, including unsaved changes. Nothing is queued or sent.</p></div></div>' +
        '<div class="panel-foot"><button class="link-btn" onclick="openVoicePreview()">Preview with a contact' + icon('arrow-right') + '</button></div>'
      : 'Save the persona first, then preview it with a real contact.</p></div></div>') + '</section>';
  if (!p.id) { rail.innerHTML = tryPanel; return; }
  const t = _voiceData.totals[p.id] || {};
  rail.innerHTML = '<section class="panel"><div class="panel-head"><div><h3>History</h3><p>Every email records the version that wrote it.</p></div></div>' +
    '<dl class="voice-stats"><div><dt>Versions</dt><dd>' + p.revision + '</dd></div><div><dt>Emails</dt><dd>' + voiceNum(t.drafted) +
      '</dd></div><div><dt>Replies</dt><dd>' + voiceRate(t.replies, t.sent) + '</dd></div></dl>' +
    '<div class="voice-recent" id="voice-recent"></div>' +
    '<div class="panel-foot"><button class="link-btn" onclick="voiceView(\'versions\')">Compare versions' + icon('arrow-right') + '</button></div></section>' + tryPanel;
  const versions = await voiceVersions(p.id);
  const el = document.getElementById('voice-recent');
  if (!el || p !== _personaEdit || !versions) return;
  el.innerHTML = versions.slice(0, 3).map((v, i) => '<div class="voice-recent-row"><span class="voice-rev' + (i ? '' : ' current') + '">v' + v.revision +
    '</span><span>' + escHtml(v.tone.length > 48 ? v.tone.slice(0, 47) + '…' : v.tone) + '</span><span class="voice-rev">' +
    escHtml(formatDate(parseUTC(v.created_at)).split(',')[0]) + '</span></div>').join('');
}

// ── Versions ──

function lineDiff(a, b) {
  const x = (a || '').split('\n').filter(l => l.trim()), y = (b || '').split('\n').filter(l => l.trim());
  const dp = Array.from({length: x.length + 1}, () => new Array(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
    dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push([' ', x[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(['-', x[i++]]);
    else out.push(['+', y[j++]]);
  }
  while (i < x.length) out.push(['-', x[i++]]);
  while (j < y.length) out.push(['+', y[j++]]);
  return out;
}

async function renderVersions() {
  const el = document.getElementById('voice-versions'), p = voiceSaved();
  if (!p) { el.innerHTML = emptyState('clock-counter-clockwise', 'Save this persona first', 'Its versions appear here once it has been saved.'); return; }
  const versions = await voiceVersions(p.id);
  if (_voiceView !== 'versions' || voiceSaved() !== p) return;
  if (!versions) { el.innerHTML = offlineState(); return; }
  const latest = versions[0].revision;
  if (!_voiceCompare || _voiceCompare.persona !== p.id)
    _voiceCompare = {persona: p.id, from: versions[1] ? versions[1].revision : latest, to: latest};
  const from = versions.find(v => v.revision === _voiceCompare.from), to = versions.find(v => v.revision === _voiceCompare.to);
  const option = (selected) => versions.map(v => '<option value="' + v.revision + '"' + (v.revision === selected ? ' selected' : '') + '>v' + v.revision +
    ' · ' + escHtml(formatDate(parseUTC(v.created_at)).split(',')[0]) + (v.revision === latest ? ' · current' : '') + '</option>').join('');
  const blocks = [['Tone', 'tone'], ['Writing preferences', 'instructions'], ['Style examples', 'examples']].map(([label, key]) => {
    const lines = lineDiff(from[key], to[key]), added = lines.filter(l => l[0] === '+').length, removed = lines.filter(l => l[0] === '-').length;
    const summary = added || removed ? [added && added + ' added', removed && removed + ' removed'].filter(Boolean).join(', ') : 'unchanged';
    return '<div><div class="voice-diff-label">' + label + '<span>' + summary + '</span></div><div class="voice-diff-lines">' +
      (lines.length ? lines.map(([k, text]) => '<div class="voice-diff-line' + (k === '+' ? ' add' : k === '-' ? ' del' : '') + '"><span>' +
        (k === ' ' ? '' : k === '-' ? '−' : '+') + '</span><span>' + escHtml(text) + '</span></div>').join('')
        : '<div class="voice-diff-empty">Empty in both versions.</div>') + '</div></div>';
  }).join('');
  const canRestore = from.revision !== latest;
  el.innerHTML = '<section class="panel"><div class="voice-versions-head"><div><h3>' + escHtml(p.name) + '</h3><p class="voice-help">' +
      (versions.length > 1 ? 'Comparing v' + from.revision + ' with v' + to.revision + '. ' : 'Only one version so far. ') +
      'Every draft keeps a link to the version that wrote it.</p></div>' +
      (versions.length > 1 ? '<div class="voice-compare"><select class="form-input" aria-label="Compare from" onchange="voiceCompareSet(\'from\', this.value)">' + option(from.revision) +
        '</select>' + icon('arrow-right') + '<select class="form-input" aria-label="Compare to" onchange="voiceCompareSet(\'to\', this.value)">' + option(to.revision) + '</select></div>' : '') +
    '</div><div class="voice-versions-body"><div class="voice-timeline">' + versions.map(v =>
      '<button class="' + (v.revision === from.revision && from !== to ? 'from' : '') + (v.revision === to.revision ? ' to' : '') +
        '" onclick="voiceCompareSet(\'from\', ' + v.revision + ')"><span class="voice-timeline-main">' +
        '<span class="head"><span class="voice-rev">v' + v.revision + '</span><span class="date">' + escHtml(formatDate(parseUTC(v.created_at))) + '</span></span>' +
        '<p>' + escHtml(v.tone) + '</p><span class="meta"><span>' + voiceNum(v.drafted) + ' emails</span><span>' + voiceNum(v.sent) + ' sent</span>' +
        (v.sent ? '<span>' + voiceRate(v.replies, v.sent) + ' replies</span>' : '') + '</span>' + (v.revision === latest ? toneBadge('good', 'Current') : '') + '</span></button>').join('') +
      '<p class="voice-timeline-note">Replies count contacts who answered any email that version wrote.</p></div>' +
      '<div class="voice-diff">' + blocks + '</div></div>' +
    '<div class="panel-foot"><span>' + (canRestore ? toneBadge('note', 'Restoring copies v' + from.revision + ' into a new v' + (latest + 1) + '. v' + latest + ' and every email it wrote stay as they are.')
      : 'Pick an older version on the left to compare or restore it.') + '</span>' +
      (canRestore && !p.archived ? '<span class="voice-foot-buttons"><button class="btn btn-secondary btn-sm" onclick="previewVersion(' + from.revision + ')">' + icon('eye') + 'Preview v' + from.revision +
        '</button><button class="btn btn-primary btn-sm" onclick="restoreVersion(' + from.revision + ')">' + icon('arrow-counter-clockwise') + 'Restore v' + from.revision + ' as v' + (latest + 1) + '</button></span>' : '') +
    '</div></section>';
}

function voiceCompareSet(which, revision) {
  _voiceCompare[which] = Number(revision);
  renderVersions();
}

async function restoreVersion(revision) {
  const p = voiceSaved(), v = (_voiceVersions[p.id] || []).find(x => x.revision === revision);
  if (!v || _voiceBusy || !await voiceKeepEdits('Restoring replaces the unsaved changes in the editor.')) return;
  _voiceBusy = true;
  const res = await postJSON('/api/personas/' + encodeURIComponent(p.id) + '/save', {name: p.name, description: p.description || '',
    avatar_seed: p.avatar_seed, tone: v.tone, instructions: v.instructions || '', examples: v.examples || '', expected_revision: p.revision});
  _voiceBusy = false;
  if (!res.ok) { showToast(res.error, 'error'); return; }
  showToast('Restored v' + revision + ' as v' + (p.revision + 1) + '.', 'success');
  _voiceCompare = null;
  await loadPersonas(p.id);
}

function previewVersion(revision) {
  const p = voiceSaved(), v = (_voiceVersions[p.id] || []).find(x => x.revision === revision);
  _voicePrompt = {..._voicePrompt, persona: p.id, version: v ? v.id : ''};
  voiceView('prompt');
}

// ── Prompt preview ──

function openVoicePreview() {
  _voicePrompt = {..._voicePrompt, persona: _personaEdit.id, version: _voiceDirty && voiceChanges().writing ? 'unsaved' : ''};
  voiceView('prompt');
}

async function renderVoicePrompt() {
  const el = document.getElementById('voice-prompt'), active = _voiceData.personas.filter(p => !p.archived);
  if (!voicePersona(_voicePrompt.persona) || voicePersona(_voicePrompt.persona).archived)
    _voicePrompt.persona = (_personaEdit && !_personaEdit.archived && _personaEdit.id) || _voiceData.default_id;
  const persona = voicePersona(_voicePrompt.persona);
  el.innerHTML = '<section class="panel"><div class="voice-controls">' +
      '<div><label class="form-label" for="voice-prompt-persona">Persona</label><select class="form-input" id="voice-prompt-persona" onchange="voicePromptChanged(\'persona\', this.value)">' +
        active.map(p => '<option value="' + escAttr(p.id) + '"' + (p.id === persona.id ? ' selected' : '') + '>' + escHtml(p.name) + '</option>').join('') + '</select></div>' +
      '<div><label class="form-label" for="voice-prompt-version">Version</label><select class="form-input" id="voice-prompt-version" onchange="voicePromptChanged(\'version\', this.value)"><option>Loading…</option></select></div>' +
      '<div><label class="form-label" for="voice-prompt-contact">Contact</label><select class="form-input" id="voice-prompt-contact" onchange="voicePromptChanged(\'contact\', this.value)"><option value="">Loading contacts…</option></select></div>' +
      '<div><label class="form-label" for="voice-prompt-instruction">Instruction (optional)</label><input class="form-input" id="voice-prompt-instruction" maxlength="500" placeholder="For example: mention their Saturday hours" value="' +
        escAttr(_voicePrompt.instruction) + '" oninput="voicePromptChanged(\'instruction\', this.value)"></div>' +
      '<div class="btn-group"><button class="btn btn-secondary" id="voice-inspect" onclick="runVoicePreview(false)" disabled>' + icon('magnifying-glass') + 'Inspect prompt</button>' +
        '<button class="btn btn-primary" id="voice-generate" onclick="runVoicePreview(true)" disabled>' + icon('sparkle') + 'Write sample</button></div>' +
    '</div><div class="panel-foot"><span>' + toneBadge('note', 'Inspecting makes no model call. Writing a sample uses one Claude call and never queues or sends anything.') + '</span></div></section>' +
    '<div class="voice-columns"><section class="panel" id="voice-prompt-result">' + emptyState('eye', 'Pick a contact', 'You will see each part of the prompt the writer receives, in order.') + '</section>' +
    '<section class="panel" id="voice-sample">' + emptyState('envelope-simple', 'No sample yet', 'Write a sample to read the email this voice would send. It is never queued.') + '</section></div>';
  const [versions, contacts] = await Promise.all([voiceVersions(persona.id),
    _voiceContacts ? Promise.resolve(_voiceContacts) : getJSON('/api/prospects').then(r => (r.ok ? (_voiceContacts = r.data) : null))]);
  if (_voiceView !== 'prompt' || !document.getElementById('voice-prompt-contact')) return;
  const unsaved = _personaEdit && _personaEdit.id === persona.id && _voiceDirty && voiceChanges().writing;
  if (_voicePrompt.version === 'unsaved' && !unsaved) _voicePrompt.version = '';
  if (_voicePrompt.version !== 'unsaved' && !(versions || []).some(v => v.id === _voicePrompt.version)) _voicePrompt.version = versions && versions[0] ? versions[0].id : '';
  document.getElementById('voice-prompt-version').innerHTML = (unsaved ? '<option value="unsaved"' + (_voicePrompt.version === 'unsaved' ? ' selected' : '') + '>Unsaved edits</option>' : '') +
    (versions || []).map((v, i) => '<option value="' + escAttr(v.id) + '"' + (v.id === _voicePrompt.version ? ' selected' : '') + '>v' + v.revision + (i ? '' : ' · current') + '</option>').join('');
  const select = document.getElementById('voice-prompt-contact');
  if (!contacts || !contacts.length) {
    select.innerHTML = '<option value="">' + (contacts ? 'Add a contact to preview an email' : 'Could not load contacts') + '</option>';
    return;
  }
  if (!contacts.some(c => c.id === _voicePrompt.contact)) _voicePrompt.contact = contacts[0].id;
  select.innerHTML = contacts.map(c => '<option value="' + escAttr(c.id) + '"' + (c.id === _voicePrompt.contact ? ' selected' : '') + '>' +
    escHtml(([c.first_name, c.last_name].filter(Boolean).join(' ') || c.email || c.company || 'Unnamed contact') + (c.company ? ' · ' + c.company : '')) + '</option>').join('');
  document.getElementById('voice-inspect').disabled = document.getElementById('voice-generate').disabled = false;
  runVoicePreview(false);
}

function voicePromptChanged(key, value) {
  _voicePrompt[key] = value;
  if (key === 'persona') { _voicePrompt.version = ''; renderVoicePrompt(); return; }
  clearTimeout(_voicePromptTimer);
  _voicePromptTimer = setTimeout(() => runVoicePreview(false), key === 'instruction' ? 500 : 0);
}

function voicePromptBody() {
  const persona = voicePersona(_voicePrompt.persona);
  const body = {prospect_id: _voicePrompt.contact, instruction: _voicePrompt.instruction.trim()};
  if (_voicePrompt.version === 'unsaved') {
    body.version_id = persona.version_id;
    body.draft = {tone: _personaEdit.tone || '', instructions: _personaEdit.instructions || '', examples: _personaEdit.examples || ''};
  } else body.version_id = _voicePrompt.version;
  return body;
}

async function runVoicePreview(generate) {
  if (!_voicePrompt.contact || !_voicePrompt.version || (_voiceBusy && generate)) return;
  const request = ++_voiceRequest, body = voicePromptBody();
  const promptEl = document.getElementById('voice-prompt-result'), sampleEl = document.getElementById('voice-sample');
  if (generate) {
    _voiceBusy = true;
    document.getElementById('voice-generate').disabled = true;
    sampleEl.innerHTML = '<div class="panel-head"><div><h3>Sample email</h3><p role="status">Writing your sample…</p></div></div>';
  }
  const inspected = await postJSON('/api/personas/prompt', body);
  if (request === _voiceRequest && promptEl && document.contains(promptEl)) renderPromptSections(promptEl, inspected);
  if (!generate) return;
  const res = await postJSON('/api/personas/preview', body);
  _voiceBusy = false;
  const button = document.getElementById('voice-generate');
  if (button) button.disabled = false;
  if (!document.contains(sampleEl)) return;
  if (!res.ok) { sampleEl.innerHTML = '<div class="panel-head"><div><h3>Sample email</h3><p class="voice-error" role="alert">' + escHtml(res.error) + '</p></div></div>'; return; }
  renderSample(sampleEl, res.data);
}

function voiceTokens(text) { return Math.ceil((text || '').length / 4); }

function renderPromptSections(el, res) {
  if (!res.ok) { el.innerHTML = '<div class="panel-head"><div><h3>Assembled prompt</h3><p class="voice-error" role="alert">' + escHtml(res.error) + '</p></div></div>'; return; }
  const d = res.data, p = d.persona;
  const source = {template: 'prompts/writer.md', knowledge: 'Writer files in skills/', persona: p.name + ' · ' + (p.unsaved ? 'unsaved edits of v' + p.revision : 'v' + p.revision),
    email: 'Contact facts, rules and your instruction', format: 'JSON only'};
  el.innerHTML = '<div class="panel-head"><div><h3>Assembled prompt</h3><p>Exactly what the writer receives, in order.</p></div>' +
      '<span class="num" title="Estimated at four characters per token">≈ ' + voiceNum(voiceTokens(d.prompt)) + ' tokens</span></div>' +
    '<div>' + d.sections.map(s => '<details class="voice-section"' + (s.key === 'persona' ? ' open' : '') + '><summary>' +
      icon(VOICE_SECTION_ICON[s.key] || 'file-text', 'kind') + '<strong>' + escHtml(s.label) + '</strong><span class="src">' + escHtml(source[s.key] || '') +
      '</span><span class="num">≈ ' + voiceNum(voiceTokens(s.text)) + '</span></summary><pre class="voice-code">' + escHtml(s.text.trim()) + '</pre></details>').join('') + '</div>' +
    '<div class="panel-foot"><button class="link-btn" onclick="copyVoicePrompt(this)">' + icon('copy') + 'Copy full prompt</button></div>';
  el.dataset.prompt = d.prompt;
}

async function copyVoicePrompt(button) {
  try { await navigator.clipboard.writeText(button.closest('section').dataset.prompt); showToast('Prompt copied.', 'success'); }
  catch { showToast('Could not copy. Select the text instead.', 'error'); }
}

function renderSample(el, d) {
  const c = (_voiceContacts || []).find(x => x.id === _voicePrompt.contact) || {}, sender = _voiceData.current.sender, p = d.persona;
  const words = (d.body.match(/\S+/g) || []).length, questions = (d.body.match(/\?/g) || []).length;
  el.innerHTML = '<div class="panel-head"><div><h3>Sample email</h3><p>Not queued. Nothing was sent.</p></div></div>' +
    '<div class="voice-email-head">' + facts([['From', escHtml(sender.name + (sender.email ? ' <' + sender.email + '>' : ''))],
      ['To', escHtml([c.first_name, c.last_name].filter(Boolean).join(' ') + (c.email ? ' <' + c.email + '>' : ''))],
      ['Subject', '<span class="voice-email-subject">' + escHtml(d.subject) + '</span>']]) + '</div>' +
    '<div class="voice-email">' + escHtml(d.body) + '</div>' +
    '<div class="panel-foot"><dl class="voice-email-stats"><div><dt>Words</dt><dd>' + words + '</dd></div><div><dt>Questions</dt><dd>' + questions +
      '</dd></div><div><dt>Persona</dt><dd>' + (p.unsaved ? 'unsaved' : 'v' + p.revision) + '</dd></div></dl>' +
      '<button class="btn btn-secondary btn-sm" onclick="runVoicePreview(true)">' + icon('arrow-clockwise') + 'Write again</button></div>';
}

// ── Generation ──

function renderVoiceConfig() {
  const d = _voiceData, product = d.current.product, sender = d.current.sender, offer = product.offer || {};
  const profile = voicePersona(d.default_id), text = v => escHtml(v || 'Not set');
  document.getElementById('voice-config').innerHTML = '<div class="voice-config"><div>' +
    '<section class="panel"><div class="voice-default">' + personaImage(profile, 'large') + '<div><h3>Default voice</h3>' +
      '<p>Every new draft is written in this voice. Drafts already written keep theirs.</p></div>' +
      '<select class="form-input" aria-label="Default voice" onchange="setDefaultPersona(this.value)">' + d.personas.filter(p => !p.archived).map(p =>
        '<option value="' + escAttr(p.id) + '"' + (p.id === d.default_id ? ' selected' : '') + '>' + escHtml(p.name) + ' · v' + p.revision + '</option>').join('') + '</select></div></section>' +
    '<section class="panel"><div class="panel-head"><div><h3>How emails go out</h3><p>Applies to every voice.</p></div></div><div class="panel-body">' +
      facts([['Provider', text(d.email.provider)], ['Before sending', d.email.require_approval ? 'Each draft waits in the Outbox for you' : 'Sent automatically'],
        ['Follow-ups', d.email.auto_approve_followups ? 'Approved with the first email' : 'Reviewed one by one'],
        ['Daily limit', '<span class="mono">' + voiceNum(d.email.max_daily_sends) + '</span> emails']]) + '</div></section></div>' +
    '<section class="panel"><div class="panel-head"><div><h3>What every voice knows</h3><p>The same facts, whichever voice writes.</p></div></div><div class="panel-body">' +
      facts([['Sender', text([sender.name, sender.role].filter(Boolean).join(', '))], ['Company', text(sender.company)], ['Email', text(sender.email)],
        ['Product', text(product.name)], ['Description', text(product.description)], ['Benefits', text((product.key_benefits || []).join(', '))],
        ['Pricing', text(product.pricing)], ['Offer', text(offer.primary)], ['Entry offer', text(offer.entry)], ['Goal', text(offer.goal)]]) +
      '<details class="voice-details"><summary>Market language</summary><pre class="voice-code">' + escHtml(JSON.stringify(d.markets, null, 2)) + '</pre></details></div>' +
      '<div class="panel-foot"><span>Read from <span class="mono">' + escHtml(d.config_file) + '</span>. Restart the agent after editing it.</span></div></section></div>';
}

async function setDefaultPersona(id) {
  const res = await postJSON('/api/personas/' + encodeURIComponent(id) + '/default');
  if (!res.ok) { showToast(res.error, 'error'); renderVoiceConfig(); return; }
  showToast('New drafts now use ' + voicePersona(id).name + '.', 'success');
  await loadPersonas(_personaEdit && _personaEdit.id);
}

// ── Email history drawer (opened from the Outbox) ──

async function showGenerationHistory(id) {
  const request = ++_historyRequest;
  openDrawer('Email history', 'Loading email…', '', '<p class="voice-help">Loading generation history…</p>');
  _drawerCtx = {type: 'generation', id};
  const res = await getJSON('/api/outbox/' + encodeURIComponent(id) + '/generation-history');
  if (!drawerOpen() || request !== _historyRequest || _drawerCtx?.id !== id) return;
  if (!res.ok) { openDrawer('Email history', 'Could not load email', '', unavailableState(res, 'envelope-simple', 'Email history')); return; }
  const d = res.data, item = d.email;
  const html = '<p class="voice-help">' + (item.status === 'sent' ? 'Final sent email' : 'Current draft') + (item.manually_edited ? ' · Edited before sending' : '') + '</p>' +
    '<h4>' + escHtml(item.subject) + '</h4><div class="voice-email">' + escHtml(item.body) + '</div>' +
    (d.generations.length ? d.generations.map(g => '<div class="voice-history"><div class="persona-heading">' + personaImage(g.persona) +
      '<div><strong>' + escHtml(g.persona.name) + ' · ' + (g.persona.unsaved ? 'unsaved edits of v' : 'v') + g.persona.revision + '</strong><p class="voice-help">' + formatDate(parseUTC(g.created_at)) + ' · ' + escHtml(g.task.replace(/_/g, ' ')) + (g.id === item.generation_id ? ' · Current generation' : ' · Previous generation') + '</p></div></div>' +
      (g.instruction ? '<p class="voice-copy">Instruction: ' + escHtml(g.instruction) + '</p>' : '') +
      '<details class="voice-details"><summary>Original generated draft</summary><h4>' + escHtml(g.original_subject) + '</h4><div class="voice-email">' + escHtml(g.original_body) + '</div></details>' +
      '<details class="voice-details"><summary>Exact prompt used</summary><pre class="voice-code">' + escHtml(g.prompt) + '</pre></details></div>').join('') :
      '<div class="voice-history"><p class="voice-copy">Unknown persona. This email has no saved generation history. Historical prompts cannot be reconstructed from today’s settings.</p></div>');
  openDrawer('Email history', item.to_email, badge(item.status) + ' ' + escHtml(item.mailbox || ''), html);
}
