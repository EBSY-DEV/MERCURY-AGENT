let _voiceData = null, _voiceView = 'profiles', _personaEdit = null;
let _voiceContacts = [], _voiceVersions = [], _voiceDirty = false, _voiceBusy = false;
let _voiceRequest = 0, _historyRequest = 0;

function personaImage(p, extra = '') {
  return '<img class="persona-avatar ' + extra + '" src="' + escAttr(p.avatar_url || '/static/avatars/' + p.avatar_seed + '.svg') + '" alt="">';
}

function personaChip(item) {
  const p = item.writing_persona;
  return '<button class="persona-chip" onclick="showGenerationHistory(\'' + escHtml(item.id) + '\')" title="View email and generation history">' +
    (p ? personaImage(p, 'tiny') : icon('question')) +
    '<span>' + (p ? escHtml(p.name) + ' · v' + Number(p.revision) : 'Unknown persona') + '</span></button>';
}

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
  loading.hidden = true;
  const id = selectedId || (_personaEdit && _personaEdit.id) || _voiceData.default_id;
  _personaEdit = {...(_voiceData.personas.find(p => p.id === id) || _voiceData.personas[0])};
  _voiceDirty = false;
  renderPersonaList(); renderPersonaEditor(); renderVoiceConfig();
  voiceView(_voiceView, true);
}

async function voiceView(view, force = false) {
  if (_voiceBusy && !force) return;
  if (!force && _voiceDirty && !await confirmModal({title:'Discard unsaved changes?', copy:'Save your persona before switching views, or discard these edits.', ok:'Discard changes'})) return;
  if (!force && _voiceDirty) {
    _personaEdit = {...(_voiceData.personas.find(p => p.id === _personaEdit.id) || _voiceData.personas.find(p => p.id === _voiceData.default_id))};
    _voiceDirty = false; renderPersonaEditor();
  }
  _voiceView = view;
  document.querySelectorAll('[data-voice-view]').forEach(b => {
    b.classList.toggle('active', b.dataset.voiceView === view);
    b.setAttribute('aria-pressed', String(b.dataset.voiceView === view));
  });
  document.querySelectorAll('.voice-view').forEach(el => el.hidden = el.id !== 'voice-' + view || !_voiceData);
  if (view === 'prompt' && _voiceData) renderVoicePrompt();
}

function renderPersonaList() {
  document.getElementById('persona-list').innerHTML = _voiceData.personas.map((p, i) =>
    '<button class="persona-row ' + (p.id === _personaEdit.id ? 'selected ' : '') + (p.archived ? 'archived' : '') +
      '" onclick="selectPersona(' + i + ')" aria-pressed="' + (p.id === _personaEdit.id) + '">' + personaImage(p) +
      '<div><strong>' + escHtml(p.name) + '</strong><p>' + escHtml(p.description || p.tone) + '</p>' +
      '<small>v' + p.revision + (p.id === _voiceData.default_id ? ' · Default for new drafts' : '') + (p.archived ? ' · Archived' : '') + '</small></div></button>'
  ).join('');
}

async function selectPersona(i) {
  if (_voiceBusy) return;
  if (_voiceDirty && !await confirmModal({title:'Discard unsaved changes?', copy:'This persona has changes you haven’t saved.', ok:'Discard changes'})) return;
  _personaEdit = {..._voiceData.personas[i]}; _voiceDirty = false;
  renderPersonaList(); renderPersonaEditor();
}

async function newPersona(copy = false) {
  if (!_voiceData || _voiceBusy) return;
  if (_voiceDirty && !await confirmModal({title:'Discard unsaved changes?', copy:'This persona has changes you haven’t saved.', ok:'Discard changes'})) return;
  const source = copy ? {..._personaEdit} : {name:'', description:'', tone:'', instructions:'', examples:''};
  _personaEdit = {...source, id:'', version_id:'', revision:0, archived:false,
    name:copy ? source.name + ' copy' : '', avatar_seed:copy ? source.avatar_seed : _voiceData.avatars[Math.floor(Math.random() * _voiceData.avatars.length)].seed};
  delete _personaEdit.avatar_url;
  _voiceDirty = false; _voiceView = 'profiles';
  renderPersonaList(); renderPersonaEditor(); voiceView('profiles', true);
  document.getElementById('voice-name').focus();
}

