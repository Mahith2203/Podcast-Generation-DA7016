'use strict';
/* Podcast Studio UI - vanilla JS, no build step. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const INDIC = /[؀-ۿऀ-෿᱐-᱿ꯀ-꯿]/;
const fmt = (sec) => {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
};
const ACTIVE = new Set(['queued', 'running']);
const BUSY_STATUS = new Set(['ingesting', 'planning', 'writing', 'voicing', 'assembling']);

const S = {
  config: null, projects: [], pid: null, project: null, job: null, pollTimer: null,
  sigs: {}, lines: [], audioKey: null, newTab: 'file', file: null,
  hasVideo: false,                         // true only when the backend knows about video (older backends do not)
  no3d: false,                             // set when this browser cannot run the 3D hosts (no WebGL): a plain name badge is shown instead
  view: '', detail: {}, homeTimer: null, homeTicks: 0, loadingDetails: false,
  filter: 'all', q: '', lang: '', polls: 0, curLine: null,
  ask: null,                               // set while an answer from "Ask the hosts" is being performed on the stage (see extras.js)
};
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const TITLE = 'Podcast Studio';

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let d = r.statusText;
    try {
      const j = await r.json();
      d = Array.isArray(j.detail) ? (j.detail[0]?.msg || JSON.stringify(j.detail)) : (j.detail || d);
    } catch (_) { /* not JSON */ }
    throw new Error(d);
  }
  return (r.headers.get('content-type') || '').includes('json') ? r.json() : r;
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, err ? 7000 : 3500);
}

const langName = (code) => S.config.languages.find((l) => l.code === code)?.name || code;
const isActive = () => S.job && ACTIVE.has(S.job.status);
const VBUSY = new Set(['directing', 'rendering']);
const isBusy = (p) => BUSY_STATUS.has(p.status) || VBUSY.has(p.video?.status);

/* ---------------------------------------------------------------- boot / routing */
async function init() {
  try {
    S.config = await api('/api/config');
  } catch (e) {
    $('#main').innerHTML = `<div class="notice err">Could not reach the server: ${esc(e.message)}</div>`;
    return;
  }
  S.hasVideo = !!S.config.defaults && 'make_video' in S.config.defaults;
  $('#mock-badge').hidden = !S.config.mock;
  $('#btn-new').onclick = () => { location.hash = '#/new'; };
  $('#btn-home').onclick = () => { location.hash = '#/home'; };
  $('#btn-doctor').onclick = showDoctor;
  $('#btn-keys').onclick = showKeys;
  $('#btn-tuning').onclick = showTuning;
  $('#project-list').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pid]');
    if (b) location.hash = '#/p/' + b.dataset.pid;
  });
  setupPlayer();
  setupKeys();
  window.addEventListener('hashchange', route);
  await refreshList();
  route();
}

async function refreshList() {
  try { S.projects = (await api('/api/projects')).projects; } catch (_) { /* keep old */ }
  renderSidebar();
}

function setView(v) { S.view = v; document.body.dataset.view = v; delete document.documentElement.dataset.boot; }

function route() {
  const h = location.hash;
  window.Extras?.unmount?.();
  stopPoll();
  stopHome();
  stopWelcome();
  resetStage();            // the 3D hosts must never keep running behind another page
  document.title = TITLE;
  if (h.startsWith('#/p/')) openProject(decodeURIComponent(h.slice(4)));
  else if (h === '#/new') showNew();
  else if (h === '#/home') showHome();
  else showWelcome();
  $('#main').focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function renderSidebar() {
  $('#btn-home')?.setAttribute('aria-current', S.view === 'home' ? 'page' : 'false');
  const el = $('#project-list');
  if (!S.projects.length) { el.innerHTML = '<div class="empty-list">No episodes yet. Create your first one.</div>'; return; }
  el.innerHTML = S.projects.map((p) => {
    const cls = p.status === 'ready' ? 'ready' : p.status === 'error' ? 'error' : BUSY_STATUS.has(p.status) ? 'run' : p.status;
    return `<button class="pitem" data-pid="${esc(p.id)}" ${p.id === S.pid ? 'aria-current="page"' : ''}>
      <span class="t">${esc(p.title || 'Untitled')}</span>
      <span class="m"><span class="dot ${cls}"></span>${esc(statusLabel(p.status))} · ${esc(langName(p.language))}${p.duration ? ' · ' + fmt(p.duration) : ''}</span></button>`;
  }).join('');
}

function statusLabel(s) {
  return ({ created: 'Starting', ingesting: 'Reading', planning: 'Planning', writing: 'Writing', script_ready: 'Script ready', voicing: 'Recording',
    assembling: 'Mixing', ready: 'Ready', error: 'Needs attention', interrupted: 'Paused' })[s] || s;
}

/* ---------------------------------------------------------------- new episode */
function defaultVoices(lang) {
  const home = lang !== 'en' && S.config.languages.some((l) => l.code === lang) ? lang : 'hi';
  const v = S.config.voices;
  const f = v.find((x) => x.lang === home && x.gender === 'female')?.name || 'Kavya';
  const m = v.find((x) => x.lang === home && x.gender === 'male')?.name || 'Amit';
  return [f, m];
}

function voiceOptions(sel) {
  const groups = {};
  S.config.voices.forEach((v) => { (groups[v.lang] ||= []).push(v); });
  return Object.keys(groups).map((l) => `<optgroup label="${esc(langName(l))}">` +
    groups[l].map((v) => `<option value="${esc(v.name)}" ${v.name === sel ? 'selected' : ''}>${esc(v.name)} (${v.gender})</option>`).join('') + '</optgroup>').join('');
}

function showNew() {
  setView('new'); S.pid = null; S.project = null; S.job = null; S.sigs = {};
  hidePlayer();
  renderSidebar();
  const c = S.config, d = c.defaults;
  const [fa, fb] = defaultVoices('en');
  const missing = c.missing_keys || [];
  $('#main').innerHTML = `
  <h1>New episode</h1>
  <p class="hint" style="margin:6px 0 18px">Turn a textbook chapter into a two-host podcast. Everything is checked against your chapter, and you can redo any segment afterwards.</p>
  ${missing.length ? `<div class="notice err" style="margin-bottom:14px">Missing: <b>${missing.map(esc).join(', ')}</b>. <button class="link" id="nv-doctor" type="button">Add your keys now</button> (no restart needed), or switch on demo mode there to try the app offline.</div>` : ''}
  <form id="new-form" class="stack" autocomplete="off">
    <section class="panel stack" aria-labelledby="h-src">
      <h2 id="h-src">Chapter</h2>
      <div class="seg-tabs" role="group" aria-label="Chapter source">
        <button type="button" data-tab="file" aria-pressed="true">Upload a file</button>
        <button type="button" data-tab="text" aria-pressed="false">Paste text</button>
      </div>
      <div id="tab-file">
        <label class="drop" id="drop" for="file-in" tabindex="0">
          <div id="drop-msg"><strong>Choose a PDF, .txt or .md file</strong><br><span class="hint">or drop it here. One chapter or section works best (up to ${c.limits.max_upload_mb} MB).</span></div>
        </label>
        <input id="file-in" type="file" accept=".pdf,.txt,.md,.markdown,application/pdf,text/plain" class="sr">
      </div>
      <div id="tab-text" hidden>
        <label class="field">Chapter text<textarea id="text-in" rows="9" placeholder="Paste the chapter or section here"></textarea></label>
      </div>
      <label class="field">Episode title <span class="hint">optional; taken from the chapter if empty</span><input type="text" id="title-in" maxlength="120"></label>
    </section>

    <section class="panel stack" aria-labelledby="h-lang">
      <h2 id="h-lang">Language and hosts</h2>
      <div class="grid2">
        <label class="field">Podcast language
          <select id="lang">${c.languages.map((l) => `<option value="${l.code}" ${l.code === 'en' ? 'selected' : ''}>${esc(l.name)}${l.code !== 'en' ? ' - ' + esc(l.native) : ''}</option>`).join('')}</select>
        </label>
        <label class="field">Show name<input type="text" id="show" value="${esc(d.show_name)}" maxlength="60"></label>
      </div>
      <div id="wmode-box" hidden class="stack">
        <div>
          <div class="hint" style="font-weight:600;color:var(--ink);margin-bottom:6px">How should the script be written?</div>
          <div class="choice" role="radiogroup" aria-label="Writing mode">
            <label><input type="radio" name="wmode" value="direct" checked><span>Write directly in the language</span></label>
            <label><input type="radio" name="wmode" value="translate"><span>Write in English, then translate with Bodhan</span></label>
          </div>
          <p class="hint" id="wmode-hint" style="margin-top:6px"></p>
        </div>
        <label class="row" style="gap:8px"><input type="checkbox" id="polish"> <span>Have the language model polish the translated lines <span class="hint">(more natural, costs a little more)</span></span></label>
      </div>
      <div class="stack" id="hosts">
        ${hostRow('a', 'Host A (curious co-host)', d.host_a.name || 'Meera', fa)}
        ${hostRow('b', 'Host B (explainer)', d.host_b.name || 'Arjun', fb)}
      </div>
      <p class="hint">English has no dedicated voice, so any of the 45 voices read it with their own accent. Use Listen to compare before you commit.</p>
    </section>

    <section class="panel stack" aria-labelledby="h-len">
      <h2 id="h-len">Length and audience</h2>
      <div>
        <div class="choice" role="radiogroup" aria-label="Target length">
          <label><input type="radio" name="length" value="auto" checked><span>Match the chapter</span></label>
          <label><input type="radio" name="length" value="5"><span>About 5 min</span></label>
          <label><input type="radio" name="length" value="10"><span>About 10 min</span></label>
          <label><input type="radio" name="length" value="15"><span>About 15 min</span></label>
          <label><input type="radio" name="length" value="20"><span>About 20 min</span></label>
        </div>
      </div>
      <label class="field">Who is listening?<input type="text" id="audience" value="${esc(d.audience)}" maxlength="120"></label>
    </section>

    <section class="panel stack" aria-labelledby="h-q">
      <h2 id="h-q">Quality and cost</h2>
      <label class="row" style="gap:8px"><input type="checkbox" id="factcheck" checked> <span><b>Check every segment against the chapter</b> and fix mistakes automatically <span class="hint">(a few extra model calls)</span></span></label>
      <label class="row" style="gap:8px"><input type="checkbox" id="reviewfirst"> <span><b>Let me read the script before recording voices</b> <span class="hint">(saves voice credits if you want changes first)</span></span></label>
      <label class="row" style="gap:8px"><input type="checkbox" id="jingle" checked> <span>Add a short chime at the start and end</span></label>
      ${S.hasVideo ? '<label class="row" style="gap:8px"><input type="checkbox" id="makevideo"> <span>Also make a narrated video (slides + captions) when the audio is ready</span></label>' : ''}
      <details class="adv"><summary>Advanced</summary>
        <div class="stack" style="margin-top:12px">
          <div class="grid2">
            <label class="field">Automatic fix rounds<input type="number" id="revisions" min="0" max="4" value="${d.max_revisions}"></label>
            <label class="field">Longest voice request (words)<input type="number" id="ttsmax" min="12" max="70" value="${d.tts_max_words}"></label>
            <label class="field">Writer model <span class="hint">default: ${esc(c.models.writer)}</span><input type="text" id="wmodel" placeholder="leave empty for the default"></label>
            <label class="field">Checker model <span class="hint">default: ${esc(c.models.checker)}</span><input type="text" id="cmodel" placeholder="leave empty for the default"></label>
          </div>
          <div>
            <div class="hint" style="font-weight:600;color:var(--ink);margin-bottom:6px">Delivery styles the hosts may use</div>
            <div class="row" id="styles">${c.styles.map((s) => `<label class="row" style="gap:6px"><input type="checkbox" value="${esc(s)}" ${c.default_allowed_styles.includes(s) ? 'checked' : ''}> ${esc(s)}</label>`).join('')}</div>
          </div>
          <label class="field">Extra instructions for the writer <span class="hint">e.g. "use examples from Indian villages", "avoid jokes"</span><textarea id="extra" rows="3" maxlength="2000"></textarea></label>
        </div>
      </details>
    </section>

    <div class="row end"><button type="submit" class="btn primary" id="create-btn">Create podcast</button></div>
  </form>`;
  bindNewForm();
}

function hostRow(k, label, name, voice) {
  return `<div class="host">
    <span class="bar" style="background:var(--${k})"></span>
    <label class="field">${esc(label)}<input type="text" id="name-${k}" value="${esc(name)}" maxlength="40"></label>
    <label class="field">Voice<select id="voice-${k}">${voiceOptions(voice)}</select></label>
    <button type="button" class="btn small" data-preview="${k}">Listen</button>
  </div>`;
}

function bindNewForm() {
  const f = $('#new-form');
  $('#nv-doctor')?.addEventListener('click', showKeys);
  $$('[data-tab]', f).forEach((b) => b.addEventListener('click', () => {
    S.newTab = b.dataset.tab;
    $$('[data-tab]', f).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    $('#tab-file').hidden = S.newTab !== 'file';
    $('#tab-text').hidden = S.newTab !== 'text';
  }));
  const fin = $('#file-in'), drop = $('#drop');
  const setFile = (file) => {
    S.file = file;
    $('#drop-msg').innerHTML = file ? `<strong>${esc(file.name)}</strong><br><span class="hint">${(file.size / 1048576).toFixed(1)} MB. Click to choose another file.</span>` : '';
  };
  fin.addEventListener('change', () => setFile(fin.files[0] || null));
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fin.click(); } });
  S.file = null;

  const langSel = $('#lang');
  const onLang = () => {
    const l = S.config.languages.find((x) => x.code === langSel.value);
    const [fa, fb] = defaultVoices(l.code);
    $('#voice-a').value = fa; $('#voice-b').value = fb;
    $('#wmode-box').hidden = l.code === 'en';
    const hint = $('#wmode-hint');
    if (l.tier === 'strong') hint.textContent = `Writing directly usually sounds more natural in ${l.name}. Translating is a good alternative if you want to use the Bodhan translation model.`;
    else if (l.tier === 'moderate') hint.textContent = `Language models are less fluent in ${l.name}. Try both modes and compare; translating often helps.`;
    else hint.textContent = `${l.name} is a low-resource language for language models, so translating from English is recommended. Check the script script (${l.script}) with a native speaker.`;
    if (l.tier === 'limited') $('input[name=wmode][value=translate]').checked = true;
    else $('input[name=wmode][value=direct]').checked = true;
  };
  langSel.addEventListener('change', onLang);

  $$('[data-preview]', f).forEach((b) => b.addEventListener('click', async () => {
    const k = b.dataset.preview;
    b.disabled = true; const old = b.textContent; b.textContent = 'Loading...';
    try {
      const r = await fetch('/api/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice: $('#voice-' + k).value, lang: langSel.value }) });
      if (!r.ok) throw new Error((await r.json()).detail || 'Preview failed');
      const url = URL.createObjectURL(await r.blob());
      const a = new Audio(url); a.onended = () => URL.revokeObjectURL(url); await a.play();
    } catch (e) { toast(e.message, true); }
    b.disabled = false; b.textContent = old;
  }));

  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = $('#text-in').value.trim();
    if (S.newTab === 'file' && !S.file) { toast('Choose a file first, or switch to Paste text.', true); return; }
    if (S.newTab === 'text' && !text) { toast('Paste the chapter text first.', true); return; }
    const lang = langSel.value;
    const settings = {
      language: lang,
      writing_mode: lang === 'en' ? 'direct' : $('input[name=wmode]:checked').value,
      polish_translation: $('#polish').checked,
      minutes: $('input[name=length]:checked').value === 'auto' ? null : Number($('input[name=length]:checked').value),
      audience: $('#audience').value.trim() || undefined,
      show_name: $('#show').value.trim() || undefined,
      host_a: { name: $('#name-a').value.trim() || 'Host A', voice: $('#voice-a').value },
      host_b: { name: $('#name-b').value.trim() || 'Host B', voice: $('#voice-b').value },
      fact_check: $('#factcheck').checked,
      max_revisions: Number($('#revisions').value || 0),
      tts_max_words: Number($('#ttsmax').value || 40),
      allowed_styles: $$('#styles input:checked').map((x) => x.value),
      jingle: $('#jingle').checked,
      auto_voice: !$('#reviewfirst').checked,
      extra_instructions: $('#extra').value.trim(),
      writer_model: $('#wmodel').value.trim() || null,
      checker_model: $('#cmodel').value.trim() || null,
    };
    if (S.hasVideo) settings.make_video = $('#makevideo').checked;   // make_video: only sent to backends that know it
    const fd = new FormData();
    if (S.newTab === 'file') fd.append('file', S.file); else fd.append('text', text);
    fd.append('title', $('#title-in').value.trim());
    fd.append('settings_json', JSON.stringify(settings));
    const btn = $('#create-btn'); btn.disabled = true; btn.textContent = 'Creating...';
    try {
      const r = await api('/api/projects', { method: 'POST', body: fd });
      S.job = r.job; S.pid = r.project.id;
      await refreshList();
      location.hash = '#/p/' + r.project.id;
    } catch (err) {
      toast(err.message, true);
      btn.disabled = false; btn.textContent = 'Create podcast';
    }
  });
}