function voiceField(id, label, value, rows, placeholder = '') {
  const attrs = ' class="form-input" id="voice-' + id + '" oninput="_voiceDirty=true" ' + 'placeholder="' + escAttr(placeholder) + '"';
  return '<div class="form-group"><label class="form-label" for="voice-' + id + '">' + label + '</label>' +
    (rows ? '<textarea' + attrs + ' rows="' + rows + '">' + escHtml(value) + '</textarea>'
      : '<input' + attrs + ' value="' + escAttr(value) + '">') + '</div>';
}

function renderPersonaEditor() {
  const p = _personaEdit;
  if (!p) return;
  const existing = !!p.id, isDefault = p.id === _voiceData.default_id;
  document.getElementById('persona-editor').innerHTML = '<form class="card voice-editor" id="voice-form" onsubmit="savePersona(event)">' +
    '<div class="persona-heading"><div id="voice-avatar">' + personaImage(p, 'large') + '</div><div><h3>' +
      (existing ? escHtml(p.name) : 'A new writing voice') + '</h3><p class="voice-copy">' +
      (existing ? 'Version ' + p.revision + (isDefault ? ' · Default for new drafts' : '') : 'Give it a name, a tone, and a little character.') + '</p>' +
      '<button class="btn btn-secondary btn-sm" type="button" onclick="shufflePersonaAvatar()"' + (p.archived ? ' disabled' : '') + '>Shuffle avatar</button></div></div>' +
    '<details class="voice-details"><summary>Choose a Critter</summary><div class="avatar-picker">' + _voiceData.avatars.map((a,i) =>
      '<button type="button" class="avatar-choice ' + (a.seed === p.avatar_seed ? 'selected' : '') + '" onclick="pickPersonaAvatar(' + i + ')" aria-label="Choose Critter ' + (i+1) + '" aria-pressed="' + (a.seed === p.avatar_seed) + '"><img src="' + escAttr(a.url) + '" alt=""></button>').join('') + '</div></details>' +
    '<fieldset' + (p.archived ? ' disabled' : '') + ' style="border:0;padding:0;margin:0">' +
    voiceField('name', 'Persona name', p.name, 0, 'Warm & Local') +
    voiceField('description', 'Description', p.description, 0, 'When should Mercury use this voice?') +
    voiceField('tone', 'Tone', p.tone, 2, 'Warm, respectful, direct. Short sentences and familiar words.') +
    voiceField('instructions', 'Writing preferences', p.instructions, 4, 'Describe its rhythm, opening style, and how it asks questions.') +
    '<p class="voice-help">Your preferences work within the shared email rules and the contact’s market language.</p>' +
    voiceField('examples', 'Example writing', p.examples, 4, 'Add examples that capture the voice. Mercury follows the style and uses the contact’s own facts.') + '</fieldset>' +
    '<div id="voice-save-error" class="voice-error" role="alert" hidden></div>' +
    '<p class="voice-help">Voice edits create a new version. Names and avatars can change without changing the voice. Existing emails keep their generation history.</p>' +
    '<div class="voice-editor-footer">' + (!p.archived ? '<button class="btn btn-primary" id="voice-save" type="submit">' + (existing ? 'Save changes' : 'Create persona') + '</button>' : '') +
      (existing && !isDefault && !p.archived ? '<button class="btn btn-secondary" type="button" onclick="personaAction(\'default\')">Set as default</button>' : '') +
      (existing ? '<button class="btn btn-secondary" type="button" onclick="newPersona(true)">Duplicate</button>' : '') +
      '<span class="spacer"></span>' + (existing && !isDefault ? '<button class="btn btn-secondary btn-sm" type="button" onclick="personaAction(\'archive\')">' + (p.archived ? 'Restore' : 'Archive') + '</button>' : '') + '</div>' +
    (existing ? '<details class="voice-details" ontoggle="if(this.open) loadPersonaVersions()"><summary>Version history</summary><div id="voice-version-history"></div></details>' : '') + '</form>';
}

function pickPersonaAvatar(i) {
  if (_personaEdit.archived || _voiceBusy) return;
  _personaEdit.avatar_seed = _voiceData.avatars[i].seed; delete _personaEdit.avatar_url; _voiceDirty = true;
  document.getElementById('voice-avatar').innerHTML = personaImage(_personaEdit, 'large');
  document.querySelectorAll('.avatar-choice').forEach((button,n) => {
    button.classList.toggle('selected', n === i); button.setAttribute('aria-pressed', String(n === i));
  });
}

function shufflePersonaAvatar() {
  const choices = _voiceData.avatars.map((a,i) => i).filter(i => _voiceData.avatars[i].seed !== _personaEdit.avatar_seed);
  pickPersonaAvatar(choices[Math.floor(Math.random() * choices.length)]);
}

async function savePersona(event) {
  event.preventDefault();
  if (_voiceBusy) return;
  const data = {avatar_seed:_personaEdit.avatar_seed, expected_revision:_personaEdit.revision || null};
  for (const key of ['name','description','tone','instructions','examples']) data[key] = document.getElementById('voice-' + key).value.trim();
  const error = document.getElementById('voice-save-error');
  if (!data.name || !data.tone) { error.textContent = 'Add a persona name and tone before saving.'; error.hidden = false; return; }
  _voiceBusy = true;
  const button = document.getElementById('voice-save'); button.disabled = true; button.textContent = 'Saving…';
  const path = _personaEdit.id ? '/api/personas/' + encodeURIComponent(_personaEdit.id) + '/save' : '/api/personas';
  const res = await postJSON(path, data);
  if (!res.ok) { _voiceBusy = false; error.textContent = res.error; error.hidden = false; button.disabled = false; button.textContent = 'Save changes'; return; }
  showToast('Persona saved.', 'success'); await loadPersonas(res.data.id); _voiceBusy = false;
}

async function personaAction(action) {
  if (_voiceBusy) return;
  if (_voiceDirty) { showToast('Save your changes first.', 'error'); return; }
  _voiceBusy = true;
  const res = await postJSON('/api/personas/' + encodeURIComponent(_personaEdit.id) + '/' + action,
    action === 'archive' ? {archived:!_personaEdit.archived} : undefined);
  if (!res.ok) { _voiceBusy = false; showToast(res.error, 'error'); return; }
  showToast(action === 'default' ? 'Default updated for future drafts.' : 'Persona updated.', 'success');
  await loadPersonas(_personaEdit.id); _voiceBusy = false;
}

async function loadPersonaVersions() {
  const id = _personaEdit.id;
  const res = await getJSON('/api/personas/' + encodeURIComponent(id) + '/versions');
  if (id !== _personaEdit.id) return;
  const el = document.getElementById('voice-version-history');
  if (el) el.innerHTML = res.ok ? res.data.versions.map(v => '<div class="voice-version"><strong>v' + v.revision +
    '</strong> <span class="muted">' + formatDate(parseUTC(v.created_at)) + '</span><p>' + escHtml(v.tone) + '</p>' +
    (v.instructions ? '<p>' + escHtml(v.instructions) + '</p>' : '') + '</div>').join('') : offlineState();
}

function renderVoiceConfig() {
  const d = _voiceData, c = d.current, sender = c.sender, product = c.product;
  const profile = d.personas.find(p => p.id === d.default_id);
  const pair = (label, value) => [escHtml(label), '<span class="voice-config-value">' + escHtml(value || 'Not configured') + '</span>'];
  document.getElementById('voice-config').innerHTML = '<div class="card"><h3>Used for new email drafts</h3><p class="voice-help">Persona changes apply on the next generation. Existing drafts retain their voice.</p>' +
    facts([pair('Default persona', profile.name + ' · v' + profile.revision), pair('Tone', profile.tone),
      pair('Sender', sender.name + ' · ' + sender.role), pair('Company', sender.company), pair('Email', sender.email),
      pair('Product', product.name), pair('Description', product.description), pair('Benefits', (product.key_benefits || []).join('\n')),
      pair('Pricing', product.pricing), pair('Offer', (product.offer || {}).primary), pair('Entry offer', (product.offer || {}).entry),
      pair('Goal', (product.offer || {}).goal), pair('Provider', d.email.provider),
      pair('Approval', d.email.require_approval ? 'Review before sending' : 'Automatic sending'), pair('Daily send limit', String(d.email.max_daily_sends))]) +
    '<details class="voice-details"><summary>Market language settings</summary><p class="voice-copy">Market settings guide the draft’s language. The Writer prompt also defines regional register and its fallback.</p><pre class="voice-code">' + escHtml(JSON.stringify(d.markets, null, 2)) + '</pre></details>' +
    '<p class="voice-help">Business settings are read from ' + escHtml(d.config_file) + '. Restart the agent after editing that file.</p></div>';
}