/* ---------------------------------------------------------------- project view */
async function openProject(pid) {
  setView('project'); S.pid = pid; S.sigs = {}; S.lines = []; S.audioKey = null; S.curLine = null; S.polls = 0; S.ask = null;
  resetStage();
  renderSidebar();
  $('#main').innerHTML = '<div class="hint">Loading...</div>';
  try {
    const r = await api('/api/projects/' + encodeURIComponent(pid));
    S.project = r.project; S.job = r.job;
  } catch (e) {
    $('#main').innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
    return;
  }
  $('#main').innerHTML = `<div id="p-head"></div><div id="p-progress"></div><div id="p-stage"></div>${S.hasVideo ? '<div id="p-video"></div>' : ''}<div id="p-tabs"></div><div id="tab-episode"><div id="p-timeline"></div><div id="p-segs"></div></div><div id="tab-learn" hidden></div><div id="tab-ask" hidden></div><div id="tab-quality" hidden></div>`;
  $('#p-segs').addEventListener('click', onSegClick);
  $('#p-timeline').addEventListener('click', onTimelineClick);
  $('#p-head').addEventListener('click', onHeadClick);
  $('#p-video')?.addEventListener('click', onVideoClick);
  $('#p-progress').addEventListener('click', onHeadClick);
  $('#p-stage').addEventListener('click', onStageClick);
  renderProject();
  if (isActive() || isBusy(S.project)) startPoll();
  loadExtras().then((Extras) => { if (Extras && S.view === 'project' && S.pid === pid) Extras.mount(); });
}

/* ---- study pack, Ask the hosts, quality dashboard: optional (static/extras.js + the backend's /api/extras). Without them nothing changes. */
let extrasLoad = null;
function loadExtras() {
  extrasLoad ||= (async () => {
    try { if (!(await fetch('/api/extras')).ok) return null; } catch (_) { return null; }
    return new Promise((resolve) => {
      const sc = document.createElement('script');
      sc.src = '/static/extras.js';
      sc.onload = () => resolve(window.Extras || null);
      sc.onerror = () => resolve(null);
      document.head.appendChild(sc);
    });
  })();
  return extrasLoad;
}

function startPoll() {
  stopPoll();
  S.pollTimer = setInterval(pollOnce, 1000);
}
function stopPoll() { clearInterval(S.pollTimer); S.pollTimer = null; }

async function pollOnce() {
  if (!S.pid) return stopPoll();
  try {
    const r = await api('/api/projects/' + encodeURIComponent(S.pid));
    S.project = r.project; S.job = r.job;
  } catch (_) { return; }
  renderProject();
  if (++S.polls % 5 === 0) refreshList();   // keep the sidebar's status dots fresh while working
  if (!isActive() && !isBusy(S.project)) {
    stopPoll();
    refreshList();
    if (S.job?.status === 'error' && S.job.error) toast(S.job.error, true);
  }
}

function renderProject() {
  const p = S.project;
  if (!p) return;
  const sig = (k, v, fn) => { const s = JSON.stringify(v); if (S.sigs[k] !== s) { S.sigs[k] = s; fn(); } };
  const v = p.video || null;   // undefined on backends without video
  sig('head', [p.status, p.message, p.title, p.final?.built_at, p.final?.stale, p.usage, p.source, isActive()], renderHead);
  sig('prog', [S.job?.id, S.job?.status, S.job?.progress, S.job?.message, p.status, p.message, p.log.slice(-6), p.segments.map((s) => s.status), v?.status, v?.final?.built_at], renderProgress);
  sig('stage', [p.settings.host_a, p.settings.host_b, stageShown()], buildStage);
  if (S.hasVideo && v) sig('video', [v, p.final?.built_at, p.status, isActive()], renderVideo);
  sig('tl', [p.segments.map((s) => [s.current, s.status]), p.final?.built_at], renderTimeline);
  sig('segs', [p.segments.map((s) => [s.current, s.status, s.versions.length]), p.final?.built_at, isActive(), p.settings.language, (v?.storyboards || []).map((b) => [b.index, b.version, b.created_at]), v?.final?.built_at], renderSegments);
  updateStage();
  tickElapsed();
  tabTitle();
  syncPlayer();
  window.Extras?.update?.();
  if (S.pollTimer === null && (isActive() || isBusy(p))) startPoll();
}

function tabTitle() {
  const p = S.project, j = S.job;
  document.title = isActive() && j ? `${Math.round((j.progress || 0) * 100)}% · ${p.plan?.episode_title || p.title || TITLE}` : TITLE;
}

function renderHead() {
  const p = S.project, f = p.final, s = p.settings;
  const base = `/api/projects/${encodeURIComponent(p.id)}/files/`;
  const busy = isActive();
  const acts = [];
  if (!busy && (p.status === 'error' || p.status === 'interrupted')) acts.push(`<button class="btn primary" data-head="resume">Resume</button>`);
  if (!busy && p.status === 'script_ready') acts.push(`<button class="btn primary" data-head="voice">Generate audio</button>`);
  if (f && !busy) {
    if (f.mp3) acts.push(`<a class="btn" href="${base}${f.mp3}?download=1">Download MP3</a>`);
    acts.push(`<a class="btn" href="${base}${f.wav}?download=1">WAV</a>`);
    acts.push(`<a class="btn" href="${base}${f.transcript_md}?download=1">Transcript</a>`);
    acts.push(`<a class="btn" href="${base}${f.srt}?download=1">Subtitles</a>`);
  }
  if (!busy && (f || p.status === 'script_ready')) acts.push(`<button class="btn" data-head="quality">Quality report</button>`);
  if (!busy && f) acts.push(`<button class="btn" data-head="remix" title="Apply the current Tuning (pacing, levels) to this episode. Uses no voice credits.">Re-mix</button>`);
  if (!busy) acts.push(`<button class="btn quiet danger" data-head="delete">Delete</button>`);
  const u = p.usage;
  const usage = (u.llm_calls || u.tts_calls) ? `<div class="usage">Used so far: ${u.llm_calls} model calls (${Math.round((u.llm_prompt_tokens + u.llm_completion_tokens) / 1000)}k tokens${u.llm_cost_usd ? `, about $${u.llm_cost_usd.toFixed(2)}` : ''}), ${u.tts_calls} voice requests (${u.tts_chars.toLocaleString()} characters${u.tts_cache_hits ? `, ${u.tts_cache_hits} reused` : ''})${u.mt_calls ? `, ${u.mt_calls} translations` : ''}.</div>` : '';
  const warns = (p.source.warnings || []).map((w) => `<div class="notice">${esc(w)}</div>`).join('');
  const err = (p.status === 'error' || p.status === 'interrupted') && p.message ? `<div class="notice ${p.status === 'error' ? 'err' : 'info'}">${esc(p.message)}</div>` : '';
  const stale = f?.stale ? `<div class="notice">The episode audio is out of date. <button class="link" data-head="assemble">Rebuild it</button>.</div>` : '';
  const sr = p.status === 'script_ready' ? `<div class="notice info">${esc(p.message)} You can read and edit the lines below, rewrite segments, then press Generate audio.</div>` : '';
  $('#p-head').innerHTML = `
    <div class="phead">
      <div>
        <h1>${esc(p.plan.episode_title || p.title || 'Untitled')}</h1>
        <div class="pmeta">
          <span class="badge ${p.status === 'ready' ? 'ok' : p.status === 'error' ? 'err' : (p.status === 'script_ready' || p.status === 'interrupted') ? 'warn' : ''}">${esc(statusLabel(p.status))}</span>
          <span>${esc(langName(s.language))}${s.language !== 'en' ? ` (${s.writing_mode === 'translate' ? 'translated' : 'written directly'})` : ''}</span>
          ${f ? `<span>${fmt(f.duration)} long</span>` : ''}
          <span>${p.segments.length ? p.segments.length + ' segments' : ''}</span>
          <span>${esc(p.source.filename || '')}</span>
        </div>
      </div>
      <div class="actions">${acts.join('')}</div>
    </div>
    ${warns}${err}${stale}${sr}${usage}`;
}

/* ---- live progress tracker */
const STEPS = [['ingest', 'Read the chapter'], ['plan', 'Plan the episode'], ['write', 'Write the script'], ['voice', 'Record the voices'], ['mix', 'Mix the audio']];
const SEG_LABEL = { pending: 'Waiting', writing: 'Writing', written: 'Script ready', voicing: 'Recording', ready: 'Done', error: 'Failed' };
const SVG_CHECK = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/* Where the pipeline is: index of the current step and what state it is in. */
function stepInfo(p) {
  const segs = p.segments;
  const hasPlan = segs.length > 0;
  const hasScript = hasPlan && segs.every((s) => s.versions.length);
  const hasVoice = hasScript && segs.every((s) => curVersion(s)?.duration);
  const st = p.status;
  let cur = ({ created: 0, ingesting: 0, planning: 1, writing: 2, script_ready: 3, voicing: 3, assembling: 4, ready: 5 })[st] ?? 0;
  let state = st === 'ready' ? 'done' : st === 'script_ready' ? 'wait' : 'run';
  if (st === 'error' || st === 'interrupted') {
    cur = !hasPlan ? 1 : !hasScript ? 2 : !hasVoice ? 3 : 4;
    state = st === 'error' ? 'fail' : 'wait';
  }
  return { cur, state };
}
const videoStepOn = (p) => S.hasVideo && !!p.video && (p.settings.make_video || VBUSY.has(p.video.status) || !!p.video.final);

function stepDetail(p, i) {
  const segs = p.segments, n = segs.length;
  if (i === 0) return 'Reading the text';
  if (i === 1) return n ? `${n} segments planned` : 'Splitting into segments';
  if (i === 2) { const w = segs.filter((s) => s.versions.length).length; return n ? `${w} of ${n} written` : 'Starting'; }
  if (i === 3) { const r = segs.filter((s) => s.status === 'ready').length; return n ? `${r} of ${n} recorded` : ''; }
  if (i === 4) return 'Joining and levelling';
  return 'Slides and captions';
}