async function renderVoicePrompt() {
  const el = document.getElementById('voice-prompt');
  const active = _voiceData.personas.filter(p => !p.archived);
  const selected = (_personaEdit && !_personaEdit.archived && _personaEdit.id) || _voiceData.default_id;
  el.innerHTML = '<div class="card"><h3>See what Mercury will write</h3><p class="voice-help">Choose a saved persona and a contact. Inspecting the prompt makes no generation call. A preview writes a sample and never queues an email.</p>' +
    '<div class="voice-two"><div class="form-group"><label class="form-label" for="voice-prompt-persona">Persona</label><select class="form-input" id="voice-prompt-persona" onchange="voicePromptPersonaChanged()">' + active.map(p => '<option value="' + escAttr(p.id) + '"' + (p.id === selected ? ' selected' : '') + '>' + escHtml(p.name) + '</option>').join('') +
    '</select></div><div class="form-group"><label class="form-label" for="voice-prompt-version">Version</label><select class="form-input" id="voice-prompt-version" onchange="clearVoicePreview()"></select></div></div>' +
    '<div class="form-group"><label class="form-label" for="voice-prompt-contact">Contact</label><select class="form-input" id="voice-prompt-contact" onchange="clearVoicePreview()"><option value="">Loading contacts…</option></select></div>' +
    '<div class="form-group"><label class="form-label" for="voice-prompt-instruction">Optional writing instruction</label><input class="form-input" id="voice-prompt-instruction" placeholder="For example: use a warmer opening" oninput="clearVoicePreview()"></div>' +
    '<div class="btn-group"><button class="btn btn-secondary" id="voice-inspect" onclick="runVoicePreview(false)" disabled>Inspect assembled prompt</button><button class="btn btn-primary" id="voice-generate" onclick="runVoicePreview(true)" disabled>Generate sample email</button></div>' +
    '<div id="voice-preview-result" aria-live="polite"></div></div>' +
    '<div class="card"><h3>Shared writing instructions</h3><p class="voice-help">Each generation combines the template, knowledge, saved voice, contact facts, market language, and any writing instruction.</p>' +
    '<details class="voice-details"><summary>Writer prompt template</summary><pre class="voice-code">' + escHtml(_voiceData.template) + '</pre></details>' +
    '<details class="voice-details"><summary>Knowledge included with the prompt</summary><pre class="voice-code">' + escHtml(_voiceData.knowledge || 'No additional knowledge files are available.') + '</pre></details></div>';
  await voicePromptPersonaChanged();
  const res = await getJSON('/api/prospects');
  if (_voiceView !== 'prompt') return;
  _voiceContacts = res.ok ? res.data : [];
  const select = document.getElementById('voice-prompt-contact');
  select.innerHTML = _voiceContacts.length ? _voiceContacts.map(p => '<option value="' + escAttr(p.id) + '">' + escHtml(([p.first_name,p.last_name].filter(Boolean).join(' ') || p.email || p.company || 'Unnamed contact') + (p.company ? ' · ' + p.company : '')) + '</option>').join('') : '<option value="">' + (res.ok ? 'Add a contact to preview an email' : 'Could not load contacts. Reopen this view to retry.') + '</option>';
  document.getElementById('voice-inspect').disabled = document.getElementById('voice-generate').disabled = !_voiceContacts.length || !_voiceVersions.length;
}

function clearVoicePreview() {
  const el = document.getElementById('voice-preview-result'); if (el) el.innerHTML = '';
}

async function voicePromptPersonaChanged() {
  clearVoicePreview();
  const select = document.getElementById('voice-prompt-persona'), id = select.value;
  document.getElementById('voice-prompt-version').innerHTML = '<option value="">Loading versions…</option>';
  document.getElementById('voice-inspect').disabled = document.getElementById('voice-generate').disabled = true;
  const res = await getJSON('/api/personas/' + encodeURIComponent(id) + '/versions');
  if (document.getElementById('voice-prompt-persona')?.value !== id) return;
  _voiceVersions = res.ok ? res.data.versions : [];
  document.getElementById('voice-prompt-version').innerHTML = _voiceVersions.length ? _voiceVersions.map(v => '<option value="' + escAttr(v.id) + '">v' + v.revision + ' · ' + escHtml(v.tone.slice(0,60)) + '</option>').join('') : '<option value="">Could not load versions. Select the persona to retry.</option>';
  document.getElementById('voice-inspect').disabled = document.getElementById('voice-generate').disabled = !document.getElementById('voice-prompt-contact').value || !_voiceVersions.length || _voiceBusy;
}