function renderProgress() {
  const j = S.job, p = S.project, el = $('#p-progress');
  const active = isActive();
  const vBusy = VBUSY.has(p.video?.status);
  const resting = ['script_ready', 'error', 'interrupted'].includes(p.status);
  if (!active && !isBusy(p) && !resting) { el.innerHTML = ''; return; }

  const withVideo = videoStepOn(p);
  const steps = withVideo ? [...STEPS, ['video', 'Make the video']] : STEPS;
  let { cur, state } = stepInfo(p);
  if (vBusy) { cur = 5; state = 'run'; }
  const items = steps.map(([, label], i) => {
    let cls = 'pend';
    if (i < cur) cls = 'done';
    else if (i === cur) cls = state;
    if (withVideo && i === 5 && !vBusy && p.status === 'ready') cls = p.video.final ? 'done' : 'pend';
    const bub = cls === 'done' ? SVG_CHECK : cls === 'fail' ? '!' : String(i + 1);
    const note = cls === 'run' ? stepDetail(p, i) : cls === 'wait' ? (i === 3 && p.status === 'script_ready' ? 'Waiting for you' : 'Paused') : cls === 'fail' ? 'Stopped here' : cls === 'done' && i === 3 ? `${p.segments.length} recorded` : '';
    return `<li class="step ${cls}"${cls === 'run' ? ' aria-current="step"' : ''}><span class="bub">${bub}</span><span class="stl">${esc(label)}</span><small>${esc(note)}</small></li>`;
  }).join('');

  const pct = Math.round((j?.progress || 0) * 100);
  const title = active ? (j.message || statusLabel(p.status)) : p.status === 'script_ready' ? 'The script is ready for you to read' : p.status === 'error' ? 'This run stopped early' : 'This run was paused';
  const chips = active && p.segments.length ? `<div class="segchips" aria-label="Segments">${p.segments.map((s) => `<span class="sc ${s.status}" title="${esc(`${s.index + 1}. ${s.title}: ${SEG_LABEL[s.status] || s.status}`)}">${s.index + 1}</span>`).join('')}</div>` : '';
  const log = p.log.length ? `<details class="act"><summary>Activity</summary><pre class="logtail">${esc(p.log.slice(-6).join('\n'))}</pre></details>` : '';
  el.innerHTML = `<section class="panel pg" role="status" aria-label="Progress">
    <div class="pg-top">
      <div><div class="pg-title">${esc(title)}</div>${active ? '<div class="pg-sub" id="pg-sub"></div>' : ''}</div>
      ${active ? '<button class="btn small" data-head="cancel">Stop</button>' : ''}
    </div>
    <ol class="steps">${items}</ol>
    ${active ? `<div class="progress"><i style="width:${pct}%"></i></div>${chips}` : ''}
    ${log}
  </section>`;
}

/* "Working for 1:24, about 2 min left" - runs on every poll without redrawing the panel. */
function tickElapsed() {
  const j = S.job, el = $('#pg-sub');
  if (!el || !j || !ACTIVE.has(j.status)) return;
  const t0 = Date.parse(j.created_at);
  const secs = Number.isFinite(t0) ? Math.max(0, (Date.now() - t0) / 1000) : 0;
  let txt = `Working for ${fmt(secs)}`;
  const pr = j.progress || 0;
  if (pr > 0.08 && pr < 0.99 && secs > 15) {
    const eta = secs * (1 - pr) / pr;
    txt += eta < 45 ? ', under a minute to go' : `, roughly ${Math.max(1, Math.round(eta / 60))} min to go`;
  }
  el.textContent = txt;
}

/* ---- timeline */
function geometry() {
  const p = S.project, f = p.final;
  const segs = p.segments;
  if (f && f.chapters.length === segs.length) {
    return { total: f.duration, timed: true, items: segs.map((s, i) => {
      const v = s.version_obj, start = f.chapters[i].start;
      return { s, v, start, dur: v?.duration || 0 };
    }) };
  }
  const total = segs.reduce((a, s) => a + s.target_words, 0) || 1;
  let acc = 0;
  return { total, timed: false, items: segs.map((s) => { const it = { s, v: null, start: acc, dur: s.target_words }; acc += s.target_words; return it; }) };
}
const curVersion = (s) => s.versions.find((v) => v.version === s.current) || null;

function renderTimeline() {
  const p = S.project;
  if (!p.segments.length) { $('#p-timeline').innerHTML = ''; return; }
  p.segments.forEach((s) => { s.version_obj = curVersion(s); });
  const g = geometry();
  const pct = (x) => (x / g.total * 100).toFixed(3) + '%';
  const a = p.settings.host_a.name, b = p.settings.host_b.name;
  const blocks = g.items.map(({ s, v, start, dur }) => {
    const flag = v?.check?.needs_review ? ' flag' : '';
    let inner = '';
    if (g.timed && v?.turn_times?.length) {
      inner = v.turns.map((t, i) => {
        const [x0, x1] = v.turn_times[i] || [0, 0];
        return `<i class="tl-t ${t.speaker}${t.reaction ? ' r' : ''}" style="left:${pct(start + x0)};width:${pct(Math.max(x1 - x0, 0.15))}"></i>`;
      }).join('');
    }
    return `<div class="tl-seg${g.timed ? '' : ' pend'}${flag}" style="left:${pct(start)};width:${pct(dur)}" title="${esc(`${s.index + 1}. ${s.title}`)}">${inner}</div>`;
  }).join('');
  const labels = g.items.map(({ s, start, dur }) => `<button type="button" data-seg="${s.index}" style="left:${pct(start)};max-width:${pct(dur)}" title="${esc(s.title)}">${s.index + 1} ${esc(s.title)}</button>`).join('');
  $('#p-timeline').innerHTML = `<section class="tl" aria-label="Conversation timeline" style="margin-top:16px">
    <div class="tl-head"><h2>Who speaks when</h2>
      <div class="tl-key"><span><i style="background:var(--a)"></i>${esc(a)}</span><span><i style="background:var(--b)"></i>${esc(b)}</span></div></div>
    <div class="tl-track" id="tl-track" data-total="${g.total}" data-timed="${g.timed}">${blocks}<div class="tl-play" id="tl-play"></div></div>
    <div class="tl-labels">${labels}</div>
    ${g.timed ? '' : '<div class="hint" style="margin-top:6px">Segment widths show the planned length. Timings appear once the voices are recorded.</div>'}
  </section>`;
}

function onTimelineClick(e) {
  const lab = e.target.closest('[data-seg]');
  if (lab) {
    const i = Number(lab.dataset.seg);
    const f = S.project.final;
    if (f?.chapters[i]) playAt(f.chapters[i].start);
    $(`#seg-${i}`)?.scrollIntoView({ block: 'start' });
    return;
  }
  const tr = e.target.closest('#tl-track');
  if (tr && tr.dataset.timed === 'true') {
    const r = tr.getBoundingClientRect();
    playAt(((e.clientX - r.left) / r.width) * Number(tr.dataset.total));
  }
}

/* ---- segments */
function renderSegments() {
  const p = S.project;
  const busy = isActive();
  const f = p.final, lang = p.settings.language;
  const names = { A: p.settings.host_a.name, B: p.settings.host_b.name };
  S.lines = [];
  $('#p-segs').innerHTML = `<div class="segs">${p.segments.map((s) => segHTML(s, f, names, busy, lang)).join('')}</div>`;
  $$('#p-segs .line[data-start]').forEach((el) => {
    const si = Number(el.dataset.seg), ti = Number(el.dataset.turn);
    const turn = curVersion(p.segments[si])?.turns[ti];
    S.lines.push({
      el, start: Number(el.dataset.start), end: Number(el.dataset.end), seg: si,
      speaker: turn?.speaker || (el.classList.contains('B') ? 'B' : 'A'), react: !!turn?.reaction, text: turn?.text || '', style: turn?.style || '',
    });
  });
}

function checkBadge(v) {
  const c = v?.check;
  if (!c || c.verdict === 'unchecked') return `<span class="badge">${c?.notes ? 'Edited by hand' : 'Not checked'}</span>`;
  if (c.needs_review) return `<span class="badge warn">Needs review: ${c.issues.filter((i) => i.severity === 'major').length + c.missing_points.length} open issue(s)</span>`;
  const fixed = c.attempts > 1 ? `, fixed in ${c.attempts - 1} round${c.attempts > 2 ? 's' : ''}` : '';
  return `<span class="badge ok">Checked against the chapter${fixed}</span>`;
}

function segHTML(s, f, names, busy, lang) {
  const v = curVersion(s);
  const ch = f?.chapters?.[s.index];
  const dis = busy ? 'disabled' : '';
  let body = '';
  if (!v) {
    body = `<div class="lines"><div class="hint" style="padding:10px">${s.status === 'writing' ? 'Writing...' : s.status === 'error' ? esc(s.error || 'Failed') : 'Waiting'}</div></div>`;
  } else {
    body = `<div class="lines">${v.turns.map((t, i) => lineHTML(s, v, t, i, ch, names, dis)).join('')}</div>`;
  }
  const issues = v?.check && (v.check.issues.length || v.check.missing_points.length) ? `<details><summary>What the checker found (${v.check.issues.length + v.check.missing_points.length})</summary>
    ${v.check.missing_points.map((m) => `<div class="issue">Missing point: ${esc(m)}</div>`).join('')}
    ${v.check.issues.map((i) => `<div class="issue ${i.severity}"><b>${i.turn != null ? 'Line ' + (i.turn + 1) + ': ' : ''}${esc(i.type)}</b> (${esc(i.severity)}). ${esc(i.detail)}${i.suggested_fix ? ` <i>Fix: ${esc(i.suggested_fix)}</i>` : ''}</div>`).join('')}
    ${v.check.notes ? `<div class="hint" style="margin-top:6px">${esc(v.check.notes)}</div>` : ''}</details>` : '';
  const kp = s.key_points.length ? `<details><summary>What this segment must cover</summary><ul class="kp">${s.key_points.map((k) => `<li>${esc(k)}</li>`).join('')}</ul></details>` : '';
  const hist = s.versions.length > 1 ? `<details><summary>Versions (${s.versions.length})</summary><div class="versions">${[...s.versions].reverse().map((x) => `
      <div class="ver ${x.version === s.current ? 'cur' : ''}"><span>v${x.version} · ${esc(({ generated: 'first draft', rewrite: 'rewritten', edited: 'edited', revoiced: 'new recording' })[x.origin])}${x.instruction ? ': ' + esc(x.instruction.slice(0, 80)) : ''}</span>
      ${x.version === s.current ? '<span class="badge ok">In use</span>' : `<button class="btn small" data-act="revert" data-seg="${s.index}" data-ver="${x.version}" ${dis}>Use this</button>`}</div>`).join('')}</div></details>` : '';
  const rewrite = v ? `<div class="rewrite" id="rw-${s.index}" hidden>
      <label class="field">What should change? <span class="hint">leave empty for a fresh take</span>
        <textarea id="rw-text-${s.index}" rows="2" placeholder="e.g. Make it simpler, add an everyday example, keep it shorter"></textarea></label>
      <div class="chips">${['Make it simpler', 'Add an everyday example', 'More playful', 'Shorter and punchier', 'More back-and-forth'].map((c) => `<button type="button" class="chip" data-act="chip" data-seg="${s.index}">${c}</button>`).join('')}</div>
      <div class="row"><button class="btn primary small" data-act="rewrite" data-seg="${s.index}" ${dis}>Rewrite segment</button>
        <button class="btn small" data-act="revoice" data-seg="${s.index}" ${dis} title="Keeps the words, records them again">Same words, new recording</button></div>
    </div>` : '';
  const vis = visualsHTML(s, dis);
  return `<article class="seg ${v?.check?.needs_review ? 'flag' : ''}" id="seg-${s.index}">
    <div class="seg-h">
      <div><div class="seg-title"><span class="seg-n">${s.index + 1}</span><h3>${esc(s.title)}</h3></div>
        <div class="seg-sub"><span>${({ intro: 'Intro', body: 'Segment', recap: 'Recap', outro: 'Outro' })[s.kind]}</span>${v?.duration ? `<span>${fmt(v.duration)}</span>` : ''}${v ? `<span>${v.turns.reduce((n, t) => n + t.text.split(/\s+/).filter(Boolean).length, 0)} words</span>` : ''}${v ? checkBadge(v) : ''}</div></div>
      <div class="seg-acts">
        ${ch ? `<button class="btn small" data-act="play-seg" data-seg="${s.index}">Play</button>` : ''}
        ${v ? `<button class="btn small" data-act="toggle-rw" data-seg="${s.index}">Rewrite...</button>` : ''}
      </div>
    </div>
    ${rewrite}${body}
    ${(issues || kp || hist || vis) ? `<div class="seg-foot">${issues}${kp}${hist}${vis}</div>` : ''}
  </article>`;
}

function lineHTML(s, v, t, i, ch, names, dis) {
  const tt = v.turn_times?.[i];
  const timing = ch && tt ? ` data-start="${(ch.start + tt[0]).toFixed(2)}" data-end="${(ch.start + tt[1]).toFixed(2)}"` : '';
  const script = INDIC.test(t.text) ? 'indic' : 'latin';
  return `<div class="line ${t.speaker}${t.reaction ? ' react' : ''}" id="ln-${s.index}-${i}"${timing} data-seg="${s.index}" data-turn="${i}">
    <div class="who">${esc(names[t.speaker])}</div>
    <div class="txt" data-script="${script}">${esc(t.text)}${t.style ? `<span class="tag">${esc(t.style)}</span>` : ''}${t.edited ? '<span class="tag edited">edited</span>' : ''}</div>
    <div class="acts">
      ${timing ? `<button class="ibtn" data-act="play-line" data-seg="${s.index}" data-turn="${i}" aria-label="Play line ${i + 1}">Play</button>` : ''}
      <button class="ibtn" data-act="edit-line" data-seg="${s.index}" data-turn="${i}" ${dis} aria-label="Edit line ${i + 1}">Edit</button>
    </div>
    ${t.text_en ? `<div class="en">English: ${esc(t.text_en)}</div>` : ''}
  </div>`;
}

async function onSegClick(e) {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  const act = b.dataset.act, si = Number(b.dataset.seg), ti = Number(b.dataset.turn);
  const p = S.project, f = p.final;
  if (act === 'play-seg') return f?.chapters[si] && playAt(f.chapters[si].start);
  if (act === 'play-line') { const l = $(`#ln-${si}-${ti}`); return l && playAt(Number(l.dataset.start)); }
  if (act === 'toggle-rw') { const r = $(`#rw-${si}`); r.hidden = !r.hidden; if (!r.hidden) $(`#rw-text-${si}`).focus(); return; }
  if (act === 'chip') { const t = $(`#rw-text-${si}`); t.value = (t.value ? t.value.trim() + '. ' : '') + b.textContent; return; }
  if (act === 'edit-line') return startEdit(si, ti);
  if (act === 'cancel-edit') { S.sigs.segs = null; return renderSegments(); }
  if (act === 'save-edit') {
    const text = $('#ed-text').value.trim(), style = $('#ed-style').value || null;
    const t = curVersion(p.segments[si]).turns[ti];
    if (!text) return toast('The line cannot be empty.', true);
    const body = { turn: ti };
    if (text !== t.text) body.text = text;
    if (style !== (t.style || null)) body.style = style || '';
    if (body.text === undefined && body.style === undefined) { S.sigs.segs = null; return renderSegments(); }
    return runAction(`/api/projects/${p.id}/segments/${si}/edit-turn`, body, 'Updating the line and rebuilding the episode...');
  }
  if (act === 'rewrite') return runAction(`/api/projects/${p.id}/segments/${si}/regenerate`, { mode: 'rewrite', instruction: $(`#rw-text-${si}`).value.trim() || null }, 'Rewriting the segment...');
  if (act === 'revoice') return runAction(`/api/projects/${p.id}/segments/${si}/regenerate`, { mode: 'revoice' }, 'Recording the segment again...');
  if (act === 'toggle-vr') { const r = $(`#vr-${si}`); r.hidden = !r.hidden; if (!r.hidden) $(`#vr-text-${si}`).focus(); return; }
  if (act === 'vchip') { const t = $(`#vr-text-${si}`); t.value = (t.value ? t.value.trim() + '. ' : '') + b.textContent; return; }
  if (act === 'redo-visuals') return runAction(`/api/projects/${p.id}/video/segments/${si}/regenerate`, { instruction: $(`#vr-text-${si}`).value.trim() || null }, 'Planning new visuals and updating the video...');
  if (act === 'revert') return runAction(`/api/projects/${p.id}/segments/${si}/revert`, { version: Number(b.dataset.ver) }, 'Switching version...');
}