async function runVoicePreview(generate) {
  const version = document.getElementById('voice-prompt-version').value;
  const contact = document.getElementById('voice-prompt-contact').value;
  if (!version || !contact || _voiceBusy) return;
  const request = ++_voiceRequest;
  _voiceBusy = true;
  const controls = ['voice-prompt-persona','voice-prompt-version','voice-prompt-contact','voice-prompt-instruction','voice-inspect','voice-generate'].map(id => document.getElementById(id));
  controls.forEach(el => el.disabled = true);
  const result = document.getElementById('voice-preview-result');
  result.innerHTML = '<p class="voice-help" role="status">' + (generate ? 'Writing your sample email…' : 'Assembling the prompt…') + '</p>';
  const res = await postJSON('/api/personas/' + (generate ? 'preview' : 'prompt'), {version_id:version, prospect_id:contact, instruction:document.getElementById('voice-prompt-instruction').value.trim()});
  _voiceBusy = false;
  controls.forEach(el => el.disabled = false);
  if (request !== _voiceRequest || !document.contains(result)) return;
  if (!res.ok) { result.innerHTML = '<p class="voice-error" role="alert">' + escHtml(res.error) + '</p>'; return; }
  const d = res.data;
  result.innerHTML = '<div class="voice-sample"><div class="persona-heading">' + personaImage(d.persona) + '<div><strong>' + escHtml(d.persona.name) + ' · v' + d.persona.revision + '</strong><p class="voice-help">' + (generate ? 'Sample only · not queued for sending' : 'Assembled prompt · no email generated') + '</p></div></div>' +
    (generate ? '<h4>' + escHtml(d.subject) + '</h4><div class="voice-email">' + escHtml(d.body) + '</div>' : '') +
    '<details class="voice-details"' + (generate ? '' : ' open') + '><summary>Exact assembled prompt</summary><pre class="voice-code long">' + escHtml(d.prompt) + '</pre></details></div>';
}

async function showGenerationHistory(id) {
  const request = ++_historyRequest;
  openDrawer('Email history', 'Loading email…', '', '<p class="voice-help">Loading generation history…</p>');
  _drawerCtx = {type:'generation', id};
  const res = await getJSON('/api/outbox/' + encodeURIComponent(id) + '/generation-history');
  if (!drawerOpen() || request !== _historyRequest || _drawerCtx?.id !== id) return;
  if (!res.ok) { openDrawer('Email history', 'Could not load email', '', unavailableState(res, 'envelope-simple', 'Email history')); return; }
  const d = res.data, item = d.email;
  const html = '<p class="voice-help">' + (item.status === 'sent' ? 'Final sent email' : 'Current draft') + (item.manually_edited ? ' · Edited before sending' : '') + '</p>' +
    '<h4>' + escHtml(item.subject) + '</h4><div class="voice-email">' + escHtml(item.body) + '</div>' +
    (d.generations.length ? d.generations.map((g,i) => '<div class="voice-history"><div class="persona-heading">' + personaImage(g.persona) +
      '<div><strong>' + escHtml(g.persona.name) + ' · v' + g.persona.revision + '</strong><p class="voice-help">' + formatDate(parseUTC(g.created_at)) + ' · ' + escHtml(g.task.replace(/_/g,' ')) + (g.id === item.generation_id ? ' · Current generation' : ' · Previous generation') + '</p></div></div>' +
      (g.instruction ? '<p class="voice-copy">Instruction: ' + escHtml(g.instruction) + '</p>' : '') +
      '<details class="voice-details"><summary>Original generated draft</summary><h4>' + escHtml(g.original_subject) + '</h4><div class="voice-email">' + escHtml(g.original_body) + '</div></details>' +
      '<details class="voice-details"><summary>Exact prompt used</summary><pre class="voice-code long">' + escHtml(g.prompt) + '</pre></details></div>').join('') :
      '<div class="voice-history"><p class="voice-copy">Unknown persona. This email has no saved generation history. Historical prompts cannot be reconstructed from today’s settings.</p></div>');
  openDrawer('Email history', item.to_email, badge(item.status) + ' ' + escHtml(item.mailbox || ''), html);
}