function startEdit(si, ti) {
  const p = S.project, v = curVersion(p.segments[si]), t = v.turns[ti];
  const el = $(`#ln-${si}-${ti}`);
  el.classList.add('editing');
  el.innerHTML = `<div class="who">${esc(t.speaker === 'A' ? p.settings.host_a.name : p.settings.host_b.name)}</div>
    <div class="edit">
      <textarea id="ed-text" rows="3" lang="${esc(p.settings.language)}">${esc(t.text)}</textarea>
      <div class="row"><label class="row" style="gap:6px">Delivery
        <select id="ed-style" style="width:auto"><option value="">Natural</option>${S.config.styles.map((x) => `<option ${x === t.style ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>
        <button class="btn primary small" data-act="save-edit" data-seg="${si}" data-turn="${ti}">Save and re-record this line</button>
        <button class="btn small" data-act="cancel-edit">Cancel</button></div>
      <div class="hint">Only this line is recorded again; the rest of the episode is reused. Hand edits are not re-checked against the chapter.</div>
    </div>`;
  $('#ed-text').focus();
}

async function runAction(path, body, msg) {
  try {
    const r = await post(path, body);
    S.job = r.job;
    toast(msg);
    S.sigs = {};
    renderProject();
    startPoll();
  } catch (e) { toast(e.message, true); }
}

async function onHeadClick(e) {
  const b = e.target.closest('[data-head]');
  if (!b) return;
  const p = S.project, a = b.dataset.head;
  try {
    if (a === 'quality') {
      showQuality(p.id, false);
    } else if (a === 'resume' || a === 'voice' || a === 'assemble' || a === 'remix') {
      const r = await post(`/api/projects/${p.id}/${a}`);
      S.job = r.job; S.sigs = {}; renderProject(); startPoll();
    } else if (a === 'cancel') {
      await post(`/api/jobs/${S.job.id}/cancel`);
      toast('Stopping after the current step...');
    } else if (a === 'delete') {
      if (!confirm('Delete this episode and all its audio? This cannot be undone.')) return;
      await api(`/api/projects/${p.id}`, { method: 'DELETE' });
      await refreshList();
      delete S.detail[p.id];
      location.hash = S.projects.length ? '#/home' : '#/new';
    }
  } catch (err) { toast(err.message, true); }
}

/* ---------------------------------------------------------------- video */
function videoCfgFromForm() {
  return { resolution: $('#v-res').value, theme: $('#v-theme').value, captions: $('#v-cap').checked, speakers: $('#v-spk').checked, use_figures: $('#v-fig').checked };
}

function renderVideo() {
  const p = S.project, v = p.video, f = p.final, el = $('#p-video');
  if (!f || !f.wav) { el.innerHTML = ''; return; }
  const base = `/api/projects/${encodeURIComponent(p.id)}/files/`;
  const busy = isActive() || BUSY_STATUS.has(p.status);
  const c = v.cfg, fin = v.final;
  const stale = fin && (fin.stale || fin.audio_built_at !== f.built_at);
  const opt = (id, label, list, cur) => `<label class="field vf">${label}<select id="${id}">${list.map(([k, n]) => `<option value="${k}" ${k === cur ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select></label>`;
  const chk = (id, label, on, tip) => `<label class="check" title="${esc(tip)}"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}> ${label}</label>`;
  const options = `<div class="vopts">
      ${opt('v-res', 'Size', [['480p', '480p (small, fast)'], ['720p', '720p (recommended)'], ['1080p', '1080p (sharpest, slowest)']], c.resolution)}
      ${opt('v-theme', 'Look', [['classic', 'Classic (light)'], ['dark', 'Dark'], ['warm', 'Warm']], c.theme)}
      ${chk('v-cap', 'Captions', c.captions, 'The spoken words along the bottom')}
      ${chk('v-spk', 'Speaker names', c.speakers, 'Who is talking, next to the caption')}
      ${chk('v-fig', "Chapter's own pictures", c.use_figures, 'Use figures from the PDF where they fit, with their original captions')}
    </div>`;
  const dis = busy ? 'disabled' : '';
  let body;
  if (fin && fin.mp4) {
    const warns = (fin.warnings || []).map((w) => `<div class="notice" style="margin-top:8px">${esc(w)}</div>`).join('');
    const notes = v.storyboards.flatMap((b) => (b.issues || []).map((i) => `Segment ${b.index + 1}: ${i}`));
    body = `<div class="vgrid">
        <video class="vplayer" controls preload="metadata" ${fin.poster ? `poster="${base}${fin.poster}?v=${encodeURIComponent(fin.built_at)}"` : ''} src="${base}${fin.mp4}?v=${encodeURIComponent(fin.built_at)}"></video>
        <div class="vside">
          <div class="hint">${fin.width}x${fin.height} · ${fmt(fin.duration)} · made from the episode audio and the script, so the pictures always match what is said.</div>
          <div class="row"><a class="btn primary" href="${base}${fin.mp4}?download=1">Download MP4</a></div>
          <details><summary>Change look or size and remake</summary>${options}
            <div class="row" style="margin-top:8px"><button class="btn small" data-v="make" ${dis}>Remake video</button>
            <button class="btn small" data-v="remake-all" ${dis} title="Ask the model to plan every segment's visuals again">Plan all visuals again</button></div>
            <div class="hint">Remaking reuses every picture that did not change, so it is quick.</div></details>
        </div></div>
        ${stale ? `<div class="notice" style="margin-top:10px">The episode changed after this video was made. <button class="link" data-v="make" ${dis}>Update the video</button> (only the changed segments are redrawn).</div>` : ''}
        ${v.status === 'error' ? `<div class="notice err" style="margin-top:10px">${esc(v.message)}</div>` : ''}
        ${warns}${notes.length ? `<details style="margin-top:8px"><summary>Visual notes (${notes.length})</summary>${notes.map((n) => `<div class="issue minor">${esc(n)}</div>`).join('')}</details>` : ''}`;
  } else {
    body = `<p class="hint">Turns this episode into a narrated explainer: slides built <b>only from what the hosts say</b> (key terms, steps, cycles, comparisons, and the chapter's own figures), with live captions, in sync with the audio. Takes a few minutes the first time.</p>
      ${options}
      ${!S.config.ffmpeg ? '<div class="notice err">ffmpeg is needed to make the video. On Windows run <code>winget install Gyan.FFmpeg</code> and restart the app.</div>' : ''}
      ${v.status === 'error' ? `<div class="notice err">${esc(v.message)}</div>` : ''}
      <div class="row" style="margin-top:10px"><button class="btn primary" data-v="make" ${dis || (!S.config.ffmpeg ? 'disabled' : '')}>Make video</button></div>`;
  }
  el.innerHTML = `<section class="panel vcard" aria-label="Video"><h2>Video</h2>${body}</section>`;
}

async function onVideoClick(e) {
  const b = e.target.closest('[data-v]');
  if (!b) return;
  const p = S.project;
  const body = videoCfgFromForm();
  if (b.dataset.v === 'remake-all') body.redo_visuals = true;
  await runAction(`/api/projects/${p.id}/video`, body, 'Making the video...');
}

function visualsHTML(s, dis) {
  const p = S.project;
  if (!S.hasVideo || !p.video) return '';
  const sb = p.video.storyboards.find((b) => b.index === s.index);
  if (!sb) return '';
  const base = `/api/projects/${encodeURIComponent(p.id)}/files/`;
  const fin = p.video.final;
  const thumbs = fin ? `<div class="vthumbs">${sb.scenes.map((sc, i) => `<figure><img loading="lazy" alt="${esc(sc.layout)} scene" src="${base}video/scenes/s${s.index}_${i}.jpg?v=${encodeURIComponent(fin.built_at)}"><figcaption>${esc(sc.layout)}</figcaption></figure>`).join('')}</div>` : '';
  const src = ({ llm: 'planned by the model and checked against the script', fallback: 'built directly from the script (safe layout)', mock: 'demo visuals' })[sb.source] || sb.source;
  const stale = sb.version !== s.current ? ' <span class="badge warn">older script</span>' : '';
  return `<details><summary>Visuals (${sb.scenes.length} scenes)${stale}</summary>
    <div class="hint">${esc(src)}${sb.issues?.length ? `; ${sb.issues.length} note(s)` : ''}</div>${thumbs}
    ${(sb.issues || []).map((i) => `<div class="issue minor">${esc(i)}</div>`).join('')}
    <div class="row" style="margin-top:6px"><button class="btn small" data-act="toggle-vr" data-seg="${s.index}">Redo visuals...</button></div>
    <div class="rewrite" id="vr-${s.index}" hidden>
      <label class="field">What should look different? <span class="hint">leave empty for a fresh plan</span>
        <textarea id="vr-text-${s.index}" rows="2" placeholder="e.g. Show it as a cycle, use a comparison table, fewer words on screen"></textarea></label>
      <div class="chips">${['Show it as a cycle', 'Show it as numbered steps', 'Use a comparison table', 'Fewer words on screen', 'Use the chapter figure'].map((c) => `<button type="button" class="chip" data-act="vchip" data-seg="${s.index}">${c}</button>`).join('')}</div>
      <div class="row"><button class="btn primary small" data-act="redo-visuals" data-seg="${s.index}" ${dis}>Redo this segment's visuals</button></div>
    </div></details>`;
}

/* ---------------------------------------------------------------- player */
function setupPlayer() {
  const a = $('#audio');
  a.addEventListener('timeupdate', onTime);
  a.addEventListener('seeked', () => { onTime(); loopOn(); });
  a.addEventListener('play', () => { ST.playStart = performance.now(); attachAnalyser(); stageLive(); loopOn(); });
  a.addEventListener('playing', attachAnalyser);
  ['pause', 'ended'].forEach((ev) => a.addEventListener(ev, () => { stageLive(); loopOn(); }));
  a.addEventListener('ended', () => window.Extras?.onEnded?.());
  $('.speed').addEventListener('click', (e) => {
    const b = e.target.closest('[data-speed]');
    if (!b) return;
    a.playbackRate = Number(b.dataset.speed);
    $$('.speed button').forEach((x) => x.classList.toggle('on', x === b));
  });
}
function hidePlayer() { const a = $('#audio'); a.pause(); $('#player').hidden = true; S.audioKey = null; }

function syncPlayer() {
  if (S.ask) return;                       // an answer from "Ask the hosts" is using the player right now
  const f = S.project?.final;
  if (!f) return hidePlayer();
  const a = $('#audio');
  const rel = f.mp3 || f.wav;
  const key = `${S.project.id}|${rel}|${f.built_at}`;
  $('#player').hidden = false;
  if (S.audioKey === key) return;
  const wasSameProject = S.audioKey && S.audioKey.split('|')[0] === S.project.id;
  const t = wasSameProject ? a.currentTime : 0, playing = wasSameProject && !a.paused;
  S.audioKey = key;
  a.src = `/api/projects/${encodeURIComponent(S.project.id)}/files/${rel}?v=${encodeURIComponent(f.built_at)}`;
  a.addEventListener('loadedmetadata', () => { if (t) a.currentTime = Math.min(t, a.duration || t); if (playing) a.play().catch(() => {}); }, { once: true });
}

function playAt(sec) {
  const a = $('#audio');
  if (!S.project?.final) return;
  a.currentTime = Math.max(0, sec);
  a.play().catch(() => toast('Press play on the player to start audio.'));
}

function onTime() {
  const a = $('#audio'), t = a.currentTime;
  const f = S.project?.final;
  const ph = $('#tl-play'), tr = $('#tl-track');
  const dur = S.ask ? S.ask.duration : f?.duration;
  if (ph && tr && f?.duration && !S.ask) { ph.style.display = 'block'; ph.style.left = (t / f.duration * 100) + '%'; }
  let cur = null, main = null, rx = null;
  for (const l of S.lines) {
    if (t >= l.start && t < l.end + 0.05) { cur = l; if (l.react) rx = l; else main = l; }
  }
  if (main !== ST.main || rx !== ST.react) stageLine(main, rx);
  stageClock(t, main || rx, dur);
  if (cur !== S.curLine) {
    S.curLine?.el.classList.remove('now');
    cur?.el.classList.add('now');
    if (cur && !a.paused && !ST.inView) {   // follow the transcript only once the big stage has scrolled away
      const r = cur.el.getBoundingClientRect();
      if (r.top < 110 || r.bottom > innerHeight - 110) cur.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    S.curLine = cur;
  }
  if (f?.chapters?.length && !S.ask) {
    let ci = 0;
    f.chapters.forEach((c, i) => { if (t >= c.start - 0.05) ci = i; });
    $('#player-chapter').textContent = `${ci + 1}. ${f.chapters[ci].title}`;
  }
}

/* ---------------------------------------------------------------- stage: the animated hosts */
/* How the two 3D hosts look (the same on the welcome screen and in every episode). */
const HOSTCFG = {
  A: { skin: '#e2ab85', shade: '#c98f6b', jacket: '#0e7c7b', jacketDark: '#0a5f5e', rim: '#46c9c5' },
  B: { skin: '#c78f64', shade: '#a8714a', jacket: '#b4235e', jacketDark: '#8c1a49', rim: '#f27aa8' },
};
const cfg3D = (k, gender) => ({ gender, ...HOSTCFG[k] });
const ICON = {
  play: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>',
  pause: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/></svg>',
  prev: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 5h2v14H6zM20 5.5v13L9.5 12z" fill="currentColor"/></svg>',
  next: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M16 5h2v14h-2zM4 5.5v13L14.5 12z" fill="currentColor"/></svg>',
};
/* All the stage's moving parts live here so a rebuild can reset them in one place. */
const ST = {
  raf: 0, last: 0, mo: { A: 0, B: 0 }, main: null, react: null, talking: null, capSig: null, words: [], cum: [], lit: -1, sec: -1, inView: true, io: null, s3d: null,
  ctx: null, an: null, buf: null, src: null, capKey: null, ampSeen: 0, playStart: 0, rmT: 0, rmV: { A: 0, B: 0 }, bars: [],
};

const stageEl = () => $('#stage');
const stageRoot = () => $('#p-stage');   // the stage plus the small dock that follows you down the page
const hostOf = (k) => S.project?.settings?.['host_' + k.toLowerCase()];
function hostGender(k) {
  const v = S.config.voices.find((x) => x.name === hostOf(k)?.voice);
  return v?.gender === 'male' ? 'male' : 'female';
}

/* The stage exists once there is something to watch: finished audio, or a job in progress. */
const stageShown = () => !!S.project && (!!S.project.final || isActive() || isBusy(S.project));

function resetStage() {
  ST.main = ST.react = null; ST.talking = null; ST.capSig = null; ST.words = []; ST.cum = []; ST.lit = -1; ST.sec = -1;
  ST.mo = { A: 0, B: 0 }; ST.bars = []; ST.inView = true;
  ST.io?.disconnect(); ST.io = null;
  if (ST.s3d) { try { ST.s3d.dispose(); } catch (_) { /* already gone */ } ST.s3d = null; }
}

function buildStage() {
  const box = $('#p-stage');
  resetStage();
  box.classList.remove('is3d', 'no3d');
  if (!stageShown()) { box.innerHTML = ''; return; }
  const card = (k) => `<div class="host-card" data-k="${k}">
      <span class="fb" aria-hidden="true">${esc((hostOf(k).name || k).trim().charAt(0).toUpperCase())}</span>
      <span class="emote" data-k="${k}" aria-hidden="true"><span class="etxt"></span><b class="dots"><i></i><i></i><i></i></b></span>
      <div class="plate"><span class="plate-dot"></span>${esc(hostOf(k).name)}</div></div>`;
  box.innerHTML = `<section class="stage" id="stage" data-mode="play" data-live="0" aria-label="The two hosts">
    <div class="st-top">
      <div class="onair" aria-hidden="true"><i></i>On air</div>
      <div class="st-seg" id="st-seg"></div>
      <div class="st-time" id="st-time"></div>
    </div>
    <div class="st-set">
      <canvas class="st3d" aria-hidden="true"></canvas>
      ${card('A')}
      <div class="st-mid" aria-hidden="true"><div class="eq">${'<i></i>'.repeat(7)}</div></div>
      ${card('B')}
    </div>
    <div class="st-cap"><span class="st-who" id="st-who" hidden></span><span class="st-text plain" id="st-text"></span></div>
    <div class="st-ctl">
      <button type="button" class="sbtn" data-stage="prev" aria-label="Previous line (J)">${ICON.prev}</button>
      <button type="button" class="sbtn big" data-stage="toggle" aria-label="Play">${ICON.play}</button>
      <button type="button" class="sbtn" data-stage="next" aria-label="Next line (K)">${ICON.next}</button>
      <span class="st-keys"><kbd>Space</kbd> play or pause <kbd>J</kbd> <kbd>K</kbd> previous or next line</span>
    </div></section>
    <div class="dock" id="st-dock" hidden aria-label="Hosts">
      ${card('A')}${card('B')}<canvas class="dock3d" aria-hidden="true"></canvas>
      <div class="dock-mid"><span class="st-who dk-who" hidden></span><span class="dk-text"></span></div>
      <div class="dock-ctl">
        <button type="button" class="sbtn" data-stage="prev" aria-label="Previous line (J)">${ICON.prev}</button>
        <button type="button" class="sbtn big" data-stage="toggle" aria-label="Play">${ICON.play}</button>
        <button type="button" class="sbtn" data-stage="next" aria-label="Next line (K)">${ICON.next}</button>
      </div>
    </div>`;
  ST.bars = $$('.eq i', box);
  // when the big stage scrolls out of view, a slim dock keeps the hosts and caption on screen
  if ('IntersectionObserver' in window) {
    ST.io = new IntersectionObserver(([en]) => { ST.inView = en.isIntersecting; dockSync(); });
    ST.io.observe($('#stage'));
  } else { ST.inView = false; dockSync(); }
  if (S.no3d) { stageEl().classList.add('no3d'); box.classList.add('no3d'); }
  stageLive();
  start3D();
}
function dockSync() { const d = $('#st-dock'); if (d) d.hidden = ST.inView; push3D(); }

/* ---- the 3D hosts. They need static/stage3d.js and a browser with WebGL; without either, a plain name badge stands in for each host. */
let s3dLoad = null;
function load3D() {
  if (window.Stage3D) return Promise.resolve(window.Stage3D);
  s3dLoad ||= new Promise((resolve) => {
    const sc = document.createElement('script');
    sc.src = '/static/stage3d.js';
    sc.onload = () => resolve(window.Stage3D && window.Stage3D.supported() ? window.Stage3D : null);
    sc.onerror = () => resolve(null);
    document.head.appendChild(sc);
  });
  return s3dLoad;
}

function start3D() {
  const el = stageEl();
  if (!el || S.no3d) return;
  load3D().then((S3D) => {
    if (stageEl() !== el) return;           // the page changed while the script was loading
    if (!S3D) { S.no3d = true; el.classList.add('no3d'); stageRoot().classList.add('no3d'); return; }
    const canvas = $('.st3d', el), dock = $('.dock3d');
    el.classList.add('is3d'); stageRoot().classList.add('is3d');
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    dock.width = Math.round(200 * dpr); dock.height = Math.round(66 * dpr);
    try {
      ST.s3d = S3D.create(canvas, { hosts: { A: cfg3D('A', hostGender('A')), B: cfg3D('B', hostGender('B')) }, dockCanvas: dock, onLayout: layout3D });
    } catch (_) { el.classList.replace('is3d', 'no3d'); stageRoot().classList.replace('is3d', 'no3d'); ST.s3d = null; S.no3d = true; return; }
    const inst = ST.s3d;
    canvas.addEventListener('webglcontextlost', (e) => {
      if (ST.s3d !== inst) return;           // we released it ourselves while leaving the page
      e.preventDefault(); S.no3d = true; S.sigs.stage = null; renderProject();
    });
    // the little speech/thought bubbles move out of their cards so they can sit over the 3D heads
    $$('.host-card', el).forEach((c) => { const em = $('.emote', c); if (em) $('.st-set', el).appendChild(em); });
    push3D();
  });
}

/* Called by the 3D scene whenever it is resized: puts the name plates and bubbles under/over the 3D heads. */
function layout3D(pos) {
  const el = stageEl();
  if (!el) return;
  ['A', 'B'].forEach((k) => {
    const pl = $(`.host-card[data-k=${k}] .plate`, el), em = $(`.emote[data-k=${k}]`, el);
    if (pl) pl.style.left = pos[k].cx + 'px';
    if (em) { em.style.left = pos[k].x + 'px'; em.style.top = pos[k].y + 'px'; }
  });
}

/* Hands the current mouth levels, speaker and expressions to the 3D scene (it does its own animation between calls). */
function push3D() {
  const s3 = ST.s3d, el = stageEl();
  if (!s3 || !el) return;
  const card = (k) => $(`.host-card[data-k=${k}]`, el);
  const exOf = (c) => [...c.classList].filter((x) => x.startsWith('ex-')).map((x) => x.slice(3));
  s3.set({
    mode: el.dataset.mode, live: el.dataset.live === '1', speaker: ST.talking, reduced: reduced(), dock: !ST.inView,
    mo: { A: ST.mo.A, B: ST.mo.B }, ex: { A: exOf(card('A')), B: exOf(card('B')) },
    reacting: { A: card('A').classList.contains('reacting'), B: card('B').classList.contains('reacting') },
  });
}

/* Called on every render: picks the stage's mode and the caption that goes with it. */
function updateStage() {
  const el = stageEl();
  if (!el) return;
  const p = S.project, a = $('#audio');
  const playing = !a.paused && !a.ended;
  let mode = 'play';
  if (!playing && (isActive() || isBusy(p))) mode = ({ voicing: 'rec', assembling: 'mix' })[p.status] || (VBUSY.has(p.video?.status) ? 'mix' : 'think');
  if (el.dataset.mode !== mode) {
    el.dataset.mode = mode;
    ST.main = ST.react = null; ST.capSig = null;
    setTalking(null);
  }
  const segs = p.segments;
  if (mode === 'think') {
    setCaption('', '', S.job?.message || statusLabel(p.status));
    $('#st-seg').textContent = segs.find((s) => s.status === 'writing')?.title || '';
  } else if (mode === 'rec') {
    const vs = segs.find((s) => s.status === 'voicing');
    setCaption('', '', vs ? `Recording segment ${vs.index + 1} of ${segs.length}` : `Recording ${segs.filter((s) => s.status === 'ready').length} of ${segs.length} segments`);
    $('#st-seg').textContent = vs?.title || '';
  } else if (mode === 'mix') {
    setCaption('', '', 'Mixing the final audio');
    $('#st-seg').textContent = '';
  } else if (!ST.main && !ST.react && !ST.words.length) {
    setCaption('', '', `Press play to hear ${hostOf('A').name} and ${hostOf('B').name}.`);
    $('#st-seg').textContent = p.plan?.episode_title || '';
  }
  stageLive();
  loopOn();
}

function setCaption(who, k, text, words) {
  const sig = `${who}|${k}|${text}|${words ? 1 : 0}`;
  if (ST.capSig === sig) return;
  ST.capSig = sig;
  const w = $('#st-who'), t = $('#st-text');
  if (!w || !t) return;
  w.textContent = who; w.dataset.k = k || ''; w.hidden = !who;
  const dw = $('.dk-who'), dt = $('.dk-text');
  if (dw && dt) { dw.textContent = who; dw.dataset.k = k || ''; dw.hidden = !who; dt.textContent = text; }
  if (words) {
    const parts = text.split(/\s+/).filter(Boolean);
    const total = parts.reduce((a, x) => a + x.length + 1, 0) || 1;
    let acc = 0;
    ST.cum = parts.map((x) => { const start = acc / total; acc += x.length + 1; return start; });
    t.innerHTML = parts.map((x) => `<span class="w">${esc(x)}</span>`).join(' ');
    ST.words = $$('.w', t); ST.lit = -1;
    t.className = 'st-text' + (text.length > 150 ? ' long' : '');
    t.dataset.script = INDIC.test(text) ? 'indic' : 'latin';
  } else {
    t.textContent = text; ST.words = []; ST.cum = []; ST.lit = -1;
    t.className = 'st-text plain';
  }
}

function setTalking(k) {
  const el = stageEl(), root = stageRoot();
  if (!el) return;
  ST.talking = k;
  $$('.host-card', root).forEach((c) => {
    c.classList.toggle('talking', c.dataset.k === k);
    c.classList.toggle('listening', !!k && c.dataset.k !== k);
  });
  el.style.setProperty('--spk', k ? `var(--${k.toLowerCase()}-lt)` : 'var(--mark)');
  push3D();
}

const EXPRESSIONS = { happy: 'ex-joy', surprise: 'ex-surprise', anger: 'ex-anger', sad: 'ex-sad', fear: 'ex-surprise', disgust: 'ex-anger' };

/* A new line started (or the overlapping reaction changed): update who talks, who listens, the caption. */
function stageLine(main, rx) {
  ST.main = main; ST.react = rx;
  const el = stageEl();
  if (!el) return;
  const line = main || rx;
  if (line) {
    setCaption(hostOf(line.speaker).name, line.speaker, line.text, true);
    $('#st-seg').textContent = S.ask ? 'Answering a listener' : `${line.seg + 1}. ${S.project.segments[line.seg]?.title || ''}`;
  }
  setTalking(line ? line.speaker : null);
  $$('.host-card', stageRoot()).forEach((c) => {
    c.className = c.className.replace(/\bex-\w+/g, '').replace(/\s+/g, ' ').trim();
    const mine = line && c.dataset.k === line.speaker;
    if (mine) {
      const ex = EXPRESSIONS[line.style] || '';
      if (/[?？]\s*$/.test(line.text)) c.classList.add('ex-ask');
      if (ex) c.classList.add(ex);
    }
    // a quick "mm-hm" from the listener pops up as a small bubble over their head
    const reactor = !!(main && rx && c.dataset.k === rx.speaker && rx.speaker !== main.speaker);
    const em = $(`.emote[data-k=${c.dataset.k}]`, el);
    if (em) {
      $('.etxt', em).textContent = reactor && rx.text.length < 28 ? rx.text : '';
      em.classList.toggle('show', reactor && rx.text.length < 28);
    }
    c.classList.toggle('reacting', reactor);
  });
  push3D();
}

/* Clock text and the word-by-word caption highlight. Cheap enough to run every frame. */
function stageClock(t, line, dur) {
  const el = stageEl();
  if (!el) return;
  const sec = Math.floor(t);
  if (sec !== ST.sec) { ST.sec = sec; const tm = $('#st-time'); if (tm) tm.textContent = `${fmt(t)} / ${fmt(dur)}`; }
  if (!ST.words.length) return;
  let n;
  if (line) {
    const frac = Math.min(1, Math.max(0, (t - line.start) / Math.max(0.2, line.end - line.start)));
    n = 0;
    while (n < ST.cum.length && ST.cum[n] <= frac + 0.02) n++;
  } else n = ST.words.length;
  if (n !== ST.lit) { ST.words.forEach((w, i) => w.classList.toggle('on', i < n)); ST.lit = n; }
}

function stageLive() {
  const el = stageEl();
  if (!el) return;
  const a = $('#audio');
  const playing = !a.paused && !a.ended && el.dataset.mode === 'play';
  el.dataset.live = playing || el.dataset.mode === 'rec' ? '1' : '0';
  const root = stageRoot();
  $$('[data-stage=toggle]', root).forEach((b) => {
    b.innerHTML = playing ? ICON.pause : ICON.play;
    b.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    b.disabled = !S.project?.final;
  });
  $$('[data-stage=prev], [data-stage=next]', root).forEach((x) => { x.disabled = !S.lines.length; });
  push3D();
}

/* ---- audio level -> mouth. The analyser listens to a copy of the stream; the sound itself is never rerouted. */
function attachAnalyser() {
  const a = $('#audio');
  const cap = a.captureStream || a.mozCaptureStream;
  if (!cap) return;
  try {
    ST.ctx ||= new (window.AudioContext || window.webkitAudioContext)();
    if (ST.ctx.state === 'suspended') ST.ctx.resume().catch(() => {});
    if (ST.capKey === a.currentSrc && ST.src) return;
    const stream = cap.call(a);
    if (!stream.getAudioTracks().length) return;   // not decoded yet; the 'playing' event tries again
    try { ST.src?.disconnect(); } catch (_) { /* old node already gone */ }
    ST.an ||= Object.assign(ST.ctx.createAnalyser(), { fftSize: 1024, smoothingTimeConstant: 0.15 });
    ST.buf ||= new Uint8Array(ST.an.fftSize);
    ST.src = ST.ctx.createMediaStreamSource(stream);
    ST.src.connect(ST.an);       // deliberately NOT connected to ctx.destination
    ST.capKey = a.currentSrc;
    ST.ampSeen = 0;
  } catch (_) { ST.an = null; ST.src = null; }
}

function readLevel() {
  if (!ST.an) return null;
  ST.an.getByteTimeDomainData(ST.buf);
  let s = 0;
  for (let i = 0; i < ST.buf.length; i++) { const v = (ST.buf[i] - 128) / 128; s += v * v; }
  const rms = Math.sqrt(s / ST.buf.length);
  ST.ampSeen = Math.max(ST.ampSeen, rms);
  if (ST.ampSeen < 0.004 && performance.now() - ST.playStart > 2500) return null;   // analyser is deaf: use the stand-in
  return Math.pow(Math.min(1, Math.max(0, (rms - 0.012) / 0.16)), 0.85);
}

/* Stand-in mouth movement (syllable-like) for browsers that cannot give us the audio level. */
function synth(t) {
  const syl = Math.abs(Math.sin(t * 8.3)) * (0.55 + 0.45 * Math.sin(t * 2.1 + 1));
  return Math.min(1, syl * (Math.sin(t * 0.9) > -0.7 ? 1 : 0.15) + 0.08);
}

function loopOn() {
  if (ST.raf) return;
  ST.last = performance.now();
  ST.raf = requestAnimationFrame(stageLoop);
}
function stageLoop(ts) {
  ST.raf = 0;
  const a = $('#audio'), el = stageEl();
  const playing = !a.paused && !a.ended;
  if (playing) onTime();
  let keep = playing;
  if (el && stageFrame(el, ts, playing)) keep = true;
  if (keep) ST.raf = requestAnimationFrame(stageLoop);
}

/* One animation frame. Returns true while something is still moving. */
function stageFrame(el, ts, playing) {
  ST.last = ts;
  const t = ts / 1000, mode = el.dataset.mode, rm = reduced();
  let spk = null, lvl = 0;
  if (mode === 'rec') {
    spk = Math.floor(t / 2.4) % 2 ? 'B' : 'A';
    if (ST.talking !== spk) setTalking(spk);
    lvl = synth(t);
  } else if (mode === 'play' && playing) {
    spk = (ST.main || ST.react)?.speaker || null;
    if (spk) { const r = readLevel(); lvl = r == null ? synth(t) : r; }
  }
  let moving = mode === 'rec' || mode === 'think';
  const want = { A: 0, B: 0 };
  for (const k of ['A', 'B']) {
    want[k] = k === spk ? lvl : 0;
    if (mode === 'play' && playing && ST.main && ST.react && ST.react.speaker === k && k !== spk) want[k] = 0.3;   // a quick "mm-hm"
  }
  if (rm && ts - ST.rmT > 250) {                // reduced motion: each mouth only toggles, about four times a second
    ST.rmT = ts;
    ST.rmV = { A: want.A > 0.3 ? 0.5 : 0, B: want.B > 0.3 ? 0.5 : 0 };
  }
  for (const k of ['A', 'B']) {
    const target = rm ? ST.rmV[k] : want[k];
    const cur = ST.mo[k];
    let next = rm ? target : cur + (target - cur) * (target > cur ? 0.55 : 0.2);
    if (next < 0.01 && target === 0) next = 0;
    ST.mo[k] = next;
    $$(`.host-card[data-k=${k}]`, stageRoot()).forEach((card) => card.style.setProperty('--mo', next.toFixed(3)));
    if (next > 0) moving = true;
  }
  const lv = spk && !rm ? ST.mo[spk] : 0;
  ST.bars.forEach((b, i) => {
    b.style.transform = `scaleY(${(0.12 + lv * (0.55 + 0.45 * Math.sin(t * (6 + i * 1.7) + i * 1.3)) * (1 - Math.abs(i - 3) * 0.12)).toFixed(3)})`;
  });
  push3D();
  return moving;
}

function onStageClick(e) {
  const b = e.target.closest('[data-stage]');
  if (!b) return;
  if (b.dataset.stage === 'toggle') togglePlay();
  else jumpLine(b.dataset.stage === 'next' ? 1 : -1);
}

function togglePlay() {
  const a = $('#audio');
  if (!S.project?.final) return;
  if (a.paused) a.play().catch(() => toast('Press play on the player to start audio.'));
  else a.pause();
}

/* Previous line restarts the current one first (like a music player); next jumps to the following line. */
function jumpLine(dir) {
  const t = $('#audio').currentTime;
  const ls = S.lines.filter((l) => !l.react);
  if (!ls.length) return;
  const target = dir > 0 ? ls.find((l) => l.start > t + 0.05) : (ls.filter((l) => l.start < t - 1.0).pop() || ls[0]);
  if (target) playAt(target.start);
}

function setupKeys() {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    if (S.view !== 'project' || !S.project?.final || $('#dlg').open) return;
    const t = e.target;
    if (t.closest?.('input, textarea, select, audio, video, [contenteditable]')) return;   // typing, or the player's own keys
    const k = e.key.toLowerCase();
    // J and K are safe even with a button focused; Space on a focused button/link must keep activating that control
    if (e.key === ' ' && !t.closest?.('button, a, summary')) { e.preventDefault(); togglePlay(); }
    else if (k === 'j') { e.preventDefault(); jumpLine(-1); }
    else if (k === 'k') { e.preventDefault(); jumpLine(1); }
  });
}

/* ---------------------------------------------------------------- welcome: the first screen
   Two big choices (open an episode / create one) next to a live scene: the two 3D hosts rehearse a short sample conversation
   while small chips show what the app does with every claim. Nothing here talks to the server except the episode list. */
const WL = { raf: 0, timer: 0, alive: false, s3d: null, t0: 0, idx: -1, chipKey: '', starts: [], durs: [], total: 0, mo: { A: 0, B: 0 }, pos: null, names: { A: 'Meera', B: 'Arjun' } };
const WL_LINES = [
  { k: 'A', t: "Ready? Today we are doing the water cycle." },
  { k: 'B', t: 'The Sun warms the sea, the water rises as vapour, and clouds form as it cools.', chip: ['ok', 'Matches the chapter, paragraph 4'] },
  { k: 'A', t: 'Wait, how much of the planet’s water is in the oceans?', ask: true },
  { k: 'B', t: 'About 97 percent. That number is right there in the chapter.', chip: ['ok', 'Number found in the chapter'] },
  { k: 'A', t: 'And who won the last cricket World Cup?', ask: true },
  { k: 'B', t: 'That is not in this chapter, so I will not guess.', chip: ['no', 'Not in the chapter, so it says so'] },
];
const WL_SVG = {
  open: '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h4l2 2.2h7a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>',
  make: '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M12 5.5v13M5.5 12h13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
  go: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M5 12h13M13 6.5 18.5 12 13 17.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  ok: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  no: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M8 7.2v4.4M8 4.4v.2" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
};

function showWelcome() {
  setView('welcome'); S.pid = null; S.project = null; S.job = null; S.sigs = {};
  hidePlayer(); renderSidebar();
  $('#main').innerHTML = welcomeHTML();
  $('#main .wl').addEventListener('click', onWelcomeClick);
  wlRenderChoices();
  startWelcome();
  // keep the numbers fresh while this screen is open (an episode may finish in the background)
  WL.timer = setInterval(async () => { await refreshList(); if (S.view === 'welcome') wlRenderChoices(); }, 5000);
}

function welcomeHTML() {
  const c = S.config, d = c.defaults || {};
  const nA = d.host_a?.name || 'Meera', nB = d.host_b?.name || 'Arjun';
  WL.names = { A: nA, B: nB };
  const missing = c.missing_keys || [];
  const langs = (c.languages || []).length;
  const steps = [['Read', 'The chapter is cleaned and numbered'], ['Write', 'Two hosts, one segment at a time'], ['Check', 'Every line against the chapter'], ['Voice', `${langs > 1 ? langs + ' languages' : 'Natural voices'}`], ['Listen', 'Audio, transcript, quiz']];
  const fb = (k) => `<span class="wl-fb" data-k="${k}" aria-hidden="true">${esc(WL.names[k].trim().charAt(0).toUpperCase())}</span>`;
  const plate = (k) => `<div class="wl-plate" data-k="${k}" aria-hidden="true"><span class="plate-dot"></span>${esc(WL.names[k])}</div>`;
  return `<div class="wl">
  <header class="wl-top">
    <div class="wl-brand"><span class="brand-mark" aria-hidden="true"></span><span class="brand-name">Podcast Studio</span></div>
    <div class="wl-tools">${c.mock ? '<span class="badge mock">Demo mode</span>' : ''}
      <button class="link" type="button" data-wl="keys">Keys</button><button class="link" type="button" data-wl="tuning">Tuning</button><button class="link" type="button" data-wl="doctor">Check setup</button></div>
  </header>
  <div class="wl-body">
    <section class="wl-copy" aria-labelledby="wl-h">
      <div class="wl-eyebrow"><i></i>Textbook chapter to two-host podcast</div>
      <h1 class="wl-h" id="wl-h">Turn any chapter into <em>a conversation.</em></h1>
      <p class="wl-tag">${esc(nA)} and ${esc(nB)} read your chapter, talk it through${langs > 1 ? ` in ${langs} languages` : ''}, and only say what the chapter says.</p>
      ${missing.length ? `<div class="notice err">Missing: <b>${missing.map(esc).join(', ')}</b>. <button class="link" type="button" data-wl="keys">Add your keys now</button> (no restart needed), or switch on demo mode there to try everything offline.</div>` : ''}
      <div class="wl-choices" id="wl-choices"></div>
    </section>
    <div class="wl-scene" id="wl-scene" role="img" aria-label="${esc(nA)} and ${esc(nB)}, two animated hosts, rehearsing a sample conversation about a chapter">
      <div class="wl-live" aria-hidden="true"><i></i>Live · sample episode</div>
      <div class="wl-set" aria-hidden="true">
        <canvas class="wl3d"></canvas>
        ${fb('A')}${fb('B')}${plate('A')}${plate('B')}
        <div class="wl-chip" id="wl-chip"><span class="wl-ico"></span><span class="wl-ct"></span></div>
      </div>
      <div class="wl-cap" aria-hidden="true"><span class="st-who" id="wl-who" hidden></span><span class="st-text" id="wl-text"></span></div>
    </div>
    <ol class="wl-flow" aria-label="How an episode is made">${steps.map(([t, s], i) => `<li style="--i:${i}"><span class="n">${i + 1}</span><b>${esc(t)}</b><small>${esc(s)}</small></li>`).join('')}</ol>
  </div>
</div>`;
}

function wlRenderChoices() {
  const box = $('#wl-choices');
  if (!box) return;
  const ps = [...S.projects].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  const ready = ps.filter((p) => p.status === 'ready').length, busy = ps.filter((p) => groupOf(p) === 'working').length;
  const sub = ps.length ? `${ps.length} ${ps.length === 1 ? 'episode' : 'episodes'}${ready ? `, ${ready} ready` : ''}${busy ? `, ${busy} being made now` : ''}` : 'Nothing here yet. Make your first one.';
  const cls = (p) => (p.status === 'ready' ? 'ready' : p.status === 'error' ? 'error' : BUSY_STATUS.has(p.status) ? 'run' : p.status);
  const recent = ps.slice(0, 3).map((p) => `<li><button type="button" class="wl-row" data-pid="${esc(p.id)}"><span class="dot ${cls(p)}"></span><span class="wl-rt">${esc(p.title || 'Untitled')}</span><small>${esc(statusLabel(p.status))}${p.duration ? ' · ' + fmt(p.duration) : ''}</small></button></li>`).join('');
  const arrow = `<span class="wl-arrow">${WL_SVG.go}</span>`;
  const open = `<div class="wl-open${ps.length ? '' : ' empty'}"><button type="button" class="wl-go" data-wl="open" ${ps.length ? '' : 'disabled'}><span class="wl-ic">${WL_SVG.open}</span><span><b>Open an existing episode</b><small>${esc(sub)}</small></span>${arrow}</button>${recent ? `<ul class="wl-recent" aria-label="Most recent episodes">${recent}</ul>` : ''}</div>`;
  const make = `<button type="button" class="wl-go primary" data-wl="new"><span class="wl-ic">${WL_SVG.make}</span><span><b>Create a new episode</b><small>Upload a chapter, pick a language and the two hosts</small></span>${arrow}</button>`;
  const sig = `${ps.length}|${ps.slice(0, 3).map((p) => [p.id, p.status, p.title, p.duration].join(':')).join(',')}|${ready}|${busy}`;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = ps.length ? open + make : make + open;
}

function onWelcomeClick(e) {
  const row = e.target.closest('[data-pid]');
  if (row) { location.hash = '#/p/' + row.dataset.pid; return; }
  const b = e.target.closest('[data-wl]');
  if (!b || b.disabled) return;
  ({ new: () => { location.hash = '#/new'; }, open: () => { location.hash = '#/home'; }, keys: showKeys, tuning: showTuning, doctor: showDoctor })[b.dataset.wl]?.();
}

function wlPlan() {
  let t = 0.7;
  WL.starts = []; WL.durs = [];
  WL_LINES.forEach((l) => {
    const dur = 1.3 + l.t.split(/\s+/).length * 0.3;
    WL.starts.push(t); WL.durs.push(dur);
    t += dur + 0.55 + (l.chip ? 0.9 : 0);
  });
  WL.total = t + 0.5;
}

function startWelcome() {
  const root = $('#wl-scene');
  if (!root) return;
  wlPlan();
  Object.assign(WL, { alive: true, idx: -1, chipKey: '', mo: { A: 0, B: 0 }, pos: null });
  const rm = reduced();
  const still = () => wlFrame(WL.starts[1] + WL.durs[1] - 0.1, true);   // reduced motion: one calm, complete moment instead of a loop
  if (rm) still(); else { WL.t0 = performance.now(); WL.raf = requestAnimationFrame(wlLoop); }
  load3D().then((S3D) => {
    if (!WL.alive || $('#wl-scene') !== root) return;
    if (!S3D) { root.classList.add('no3d'); return; }
    const canvas = $('.wl3d', root);
    try {
      WL.s3d = S3D.create(canvas, { hosts: { A: cfg3D('A', 'female'), B: cfg3D('B', 'male') }, onLayout: wlLayout });
    } catch (_) { WL.s3d = null; root.classList.add('no3d'); return; }
    const inst = WL.s3d;
    root.classList.add('is3d');
    canvas.addEventListener('webglcontextlost', (ev) => {
      if (WL.s3d !== inst) return;
      ev.preventDefault(); WL.s3d = null; root.classList.remove('is3d'); root.classList.add('no3d');
    });
    if (rm) still();
  });
}

function stopWelcome() {
  WL.alive = false;
  cancelAnimationFrame(WL.raf); WL.raf = 0;
  clearInterval(WL.timer); WL.timer = 0;
  if (WL.s3d) { try { WL.s3d.dispose(); } catch (_) { /* already gone */ } WL.s3d = null; }
}

function wlLayout(pos) {
  WL.pos = pos;
  ['A', 'B'].forEach((k) => { const pl = $(`.wl-plate[data-k=${k}]`); if (pl) pl.style.left = pos[k].cx + 'px'; });
}

function wlLoop(ts) {
  if (!WL.alive) return;
  WL.raf = requestAnimationFrame(wlLoop);
  if (!$('#wl-scene')) { stopWelcome(); return; }
  wlFrame(((ts - WL.t0) / 1000) % WL.total, false);
}

/* One moment of the sample conversation: who talks, the caption, the check chip, and the 3D pose. Pure function of time. */
function wlFrame(t, rm) {
  const root = $('#wl-scene');
  if (!root) return;
  let i = 0;
  for (let n = 0; n < WL.starts.length; n++) if (t >= WL.starts[n]) i = n;
  const L = WL_LINES[i], lt = t - WL.starts[i], dur = WL.durs[i];
  const speaking = lt >= 0 && lt < dur;
  const who = $('#wl-who'), text = $('#wl-text');
  const words = L.t.split(/\s+/);
  if (i !== WL.idx) {
    WL.idx = i; WL.lit = -1;
    who.hidden = false; who.textContent = WL.names[L.k]; who.dataset.k = L.k;
    text.innerHTML = words.map((w) => `<span class="w">${esc(w)}</span>`).join(' ');
    text.className = 'st-text' + (L.t.length > 70 ? ' long' : '');
  }
  const n = lt < 0 ? 0 : speaking ? Math.min(words.length, Math.ceil(words.length * lt / (dur * 0.9))) : words.length;
  if (n !== WL.lit) { WL.lit = n; $$('.w', text).forEach((w, j) => w.classList.toggle('on', j < n)); }

  // the check chip: "checking..." first, then what the app decided
  const chip = $('#wl-chip');
  let state = '';
  if (L.chip && lt > 0.7 && lt < dur + 1.2) state = lt < 1.9 ? 'chk' : L.chip[0];
  const key = state ? `${i}|${state}` : '';
  if (key !== WL.chipKey) {
    WL.chipKey = key;
    if (state) {
      chip.dataset.k = L.k;
      chip.className = 'wl-chip on ' + state;
      $('.wl-ico', chip).innerHTML = state === 'chk' ? '' : WL_SVG[state];
      $('.wl-ct', chip).textContent = state === 'chk' ? 'Checking against the chapter...' : L.chip[1];
      // sit above the speaker, but never poke out of the picture on a narrow screen
      const sw = (chip.offsetParent || chip.parentElement).clientWidth, cw = chip.offsetWidth;
      const cx = WL.pos ? WL.pos[L.k].cx : sw * (L.k === 'A' ? 0.25 : 0.75);
      chip.style.left = Math.max(cw / 2 + 10, Math.min(sw - cw / 2 - 10, cx)) + 'px';
    } else chip.classList.remove('on');      // keep its colours while it fades out
  }

  const k = speaking ? L.k : null;
  const lvl = k && !rm ? synth(t) : 0;
  for (const x of ['A', 'B']) WL.mo[x] += ((x === k ? lvl : 0) - WL.mo[x]) * (x === k ? 0.55 : 0.2);
  $$('.wl-fb, .wl-plate', root).forEach((el) => { el.classList.toggle('talking', el.dataset.k === k); if (el.classList.contains('wl-fb')) el.style.setProperty('--mo', WL.mo[el.dataset.k].toFixed(3)); });
  WL.s3d?.set({
    mode: 'play', live: true, speaker: k, reduced: rm, dock: false, mo: { A: WL.mo.A, B: WL.mo.B },
    ex: { A: L.ask && k === 'A' ? ['ask'] : [], B: [] }, reacting: { A: false, B: false },
  });
}

/* ---------------------------------------------------------------- home dashboard */
const groupOf = (p) => (p.status === 'ready' ? 'ready' : (BUSY_STATUS.has(p.status) || p.status === 'created') ? 'working' : 'attention');
const FILTERS = [['all', 'All'], ['ready', 'Ready'], ['working', 'In progress'], ['attention', 'Needs you']];

function stopHome() { clearInterval(S.homeTimer); S.homeTimer = null; }

function ago(iso) {
  const t = Date.parse(iso);
  if (!t) return '';
  const d = (Date.now() - t) / 1000;
  if (d < 90) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  const days = Math.round(d / 86400);
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return new Date(t).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}
const longTime = (s) => (s < 3600 ? `${Math.max(1, Math.round(s / 60))} min` : `${(s / 3600).toFixed(1)} hours`);

function showHome() {
  setView('home'); S.pid = null; S.project = null; S.job = null; S.sigs = {}; S.homeTicks = 0;
  hidePlayer(); resetStage(); renderSidebar();
  $('#main').innerHTML = `<div id="h-top"></div><div id="h-live"></div>
    <div class="tools"><input type="search" id="h-q" class="search" placeholder="Search episodes" aria-label="Search episodes" value="${esc(S.q)}" autocomplete="off"><div id="h-chips" class="chips"></div></div>
    <div id="h-grid"></div>`;
  $('#h-q').addEventListener('input', (e) => { S.q = e.target.value; renderHome(); });
  ['#h-top', '#h-live', '#h-chips', '#h-grid'].forEach((s) => $(s).addEventListener('click', onHomeClick));
  renderHome();
  loadDetails();
  S.homeTimer = setInterval(homeTick, 2000);
}

async function homeTick() {
  S.homeTicks++;
  const busy = S.projects.some((p) => groupOf(p) === 'working');
  if (busy || S.homeTicks % 8 === 0) {
    await refreshList();
    if (S.view !== 'home') return;
    renderHome();
    loadDetails();
  }
}

function onHomeClick(e) {
  if (e.target.closest('a')) return;
  const f = e.target.closest('[data-filter]');
  if (f) { S.filter = f.dataset.filter; renderHome(); return; }
  const h = e.target.closest('[data-home]');
  if (h) {
    if (h.dataset.home === 'new') location.hash = '#/new';
    if (h.dataset.home === 'clear') { S.q = ''; S.filter = 'all'; $('#h-q').value = ''; renderHome(); }
    return;
  }
  const c = e.target.closest('[data-pid]');
  if (c) location.hash = '#/p/' + c.dataset.pid;
}

/* Full details (usage, timings) are only in the per-episode call, so fetch them quietly and cache by updated_at. */
async function loadDetails() {
  if (S.loadingDetails) return;
  S.loadingDetails = true;
  try {
    const todo = S.projects.filter((p) => { const d = S.detail[p.id]; return !d || d.stamp !== p.updated_at || groupOf(p) === 'working'; }).slice(0, 30);
    for (let i = 0; i < todo.length; i += 4) {
      await Promise.all(todo.slice(i, i + 4).map(async (p) => {
        try {
          const r = await api('/api/projects/' + encodeURIComponent(p.id));
          r.project.segments.forEach((s) => { s.version_obj = curVersion(s); });
          S.detail[p.id] = { stamp: p.updated_at, p: r.project, job: r.job, mini: null };
        } catch (_) { /* leave the card without a preview */ }
      }));
      if (S.view !== 'home') return;
      renderHome();
    }
  } finally { S.loadingDetails = false; }
}

function filteredProjects() {
  const q = S.q.trim().toLowerCase();
  return [...S.projects]
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    .filter((p) => (S.filter === 'all' || groupOf(p) === S.filter) && (!q || `${p.title} ${langName(p.language)} ${statusLabel(p.status)}`.toLowerCase().includes(q)));
}

function renderHome() {
  if (S.view !== 'home') return;
  const sig = (k, v, fn) => { const s = JSON.stringify(v); if (S.sigs[k] !== s) { S.sigs[k] = s; fn(); } };
  const ps = S.projects;
  const dkey = ps.map((p) => { const d = S.detail[p.id]; return d ? [d.stamp, d.job?.progress, d.job?.message] : 0; });
  sig('top', [ps.map((p) => [p.id, p.status, p.duration, p.language]), dkey.map((x) => !!x)], homeTop);
  sig('live', [ps.map((p) => [p.id, p.status, p.message]), dkey], homeLive);
  const counts = Object.fromEntries(FILTERS.map(([k]) => [k, k === 'all' ? ps.length : ps.filter((p) => groupOf(p) === k).length]));
  sig('chips', [S.filter, counts], () => {
    $('#h-chips').innerHTML = FILTERS.filter(([k]) => k === 'all' || counts[k]).map(([k, label]) =>
      `<button type="button" class="chip${S.filter === k ? ' on' : ''}" data-filter="${k}" aria-pressed="${S.filter === k}">${esc(label)} <span class="cnt">${counts[k]}</span></button>`).join('');
  });
  sig('grid', [S.q, S.filter, ps.map((p) => [p.id, p.status, p.title, p.duration, p.updated_at, p.message]), dkey], homeGrid);
}

function homeTop() {
  const ps = S.projects, ready = ps.filter((p) => p.status === 'ready');
  const secs = ready.reduce((a, p) => a + (p.duration || 0), 0);
  const langs = new Set(ready.map((p) => p.language));
  const working = ps.filter((p) => groupOf(p) === 'working').length;
  const waiting = ps.filter((p) => groupOf(p) === 'attention').length;
  let line;
  if (!ps.length) line = 'Nothing recorded yet. Your episodes will gather here.';
  else if (!ready.length) line = `${ps.length} ${ps.length === 1 ? 'episode' : 'episodes'} so far, none finished yet.`;
  else {
    const bits = [`<b>${ready.length}</b> ${ready.length === 1 ? 'episode is' : 'episodes are'} ready`];
    if (secs) bits.push(`with <b>${longTime(secs)}</b> of audio`);
    bits.push(`in <b>${langs.size}</b> ${langs.size === 1 ? 'language' : 'languages'}`);
    line = bits.join(', ') + '.';
  }
  if (working) line += ` ${working} ${working === 1 ? 'is' : 'are'} being made right now.`;
  if (waiting) line += ` ${waiting} ${waiting === 1 ? 'needs' : 'need'} a look from you.`;
  const us = ps.map((p) => S.detail[p.id]?.p.usage).filter(Boolean);
  let behind = '';
  if (ps.length && us.length === ps.length) {
    const sum = (k) => us.reduce((a, u) => a + (u[k] || 0), 0);
    const cost = sum('llm_cost_usd'), hits = sum('tts_cache_hits');
    behind = `<p class="behind">Behind the scenes: ${sum('llm_calls').toLocaleString()} model calls, ${sum('tts_calls').toLocaleString()} voice requests (${sum('tts_chars').toLocaleString()} characters)${hits ? `, ${hits.toLocaleString()} reused from earlier recordings` : ''}${cost ? `, about $${cost.toFixed(2)}` : ''}.</p>`;
  }
  $('#h-top').innerHTML = `<div class="home-top"><div><h1>Your studio</h1><p class="ledger">${line}</p>${behind}</div>
    <button class="btn primary" type="button" data-home="new">New episode</button></div>`;
}

function homeLive() {
  const busy = S.projects.filter((p) => groupOf(p) === 'working');
  if (!busy.length) { $('#h-live').innerHTML = ''; return; }
  $('#h-live').innerHTML = `<section class="panel live-box" aria-label="Being made now">${busy.map((p) => {
    const d = S.detail[p.id], pr = d?.job?.progress;
    return `<div class="live"><span class="dot run"></span>
      <div class="live-t"><b>${esc(p.title || 'Untitled')}</b><small>${esc(statusLabel(p.status))}${(d?.job?.message || p.message) ? ': ' + esc(d?.job?.message || p.message) : ''}</small></div>
      <div class="progress"><i style="width:${Math.round((pr || 0) * 100)}%"></i></div>
      <button class="btn small" type="button" data-pid="${esc(p.id)}">Watch</button></div>`;
  }).join('')}</section>`;
}

/* The "who speaks when" strip, shrunk to fit a card. */
function miniStrip(p) {
  const f = p.final;
  if (!f || !f.duration || f.chapters.length !== p.segments.length) return '<div class="mini empty"></div>';
  const bars = [];
  p.segments.forEach((s, i) => {
    const v = s.version_obj;
    if (!v?.turn_times?.length) return;
    const start = f.chapters[i].start;
    v.turns.forEach((t, j) => {
      const tt = v.turn_times[j];
      if (!tt) return;
      bars.push(`<i class="${t.speaker}" style="left:${((start + tt[0]) / f.duration * 100).toFixed(2)}%;width:${Math.max((tt[1] - tt[0]) / f.duration * 100, 0.25).toFixed(2)}%"></i>`);
    });
  });
  return `<div class="mini" aria-hidden="true">${bars.join('')}</div>`;
}

function episodeCard(p) {
  const d = S.detail[p.id], g = groupOf(p), f = d?.p.final;
  let viz;
  if (g === 'working') {
    viz = `<div class="progress"><i style="width:${Math.round((d?.job?.progress || 0) * 100)}%"></i></div><div class="hint">${esc(d?.job?.message || p.message || statusLabel(p.status))}</div>`;
  } else if (f) {
    viz = d.mini ||= miniStrip(d.p);
  } else if (g === 'ready') {
    viz = '<div class="mini loading"></div>';
  } else {
    viz = `<div class="mini empty"></div><div class="hint">${esc(p.message || statusLabel(p.status))}</div>`;
  }
  const base = `/api/projects/${encodeURIComponent(p.id)}/files/`;
  return `<article class="ecard ${g}" data-pid="${esc(p.id)}">
    <h3><button type="button" class="ecard-t" data-pid="${esc(p.id)}">${esc(p.title || 'Untitled')}</button></h3>
    <div class="ecard-meta"><span class="badge ${g === 'ready' ? 'ok' : p.status === 'error' ? 'err' : g === 'attention' ? 'warn' : ''}">${esc(statusLabel(p.status))}</span>
      <span>${esc(langName(p.language))}</span>${p.duration ? `<span>${fmt(p.duration)}</span>` : ''}${p.segments ? `<span>${p.segments} segments</span>` : ''}<span>${esc(ago(p.updated_at))}</span></div>
    ${viz}
    <div class="ecard-acts"><button type="button" class="btn small ${g === 'ready' ? 'primary' : ''}" data-pid="${esc(p.id)}">${g === 'ready' ? 'Listen' : g === 'working' ? 'Watch' : 'Open'}</button>
      ${f?.mp3 ? `<a class="btn small" href="${base}${f.mp3}?download=1">MP3</a>` : ''}</div>
  </article>`;
}

function homeGrid() {
  const list = filteredProjects();
  if (!S.projects.length) {
    $('#h-grid').innerHTML = `<div class="panel empty-home"><h2>Make your first episode</h2><p class="hint">Upload a chapter and two hosts will talk it through. You can listen, edit any line and redo any segment.</p>
      <button class="btn primary" type="button" data-home="new">Create a podcast</button></div>`;
    return;
  }
  if (!list.length) {
    $('#h-grid').innerHTML = `<div class="panel"><p class="hint" style="margin:0">No episodes match${S.q.trim() ? ` "${esc(S.q.trim())}"` : ' this filter'}. <button class="link" type="button" data-home="clear">Show everything</button></p></div>`;
    return;
  }
  $('#h-grid').innerHTML = `<div class="egrid">${list.map(episodeCard).join('')}</div>`;
}

/* ---------------------------------------------------------------- setup check */
async function showDoctor() {
  const dlg = $('#dlg');
  $('#dlg-body').innerHTML = '<div class="dlg-in"><h2>Checking your setup...</h2><p class="hint">This makes a few tiny test calls and takes a few seconds.</p></div>';
  dlg.showModal();
  try {
    const { checks } = await api('/api/doctor');
    $('#dlg-body').innerHTML = `<div class="dlg-in"><h2>Setup check</h2>
      <div>${checks.map((c) => `<div class="check"><span class="dot ${c.ok ? 'ready' : 'error'}"></span><div><b>${esc(c.name)}</b>${esc(c.detail)}${c.hint ? `<div class="hint">${esc(c.hint)}</div>` : ''}</div></div>`).join('')}</div>
      <div class="row end"><button class="btn primary" onclick="document.getElementById('dlg').close()">Close</button></div></div>`;
  } catch (e) {
    $('#dlg-body').innerHTML = `<div class="dlg-in"><h2>Setup check failed</h2><div class="notice err">${esc(e.message)}</div><div class="row end"><button class="btn" onclick="document.getElementById('dlg').close()">Close</button></div></div>`;
  }
}

/* ---------------------------------------------------------------- keys, tuning, quality */
const closeBtn = '<button class="btn" type="button" onclick="document.getElementById(\'dlg\').close()">Close</button>';

function showKeys() {
  const c = S.config, ks = c.key_status || {};
  const state = (n) => { const x = ks[n] || { count: 0, tails: [] }; return x.count ? `<span class="ok-txt">${x.count} saved (${x.tails.map(esc).join(', ')})</span>` : '<span class="hint">not set</span>'; };
  $('#dlg-body').innerHTML = `<form class="dlg-in" id="keys-form" autocomplete="off">
    <h2>API keys and models</h2>
    <p class="hint">Paste a key into the box where it belongs. Keys are saved only in the <code>.env</code> file on this computer, are never shown again
    (just the last 4 characters) and are only sent to OpenRouter / Bodhan. Leave a box empty to keep what is saved.
    Several keys? Separate them with commas, and the next is used automatically when one runs out of credit.</p>
    <div class="notice ${c.missing_keys.length ? 'err' : 'info'}">${c.missing_keys.length ? 'Still missing: <b>' + c.missing_keys.map(esc).join(', ') + '</b>' : 'Required keys are set.'}${c.mock ? ' Demo mode is ON (no real calls).' : ''}</div>
    <label class="field"><span>OpenRouter key(s) &middot; ${state('OPENROUTER_API_KEY')}</span><span class="hint">writes the script. From the TA e-mail "[DA7016] - Access to OpenRouter Credits"</span>
      <input type="password" name="openrouter_api_key" placeholder="sk-or-..." spellcheck="false"></label>
    <label class="field"><span>Bodhan TTS key(s) &middot; ${state('BODHAN_TTS_API_KEY')}</span><span class="hint">the voices. console.bodhan.ai &rarr; API keys &rarr; create a key with model <b>indic-speak</b></span>
      <input type="password" name="bodhan_tts_api_key" spellcheck="false"></label>
    <label class="field"><span>Bodhan translation key(s) &middot; ${state('BODHAN_MT_API_KEY')}</span><span class="hint">optional. A key with model <b>indic-translate</b>; only the "translate" writing mode needs it</span>
      <input type="password" name="bodhan_mt_api_key" spellcheck="false"></label>
    <div class="grid2">
      <label class="field">Writer model <input name="model_writer" placeholder="${esc(c.models.writer)}" spellcheck="false"></label>
      <label class="field">Fact-checker model <input name="model_checker" placeholder="${esc(c.models.checker)}" spellcheck="false"></label>
    </div>
    <label class="check-row"><input type="checkbox" name="mock" ${c.mock ? 'checked' : ''}> Demo mode (offline, fake beep voices, no credits used)</label>
    <div class="row end">${closeBtn}<button class="btn primary" type="submit">Save and test</button></div></form>`;
  $('#dlg').showModal();
  $('#keys-form').onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { mock: f.get('mock') === 'on' };
    for (const k of ['openrouter_api_key', 'bodhan_tts_api_key', 'bodhan_mt_api_key', 'model_writer', 'model_checker']) if ((f.get(k) || '').trim()) body[k] = f.get(k).trim();
    try {
      await post('/api/setup/keys', body);
      S.config = await api('/api/config');
      $('#mock-badge').hidden = !S.config.mock;
      showDoctor();
    } catch (err) { toast(err.message, true); }
  };
}

const TUNING_INFO = {
  writer_temperature: ['Creativity of the script', '0.3 plain and safe · 0.85 natural · 1.1 very chatty (more risk of drifting from the chapter)', 0, 1.5, 0.05],
  words_per_minute_scale: ['Density', 'Above 1 packs more words per minute; below 1 gives a slower, airier episode', 0.6, 1.5, 0.05],
  gap_same_speaker: ['Pause, same speaker (s)', 'Between two lines by the same host', 0.05, 1, 0.02],
  gap_speaker_change: ['Pause, speaker change (s)', 'Smaller = snappier conversation', 0.05, 1.5, 0.02],
  gap_after_question: ['Extra pause after a question (s)', 'Gives the answer room to land', 0, 0.8, 0.02],
  gap_jitter: ['Rhythm variation (s)', 'Random +/- so timing is not robotic', 0, 0.2, 0.01],
  reaction_overlap: ['Interjection overlap (s)', 'How far a quick "hmm" may cut into the speaker. 0 = never', 0, 0.5, 0.02],
  segment_gap: ['Pause between segments (s)', '', 0.1, 2.5, 0.05],
  speech_level_db: ['Speech level (dB)', 'Before the final loudness pass', -30, -10, 1],
  final_loudness_lufs: ['Final loudness (LUFS)', '-16 is the podcast standard', -24, -10, 1],
  jingle_gain: ['Jingle volume', '0 turns the chimes off', 0, 1.5, 0.05],
  director_temperature: ['Video: visual creativity', 'How freely the model chooses slide layouts (0.3 conservative, 0.8 varied)', 0, 1, 0.05],
  scene_words: ['Video: words per scene', 'Spoken words before the slide changes. Smaller = more, shorter slides', 25, 140, 5],
};

async function showTuning() {
  let t;
  try { t = await api('/api/tuning'); } catch (e) { toast(e.message, true); return; }
  // only the knobs this backend actually has (an audio-only backend has no video settings)
  const known = Object.entries(TUNING_INFO).filter(([k]) => k in t.values);
  const rows = known.map(([k, [label, hint, lo, hi, step]]) => `
    <label class="field">${esc(label)}${hint ? ` <span class="hint">${esc(hint)}</span>` : ''}
      <input type="number" name="${k}" min="${lo}" max="${hi}" step="${step}" value="${t.values[k]}"></label>`).join('');
  $('#dlg-body').innerHTML = `<form class="dlg-in" id="tune-form">
    <h2>Tuning</h2>
    <p class="hint">Changes the style of <b>new</b> episodes. To hear pacing/level changes on an existing episode, save and then press <b>Re-mix</b> on it (no voice credits are used).</p>
    <div class="row">${t.presets.map((p) => `<button class="btn small" type="button" data-preset="${p}">${esc(p)}</button>`).join('')}</div>
    <div class="grid2">${rows}</div>
    <div class="row end">${closeBtn}<button class="btn primary" type="submit">Save</button></div></form>`;
  $('#dlg').showModal();
  $('#tune-form').onclick = async (e) => {
    const b = e.target.closest('[data-preset]');
    if (!b) return;
    try { await api('/api/tuning', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preset: b.dataset.preset }) }); toast(`Preset "${b.dataset.preset}" applied.`); showTuning(); } catch (err) { toast(err.message, true); }
  };
  $('#tune-form').onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    for (const [k] of known) values[k] = parseFloat(e.target.elements[k].value);
    try { await api('/api/tuning', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values }) }); toast('Tuning saved.'); $('#dlg').close(); } catch (err) { toast(err.message, true); }
  };
}

async function showQuality(pid, deep) {
  $('#dlg-body').innerHTML = `<div class="dlg-in"><h2>Checking quality...</h2><p class="hint">${deep ? 'Running a fresh fact-check of every segment against the chapter. This makes a few model calls.' : 'This takes a moment.'}</p></div>`;
  $('#dlg').showModal();
  try {
    const r = await api(`/api/projects/${encodeURIComponent(pid)}/validate?deep=${deep ? 1 : 0}`, { method: 'POST' });
    const cls = { pass: 'ready', warn: 'script_ready', fail: 'error' };
    let area = '';
    const rows = r.checks.map((c) => {
      const head = c.area !== area ? `<div class="hint" style="margin-top:10px;text-transform:uppercase;letter-spacing:.04em">${esc(c.area)}</div>` : '';
      area = c.area;
      return `${head}<div class="check"><span class="dot ${cls[c.status]}"></span><div><b>${esc(c.name)}</b> ${esc(c.detail)}</div></div>`;
    }).join('');
    $('#dlg-body').innerHTML = `<div class="dlg-in"><h2>Quality report <span class="badge ${r.verdict === 'pass' ? 'ok' : r.verdict === 'fail' ? 'err' : 'warn'}">${r.verdict.toUpperCase()} · ${r.score}/100</span></h2>
      <p class="hint">${r.counts.pass || 0} passed · ${r.counts.warn || 0} warnings · ${r.counts.fail || 0} failed. ${r.deep ? '' : 'The deep check re-verifies every claim with the model (a few calls).'}</p>
      <div style="max-height:55vh;overflow:auto">${rows}</div>
      <div class="row end">${closeBtn}${r.deep ? '' : '<button class="btn primary" id="q-deep" type="button">Run deep fact-check</button>'}</div></div>`;
    $('#q-deep')?.addEventListener('click', () => showQuality(pid, true));
  } catch (e) {
    $('#dlg-body').innerHTML = `<div class="dlg-in"><h2>Quality check failed</h2><div class="notice err">${esc(e.message)}</div><div class="row end">${closeBtn}</div></div>`;
  }
}

init();
