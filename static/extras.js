'use strict';
/* Podcast Studio - study pack, "Ask the hosts" and the quality dashboard.
   Optional add-on: it needs the extras backend (podcast_studio/extras) and static/extras.css. app.js loads this file on demand and
   simply carries on without it, so the rest of the app never depends on it. It shares app.js's globals (S, ST, api, post, esc, fmt ...). */
(function () {
  const P = {
    mounted: false, pid: null, tab: 'episode', usable: false, tabsSig: '',
    learn: null, learnTimer: 0, sub: 'quiz', quiz: null, flash: false, glossQ: '',
    asks: null, asking: false, askMsg: '', askTimer: 0, askDraft: '', hints: null, saved: null,
    ins: null, insKey: '', insBusy: false, insErr: '',
  };
  window.Extras = { mount, unmount, update, onEnded };

  const I = {
    book: '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M5 4.5h9.5A3.5 3.5 0 0 1 18 8v11.5H8.5A3.5 3.5 0 0 1 5 16zM5 16a3.5 3.5 0 0 1 3.5-3.5H18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
    ask: '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M4 5.5h16v10H12l-4.5 4v-4H4z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M9.8 9.4c.3-1.3 1.3-1.8 2.2-1.8 1.2 0 2.1.7 2.1 1.7 0 1.3-1.6 1.4-2 2.4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="12.1" cy="13.2" r=".9" fill="currentColor"/></svg>',
    gauge: '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M4 18a8 8 0 1 1 16 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M12 18l4-6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="18" r="1.6" fill="currentColor"/></svg>',
    list: '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M5 6.5h14M5 12h14M5 17.5h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    play: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>',
    stop: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/></svg>',
    check: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    cross: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
    print: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M7 9V4h10v5M7 17H5v-7h14v7h-2M8 14h8v6H8z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
    send: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M4 12l16-7-6 15-3-6z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>',
  };
  const TABS = [['episode', 'Episode', I.list], ['learn', 'Learn', I.book], ['ask', 'Ask the hosts', I.ask], ['quality', 'Quality', I.gauge]];
  const u = (suffix) => `/api/projects/${encodeURIComponent(S.pid)}${suffix}`;
  const names = () => ({ A: S.project.settings.host_a.name, B: S.project.settings.host_b.name });
  const pct = (x) => (x == null ? 'n/a' : Math.round(x * 100) + '%');
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode: not remembered */ } },
  };
  const lg = () => S.project?.settings?.language || 'en';

  /* ================================================================ lifecycle (called by app.js) */
  function mount() {
    if (!$('#p-tabs')) return;
    if (!document.getElementById('extras-css')) {
      const l = document.createElement('link');
      l.id = 'extras-css'; l.rel = 'stylesheet'; l.href = '/static/extras.css';
      document.head.appendChild(l);
    }
    Object.assign(P, { mounted: true, pid: S.pid, tab: 'episode', tabsSig: '', learn: null, quiz: null, asks: null, hints: null, ins: null, insKey: '', insErr: '', asking: false, sub: 'quiz', saved: null });
    $('#p-tabs').addEventListener('click', onTabClick);
    $('#p-tabs').addEventListener('keydown', onTabKey);
    $('#tab-learn').addEventListener('click', onLearnClick);
    $('#tab-learn').addEventListener('input', onLearnInput);
    $('#tab-ask').addEventListener('click', onAskClick);
    $('#tab-ask').addEventListener('submit', onAskSubmit);
    $('#tab-ask').addEventListener('keydown', onAskKey);
    $('#tab-quality').addEventListener('click', onQualityClick);
    document.addEventListener('keydown', onQuizKey);
    update();
  }

  function unmount() {
    clearTimeout(P.learnTimer); clearTimeout(P.askTimer);
    exitAsk(true);
    document.removeEventListener('keydown', onQuizKey);
    P.mounted = false;
  }

  function usable() {
    const p = S.project;
    return !!p && ['ready', 'script_ready'].includes(p.status) && !isActive() && p.segments.length > 0 && p.segments.every((s) => s.versions.length);
  }

  function update() {
    if (!P.mounted || P.pid !== S.pid || !S.project) return;
    P.usable = usable();
    if (S.ask && isActive()) exitAsk();
    const sig = JSON.stringify([P.usable, P.tab, P.learn?.status, P.learn?.kept, P.asks?.length, P.ins?.validation?.score, S.project.updated_at]);
    if (sig === P.tabsSig) return;
    P.tabsSig = sig;
    renderTabs();
    if (!P.usable && P.tab !== 'episode') showTab('episode');
    // the episode changed (a segment was rewritten, audio rebuilt): the saved insights are out of date
    if (P.tab === 'quality' && P.ins && P.insKey !== insightsKey()) loadInsights();
  }

  const insightsKey = () => `${S.project.updated_at}|${S.project.final?.built_at || ''}|${S.project.usage?.llm_calls}`;

  /* ================================================================ tabs */
  function renderTabs() {
    const box = $('#p-tabs');
    if (!P.usable) { box.innerHTML = ''; return; }
    const badge = { learn: P.learn?.status === 'ready' ? P.learn.quiz.length + ' Q' : '', ask: P.asks?.length || '', quality: P.ins?.validation ? P.ins.validation.score : '' };
    box.innerHTML = `<div class="ptabs" role="tablist" aria-label="Episode sections">${TABS.map(([k, label, ic]) =>
      `<button type="button" role="tab" id="ptab-${k}" data-ptab="${k}" aria-selected="${P.tab === k}" aria-controls="${k === 'episode' ? 'tab-episode' : 'tab-' + k}" tabindex="${P.tab === k ? 0 : -1}">${ic}<span>${label}</span>${badge[k] ? `<em class="pt-badge">${esc(badge[k])}</em>` : ''}</button>`).join('')}</div>`;
  }

  function onTabClick(e) {
    const b = e.target.closest('[data-ptab]');
    if (b) showTab(b.dataset.ptab);
  }
  function onTabKey(e) {
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
    const keys = TABS.map((t) => t[0]);
    let i = keys.indexOf(P.tab);
    i = e.key === 'Home' ? 0 : e.key === 'End' ? keys.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length;
    e.preventDefault();
    showTab(keys[i]);
    $(`#ptab-${keys[i]}`)?.focus();
  }

  function showTab(name) {
    if (!P.usable && name !== 'episode') name = 'episode';
    P.tab = name;
    $('#tab-episode').hidden = name !== 'episode';
    ['learn', 'ask', 'quality'].forEach((k) => { $('#tab-' + k).hidden = name !== k; });
    if (name === 'episode' && S.ask) exitAsk();
    P.tabsSig = '';
    renderTabs();
    if (name === 'learn') { renderLearn(); if (!P.learn || P.learn.status === 'working') loadLearn(); }
    if (name === 'ask') { renderAsk(); if (!P.asks) loadAsks(); }
    if (name === 'quality') { renderQuality(); if (!P.ins || P.insKey !== insightsKey()) loadInsights(); }
  }

  /* ================================================================ LEARN: takeaways, glossary, quiz */
  async function loadLearn() {
    clearTimeout(P.learnTimer);
    const pid = S.pid;
    try { P.learn = await api(u('/learn')); } catch (e) { P.learn = { status: 'error', message: e.message }; }
    if (S.pid !== pid || !P.mounted) return;
    P.tabsSig = '';
    if (P.tab === 'learn') renderLearn();
    update();
    if (P.learn.status === 'working') P.learnTimer = setTimeout(loadLearn, 1200);
  }

  async function startLearn(regen) {
    try { P.learn = await post(u('/learn'), { regenerate: !!regen }); } catch (e) { toast(e.message, true); return; }
    P.quiz = null; P.sub = 'quiz';
    renderLearn();
    if (P.learn.status === 'working') P.learnTimer = setTimeout(loadLearn, 900);
  }

  function renderLearn() {
    const box = $('#tab-learn');
    const d = P.learn;
    if (!d) { box.innerHTML = '<div class="pp"><div class="hint">Loading…</div></div>'; return; }
    if (d.status === 'none') {
      box.innerHTML = `<div class="pp hero">
        <div class="hero-art" aria-hidden="true"><span>?</span><span>A</span><span>✓</span></div>
        <div><h2>Turn this episode into a study pack</h2>
        <p class="mut">Key takeaways, a glossary and a multiple-choice quiz, all made from this chapter. Every item is checked against the chapter before you see it, and anything that is not supported is thrown away.</p>
        <ul class="ticks"><li>Takeaways in the chapter's order</li><li>Glossary with flashcard mode</li><li>Quiz with explanations that point back to the chapter</li></ul>
        <button class="btn primary" data-learn="start">Create study pack</button>
        <span class="mut psm">${S.config.mock ? 'Demo mode: built from the chapter\'s own sentences, no credits used.' : 'Uses about two model calls.'}</span></div></div>`;
      return;
    }
    if (d.status === 'working') {
      box.innerHTML = `<div class="pp"><div class="pworking"><span class="spin" aria-hidden="true"></span><div><strong>${esc(d.message || 'Working…')}</strong><div class="mut psm">The study pack is being written and checked against the chapter.</div></div></div>
        <div class="skel"><i></i><i></i><i></i></div></div>`;
      return;
    }
    if (d.status === 'error') {
      box.innerHTML = `<div class="pp"><div class="notice err">${esc(d.message || 'Something went wrong.')}</div><p><button class="btn primary" data-learn="start">Try again</button></p></div>`;
      return;
    }
    const m = d.meta || {}, k = m.kept || {}, dropped = (m.dropped || []).length;
    const chips = [`<span class="badge ok">${I.check} Checked against the chapter</span>`,
      dropped ? `<span class="badge" title="${esc((m.dropped || []).map((x) => x.why).join('; '))}">${dropped} unsupported item${dropped > 1 ? 's' : ''} removed</span>` : '',
      m.verified_by_model ? '<span class="badge">Second-model check</span>' : '',
      d.translated ? `<span class="badge">Translated into ${esc(langName(d.language))}</span>` : '',
      m.offline ? '<span class="badge mock">Demo mode</span>' : ''].join('');
    const subs = [['quiz', 'Quiz', k.quiz], ['takeaways', 'Takeaways', k.takeaways], ['glossary', 'Glossary', k.glossary]];
    box.innerHTML = `<div class="pp" lang="${esc(d.language)}">
      <div class="pp-head"><div><h2>Study pack</h2><div class="pchips">${chips}</div></div>
        <div class="pp-acts"><a class="btn" href="${u('/learn/sheet')}" target="_blank" rel="noopener">${I.print} Printable sheet</a><button class="btn quiet" data-learn="regen" title="Write a fresh pack">New pack</button></div></div>
      <div class="seg-tabs lsub" role="group" aria-label="Study pack sections">${subs.map(([id, label, n]) => `<button type="button" data-lsub="${id}" aria-pressed="${P.sub === id}">${label} <span class="n">${n ?? ''}</span></button>`).join('')}</div>
      <div id="learn-body">${P.sub === 'quiz' ? quizHTML(d) : P.sub === 'takeaways' ? takeawaysHTML(d) : glossaryHTML(d)}</div></div>`;
  }

  const srcLine = (it) => (it.source?.section ? `<small class="src">From the chapter: ${esc(it.source.section)}</small>` : '');
  function takeawaysHTML(d) {
    return `<ol class="tks">${d.takeaways.map((t, i) => `<li><span class="tk-n">${i + 1}</span><div><p>${esc(t.text)}</p>${srcLine(t)}</div></li>`).join('')}</ol>`;
  }
  function glossaryHTML(d) {
    const q = P.glossQ.trim().toLowerCase();
    const rows = d.glossary.map((g, i) => [g, i]).filter(([g]) => !q || g.term.toLowerCase().includes(q) || g.definition.toLowerCase().includes(q));
    return `<div class="gl-bar"><input type="search" id="gl-q" placeholder="Search terms" value="${esc(P.glossQ)}" aria-label="Search the glossary">
      <label class="switch"><input type="checkbox" id="gl-fc" ${P.flash ? 'checked' : ''}><span>Flashcard mode</span></label></div>
      <div class="gl-grid ${P.flash ? 'fc' : ''}">${rows.map(([g, i]) => `<article class="gl" data-gl="${i}" tabindex="0"><h4>${esc(g.term)}</h4><p>${esc(g.definition)}</p>${srcLine(g)}${P.flash ? '<span class="tap">Tap to reveal</span>' : ''}</article>`).join('') || '<div class="hint">No terms match.</div>'}</div>`;
  }

  /* ---- quiz */
  function qs(d) { return P.quiz ||= { order: d.quiz.map((_, i) => i), pos: 0, picks: {}, done: false }; }
  function quizHTML(d) {
    const z = qs(d);
    if (!d.quiz.length) return '<div class="hint">No quiz questions passed the chapter check. Try a new pack.</div>';
    if (z.done) return quizDone(d, z);
    const qi = z.order[z.pos], q = d.quiz[qi], pick = z.picks[qi], answered = pick !== undefined;
    const dots = z.order.map((idx, j) => { const pk = z.picks[idx]; return `<i class="${j === z.pos ? 'cur ' : ''}${pk === undefined ? '' : pk === d.quiz[idx].answer ? 'ok' : 'bad'}"></i>`; }).join('');
    return `<div class="qz" data-qi="${qi}">
      <div class="qz-top"><span class="qz-count">Question ${z.pos + 1} of ${z.order.length}</span><span class="lvl ${esc(q.level)}">${esc(q.level)}</span><span class="qz-dots" aria-hidden="true">${dots}</span></div>
      <h3 class="qz-q">${esc(q.question)}</h3>
      <div class="qz-opts" role="group" aria-label="Answers">${q.options.map((o, k) => {
        const st = !answered ? '' : k === q.answer ? 'ok' : k === pick ? 'bad' : 'dim';
        return `<button type="button" class="qz-opt ${st}" data-opt="${k}" ${answered ? 'disabled' : ''}><b>${'ABCD'[k]}</b><span>${esc(o)}</span>${st === 'ok' ? `<em>${I.check}</em>` : st === 'bad' ? `<em>${I.cross}</em>` : ''}</button>`;
      }).join('')}</div>
      ${answered ? `<div class="qz-fb ${pick === q.answer ? 'ok' : 'bad'}" role="status"><strong>${pick === q.answer ? 'Correct.' : 'Not quite.'}</strong> ${esc(q.explanation)}
        ${q.source ? `<details><summary>See it in the chapter</summary><p class="mut">${esc(q.source.excerpt)}</p><small class="src">${esc(q.source.section || '')}</small></details>` : ''}</div>
        <div class="qz-nav"><button class="btn primary" data-qz="next">${z.pos + 1 < z.order.length ? 'Next question' : 'See my score'}</button></div>`
      : '<div class="mut psm qz-hint">Pick an answer, or press 1 to 4.</div>'}
    </div>`;
  }
  function quizDone(d, z) {
    const right = z.order.filter((i) => z.picks[i] === d.quiz[i].answer).length, n = z.order.length;
    const best = store.get('ps-quiz-' + S.pid);
    const isFull = n === d.quiz.length;
    if (isFull && (!best || right > best.best)) store.set('ps-quiz-' + S.pid, { best: right, total: n });
    const shown = isFull ? store.get('ps-quiz-' + S.pid) : best;
    const missed = z.order.filter((i) => z.picks[i] !== d.quiz[i].answer);
    const f = right / n, msg = f === 1 ? 'Perfect. You could host this episode.' : f >= 0.75 ? 'Strong. Review the ones you missed.' : f >= 0.5 ? 'Good start. The explanations below will help.' : 'Listen again, then retry. It sticks.';
    const C = 2 * Math.PI * 52;
    return `<div class="qz-done"><div class="ring sm" aria-label="${right} out of ${n}"><svg viewBox="0 0 120 120"><circle class="rg-bg" cx="60" cy="60" r="52"/><circle class="rg-fg" cx="60" cy="60" r="52" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - f)).toFixed(1)}"/></svg><b>${right}<small>/${n}</small></b></div>
      <div><h3>${esc(msg)}</h3>${shown ? `<p class="mut psm">Your best on this episode: ${shown.best}/${shown.total}</p>` : ''}
        <div class="pp-acts"><button class="btn primary" data-qz="retry">Try again</button>${missed.length ? `<button class="btn" data-qz="missed">Retry the ${missed.length} I missed</button>` : ''}</div></div></div>
      <ol class="qz-rev">${z.order.map((i) => { const q = d.quiz[i], ok = z.picks[i] === q.answer; return `<li class="${ok ? 'ok' : 'bad'}"><span class="mark">${ok ? I.check : I.cross}</span><div><p>${esc(q.question)}</p><small>Answer: <b>${esc(q.options[q.answer])}</b>${ok ? '' : ` · you chose ${esc(q.options[z.picks[i]] ?? '')}`}</small></div></li>`; }).join('')}</ol>`;
  }
  function pickOption(k) {
    const d = P.learn, z = qs(d);
    if (z.done || z.picks[z.order[z.pos]] !== undefined) return;
    z.picks[z.order[z.pos]] = k;
    renderLearn();
    $('.qz-nav .btn', $('#tab-learn'))?.focus({ preventScroll: true });
  }
  function onQuizKey(e) {
    if (!P.mounted || P.tab !== 'learn' || P.sub !== 'quiz' || P.learn?.status !== 'ready' || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest?.('input, textarea, select')) return;
    if (/^[1-4]$/.test(e.key)) { e.preventDefault(); pickOption(Number(e.key) - 1); }
  }

  function onLearnClick(e) {
    const t = e.target;
    const a = t.closest('[data-learn]');
    if (a) {
      if (a.dataset.learn === 'regen' && !confirm('Write a new study pack? The current one is replaced.')) return;
      return startLearn(a.dataset.learn === 'regen');
    }
    const s = t.closest('[data-lsub]');
    if (s) { P.sub = s.dataset.lsub; return renderLearn(); }
    const o = t.closest('[data-opt]');
    if (o) return pickOption(Number(o.dataset.opt));
    const z = t.closest('[data-qz]');
    if (z) {
      const q = qs(P.learn), d = P.learn;
      if (z.dataset.qz === 'next') { if (q.pos + 1 < q.order.length) q.pos++; else q.done = true; }
      else if (z.dataset.qz === 'retry') P.quiz = null;
      else if (z.dataset.qz === 'missed') { const miss = q.order.filter((i) => q.picks[i] !== d.quiz[i].answer); P.quiz = { order: miss, pos: 0, picks: {}, done: false }; }
      renderLearn();
      return $('#tab-learn').scrollIntoView({ block: 'nearest' });
    }
    const g = t.closest('[data-gl]');
    if (g && P.flash) g.classList.toggle('show');
  }
  function onLearnInput(e) {
    if (e.target.id === 'gl-q') {
      P.glossQ = e.target.value;
      const body = $('#learn-body'), pos = e.target.selectionStart;
      body.innerHTML = glossaryHTML(P.learn);
      const inp = $('#gl-q'); inp.focus(); inp.setSelectionRange(pos, pos);
    } else if (e.target.id === 'gl-fc') { P.flash = e.target.checked; $('#learn-body').innerHTML = glossaryHTML(P.learn); }
  }

  /* ================================================================ ASK THE HOSTS */
  async function loadAsks() {
    const pid = S.pid;
    try { P.asks = (await api(u('/ask'))).answers; } catch (_) { P.asks = []; }
    if (S.pid !== pid || !P.mounted) return;
    P.tabsSig = ''; update();
    if (P.tab === 'ask') renderAsk();
    if (!P.hints) buildHints();
  }

  /* Suggestions come from the chapter itself: glossary terms when a study pack exists, otherwise segment titles. */
  async function buildHints() {
    const pid = S.pid, out = [];
    let terms = [];
    try { const d = P.learn?.status === 'ready' ? P.learn : await api(u('/learn')); if (d.status === 'ready') terms = d.glossary.map((g) => g.term); } catch (_) { /* hints are optional */ }
    terms.slice(0, 2).forEach((t) => out.push(`What is ${t}?`));
    S.project.segments.filter((s) => s.kind === 'body').slice(0, 4 - out.length).forEach((s) => out.push(`Can you explain ${s.title.replace(/[.?!]+$/, '')} again, simply?`));
    if (S.pid !== pid) return;
    P.hints = out.slice(0, 3);
    if (P.tab === 'ask' && !P.asking) renderAsk();
  }

  function renderAsk() {
    const box = $('#tab-ask');
    const keep = $('#ask-q', box)?.value ?? P.askDraft;
    const n = names();
    const rows = P.asks || [];
    const canStage = !!S.project.final;
    box.innerHTML = `<div class="pp">
      <div class="pp-head"><div><h2>Ask the hosts</h2><p class="mut">Type a question about this chapter. ${esc(n.A)} and ${esc(n.B)} answer in their own voices, only from what the chapter says.</p></div>
        ${rows.length ? '<button class="btn quiet" data-ask="clear">Clear conversation</button>' : ''}</div>
      <form id="ask-form" class="ask-form" autocomplete="off">
        <textarea id="ask-q" rows="2" maxlength="400" placeholder="Ask anything about this chapter…" aria-label="Your question" ${P.asking ? 'disabled' : ''}>${esc(keep)}</textarea>
        <button class="btn primary" type="submit" ${P.asking ? 'disabled' : ''}>${I.send} Ask</button>
      </form>
      ${P.hints?.length && !P.asking ? `<div class="hints" aria-label="Suggested questions">${P.hints.map((h) => `<button type="button" class="hint-chip" data-hint="${esc(h)}">${esc(h)}</button>`).join('')}<button type="button" class="hint-chip probe" data-hint="Who won the last cricket world cup?" title="Shows what the hosts do with a question the chapter cannot answer">Off-topic test</button></div>` : ''}
      ${P.asking ? `<div class="thinking" role="status"><span class="av-dots"><i style="background:var(--a)"></i><i style="background:var(--b)"></i></span><div><strong>${esc(n.A)} and ${esc(n.B)} are on it…</strong><div class="mut psm">${esc(P.askMsg)}</div></div></div>` : ''}
      <div class="answers">${rows.slice().reverse().map((a) => answerHTML(a, canStage, n)).join('')}</div>
      ${!rows.length && !P.asking ? '<div class="hint empty-ask">No questions yet. Try one of the suggestions above.</div>' : ''}</div>`;
    if (S.ask) bindAskLines();
  }

  function answerHTML(a, canStage, n) {
    const status = a.needs_review ? ['err', 'Needs review'] : a.grounded ? ['ok', 'From the chapter'] : ['warn', 'Not in the chapter'];
    const playing = S.ask?.id === a.id;
    return `<article class="ans${playing ? ' playing' : ''}" data-id="${a.id}">
      <div class="ans-q"><span class="you">You</span><p>${esc(a.question)}</p></div>
      <div class="ans-card">
        <div class="ans-head"><span class="badge ${status[0]}">${status[1]}</span><span class="mut psm">${fmt(a.duration)}</span>
          ${canStage ? `<button class="btn primary small" data-ask="${playing ? 'stop' : 'play'}" data-id="${a.id}">${playing ? I.stop + ' Stop' : I.play + ' Play on stage'}</button>`
      : `<audio controls preload="none" src="${u('/' + a.audio)}"></audio>`}</div>
        <ol class="ans-lines" lang="${esc(a.language || lg())}">${a.turns.map((t, i) => `<li class="aline ${t.speaker}${t.reaction ? ' r' : ''}" data-i="${i}"><b>${esc(n[t.speaker])}</b> ${esc(t.text)}</li>`).join('')}</ol>
        ${a.sources?.length ? `<details class="ans-src"><summary>Where this came from (${a.sources.length} paragraph${a.sources.length > 1 ? 's' : ''} of the chapter)</summary><ul>${a.sources.map((s) => `<li><b>${esc(s.section || 'Chapter')}</b><span>${esc(s.excerpt)}</span></li>`).join('')}</ul></details>`
      : a.grounded ? '' : `<p class="mut psm ans-note">The hosts only answer from the chapter, so they decline rather than guess.${a.related?.length ? ' Closest parts: ' + a.related.map((r) => esc(r.title)).join(', ') + '.' : ''}</p>`}
      </div></article>`;
  }

  function onAskKey(e) {
    if (e.target.id === 'ask-q' && e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#ask-form').requestSubmit(); }
  }
  async function onAskSubmit(e) {
    e.preventDefault();
    const q = ($('#ask-q').value || '').trim();
    if (q.length < 3) { toast('Type a question first.', true); return; }
    if (P.asking) return;
    P.asking = true; P.askDraft = ''; P.askMsg = 'Finding the right paragraphs…';
    const steps = ['Finding the right paragraphs…', 'Writing the answer…', 'Checking it against the chapter…', 'Recording their voices…'];
    let si = 0;
    clearInterval(P.askTimer);
    P.askTimer = setInterval(() => { si = Math.min(si + 1, steps.length - 1); P.askMsg = steps[si]; const el = $('.thinking .psm', $('#tab-ask')); if (el) el.textContent = P.askMsg; }, 2600);
    renderAsk();
    const pid = S.pid;
    try {
      const a = await post(u('/ask'), { question: q });
      if (S.pid !== pid) return;
      (P.asks ||= []).push(a);
      P.asking = false;
      clearInterval(P.askTimer);
      P.tabsSig = ''; update(); renderAsk();
      refreshUsage();
      if (S.project.final) playAnswer(a.id);
    } catch (err) {
      if (S.pid !== pid) return;
      P.asking = false; P.askDraft = q; clearInterval(P.askTimer);
      toast(err.message, true);
      renderAsk();
    }
  }
  async function refreshUsage() {   // the question cost something: show it in the header like every other job
    try { const r = await api(`/api/projects/${encodeURIComponent(S.pid)}`); S.project = r.project; S.sigs.head = null; renderProject(); } catch (_) { /* cosmetic */ }
  }

  async function onAskClick(e) {
    const t = e.target;
    const h = t.closest('[data-hint]');
    if (h) { $('#ask-q').value = h.dataset.hint; $('#ask-form').requestSubmit(); return; }
    const a = t.closest('[data-ask]');
    if (!a) return;
    if (a.dataset.ask === 'play') playAnswer(Number(a.dataset.id));
    else if (a.dataset.ask === 'stop') exitAsk();
    else if (a.dataset.ask === 'clear') {
      if (!confirm('Delete all questions and answers for this episode?')) return;
      exitAsk(true);
      try { await api(u('/ask'), { method: 'DELETE' }); P.asks = []; P.tabsSig = ''; update(); renderAsk(); } catch (err) { toast(err.message, true); }
    }
  }

  /* ---- playing an answer on the stage: the answer's audio and lines temporarily replace the episode's in the shared player,
     so the 3D hosts, the caption and the dock all perform it. The episode is put back, paused where it was. */
  function playAnswer(id) {
    const a = (P.asks || []).find((x) => x.id === id);
    if (!a || !S.project.final) return;
    const au = $('#audio');
    if (!S.ask) P.saved = { t: au.currentTime, wasPlaying: !au.paused };
    au.pause();
    S.ask = { id, duration: a.duration };
    ST.main = ST.react = null; ST.capSig = null; ST.words = []; ST.cum = []; ST.lit = -1; ST.sec = -1;
    S.curLine = null;
    bindAskLines();
    S.audioKey = 'ask|' + id;
    $('#player').hidden = false;
    $('#player-chapter').textContent = 'Answer to your question';
    au.src = u('/' + a.audio);
    au.currentTime = 0;
    au.play().catch(() => toast('Press play to hear the answer.'));
    const tl = $('#tl-play'); if (tl) tl.style.display = 'none';
    renderAsk();
    if (!reduced()) stageRoot()?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function bindAskLines() {
    const a = (P.asks || []).find((x) => x.id === S.ask?.id);
    if (!a) return;
    const rows = $$(`#tab-ask .ans[data-id="${a.id}"] .aline`);
    S.lines = a.turns.map((t, i) => ({
      el: rows[i] || document.createElement('li'), start: t.start, end: t.end, seg: -1,
      speaker: t.speaker, react: !!t.reaction, text: t.text, style: t.style || '',
    }));
  }

  function exitAsk(silent) {
    if (!S.ask) return;
    const au = $('#audio');
    au.pause();
    const sv = P.saved;
    S.ask = null; P.saved = null;
    ST.main = ST.react = null; ST.capSig = null; ST.words = []; ST.cum = []; ST.lit = -1; ST.sec = -1; S.curLine = null;
    if (silent) { S.lines = []; return; }
    setTalking(null);
    S.audioKey = null;
    S.sigs.segs = null;                       // rebuilds S.lines for the episode
    renderProject();                          // puts the episode audio back (syncPlayer)
    if (sv?.t > 0) au.addEventListener('loadedmetadata', () => { au.currentTime = Math.min(sv.t, au.duration || sv.t); }, { once: true });
    if (P.tab === 'ask') renderAsk();
  }

  function onEnded() {
    if (!S.ask) return;
    toast('Back to the episode. It is paused where you left it.');
    exitAsk();
  }

  /* ================================================================ QUALITY dashboard */
  async function loadInsights() {
    const pid = S.pid, key = insightsKey();
    P.insBusy = true; P.insErr = '';
    if (P.tab === 'quality') renderQuality();
    try { P.ins = await api(u('/insights')); P.insKey = key; } catch (e) { P.insErr = e.message; }
    if (S.pid !== pid || !P.mounted) return;
    P.insBusy = false; P.tabsSig = ''; update();
    if (P.tab === 'quality') renderQuality();
  }

  const AREA = { structure: 'Structure', faithfulness: 'Faithfulness to the chapter', naturalness: 'Natural conversation', audio: 'Audio quality', outputs: 'Transcripts and subtitles', video: 'Video' };

  function renderQuality() {
    const box = $('#tab-quality');
    const d = P.ins;
    if (!d) {
      box.innerHTML = P.insErr ? `<div class="pp"><div class="notice err">${esc(P.insErr)}</div><p><button class="btn primary" data-q="refresh">Try again</button></p></div>`
        : '<div class="pp"><div class="pworking"><span class="spin" aria-hidden="true"></span><strong>Measuring the episode…</strong></div><div class="skel"><i></i><i></i><i></i></div></div>';
      return;
    }
    const v = d.validation, L = d.loop, sp = d.speaking, cov = d.coverage, gr = d.grounding, c = d.cost;
    const score = v ? v.score : null, C = 2 * Math.PI * 52;
    const passed = L.passed_first_try + L.repaired;
    const verdict = !v ? 'Not validated' : v.verdict === 'pass' ? 'All checks passed' : v.verdict === 'warn' ? 'Passed with warnings' : 'Needs attention';
    const sentence = [
      L.fact_check_on ? `${passed} of ${L.segments} segments passed the fact-checker${L.repaired ? `, ${L.repaired} of them only after the repair loop fixed problems` : passed === L.segments ? ', all on the first try' : ''}${L.needs_review ? `; ${L.needs_review} flagged for review` : ''}.` : 'The fact-checker was switched off for this episode.',
      gr ? (gr.turns_with_unsupported_numbers ? `${gr.turns_with_unsupported_numbers} line(s) mention numbers that are not in the chapter.` : 'Every number spoken appears in the chapter.') : '',
      cov ? `${pct(cov.covered_share)} of the chapter's paragraphs ${cov.comparable ? 'are reflected in the script' : 'were planned into a segment'}.` : '',
    ].filter(Boolean).join(' ');
    const tiles = [
      [`${passed}/${L.segments}`, 'segments fact-checked OK', L.needs_review ? 'bad' : 'ok'],
      [gr ? pct(gr.grounded_share) : 'n/a', 'lines with grounded numbers', gr && gr.grounded_share < 1 ? 'warn' : 'ok'],
      [cov ? pct(cov.covered_share) : 'n/a', 'chapter covered', cov && cov.covered_share < 0.85 ? 'warn' : 'ok'],
      [fmt(d.duration_s), `episode length${d.target_minutes ? ` · target ${Math.round(d.target_minutes)} min` : ''}`, ''],
      [c.usd ? '$' + c.usd.toFixed(2) : '$0', `${c.llm_calls} model calls${c.usd_per_audio_minute ? ` · $${c.usd_per_audio_minute.toFixed(3)} per minute` : ''}`, ''],
    ];
    const segN = Math.max(1, L.segments);
    const bar = (n, cls, label) => (n ? `<i class="${cls}" style="--w:${(100 * n / segN).toFixed(1)}%" title="${n} ${label}"></i>` : '');
    const checks = v ? Object.entries(v.checks.reduce((m, ch) => { (m[ch.area] ||= []).push(ch); return m; }, {})).map(([area, list]) => {
      const cnt = { pass: 0, warn: 0, fail: 0 };
      list.forEach((x) => { cnt[x.status]++; });
      return `<details class="qd-area" ${cnt.warn || cnt.fail ? 'open' : ''}><summary><b>${esc(AREA[area] || area)}</b><span class="cc">${cnt.fail ? `<em class="st fail">${cnt.fail} fail</em>` : ''}${cnt.warn ? `<em class="st warn">${cnt.warn} warn</em>` : ''}<em class="st pass">${cnt.pass} pass</em></span></summary>
        <ul>${list.map((x) => `<li><em class="st ${x.status}">${x.status}</em><div><b>${esc(x.name)}</b><span>${esc(x.detail)}</span></div></li>`).join('')}</ul></details>`;
    }).join('') : '<div class="hint">No validation available.</div>';
    const sm = d.script || {};
    const good = (ok) => (ok ? 'ok' : 'warn');
    const metr = sm.turns ? [
      [`${sm.avg_words_per_turn}`, 'words per turn', good(sm.avg_words_per_turn >= 8 && sm.avg_words_per_turn <= 38)],
      [pct(sm.speaker_switch_rate), 'turns change speaker', good(sm.speaker_switch_rate >= 0.7)],
      [pct(sm.question_share), 'are questions', good(sm.question_share >= 0.12 && sm.question_share <= 0.6)],
      [pct(sm.reaction_share), 'short reactions', good(sm.reaction_share >= 0.08)],
      [`${sm.longest_monologue}`, 'words, longest monologue', good(sm.longest_monologue <= 110)],
    ] : [];
    const rows = d.segments.map((r) => {
      const st = r.needs_review ? 'fail' : r.verdict === 'pass' ? 'pass' : r.verdict === 'revise' ? 'warn' : 'none';
      const maxw = Math.max(...d.segments.map((x) => x.words), 1);
      return `<tr data-segjump="${r.index - 1}" tabindex="0"><td class="num">${r.index}</td><td><span class="tt">${esc(r.title)}</span><small>${esc(r.kind)}${r.origin && r.origin !== 'generated' ? ' · ' + esc(r.origin) : ''}</small></td>
        <td><span class="qmini"><i style="--w:${(100 * r.words / maxw).toFixed(1)}%"></i></span><small>${r.words} words · ${fmt(r.seconds)}</small></td>
        <td><em class="st ${st}">${{ pass: 'pass', warn: 'revise', fail: 'review', none: 'none' }[st]}</em></td><td class="num">${r.attempts || '·'}</td>
        <td><small>${r.major + r.minor ? `${r.major} major, ${r.minor} minor` : 'none'}${r.missing ? `, ${r.missing} missing` : ''}</small></td></tr>`;
    }).join('');
    const tiles2 = cov ? cov.cells.map((x) => `<i class="ct ${x.status}" title="${esc((x.section || 'Paragraph ' + x.id) + ' · ' + ({ covered: 'covered', planned: 'planned into segment ' + x.segment, thin: 'thinly covered', missing: 'not planned into any segment', heading: 'heading' }[x.status] || x.status) + (x.segment && x.status === 'covered' ? ' · segment ' + x.segment : ''))}"></i>`).join('') : '';
    const lrn = d.learn, ak = d.ask;
    $('#tab-quality').innerHTML = `<div class="pp qd${P.insBusy ? ' busy' : ''}">
      ${d.demo_mode ? '<div class="notice info">Demo mode: this episode was made with offline stand-ins, so these numbers show the machinery works, not real model quality.</div>' : ''}
      <div class="qd-hero">
        <div class="ring" role="img" aria-label="Validation score ${score ?? 'none'} out of 100"><svg viewBox="0 0 120 120"><circle class="rg-bg" cx="60" cy="60" r="52"/><circle class="rg-fg ${score == null ? '' : score >= 80 ? 'good' : score >= 60 ? 'warn' : 'fail'}" cx="60" cy="60" r="52" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${C.toFixed(1)}" data-to="${(C * (1 - (score || 0) / 100)).toFixed(1)}"/></svg><b data-count="${score ?? 0}">${score ?? '–'}</b><small>/100</small></div>
        <div class="qd-hl"><h2>${esc(verdict)}</h2>${v ? `<div class="pchips"><span class="badge ok">${v.counts.pass || 0} passed</span>${v.counts.warn ? `<span class="badge warn">${v.counts.warn} warning${v.counts.warn > 1 ? 's' : ''}</span>` : ''}${v.counts.fail ? `<span class="badge err">${v.counts.fail} failed</span>` : ''}</div>` : ''}<p>${esc(sentence)}</p>
          <div class="pp-acts"><a class="btn primary" href="${u('/insights/report')}" target="_blank" rel="noopener">${I.print} Printable evidence report</a>
            <a class="btn" href="${u('/insights/report?download=1')}">Download</a><button class="btn quiet" data-q="refresh">${P.insBusy ? 'Refreshing…' : 'Refresh'}</button></div></div>
      </div>
      <div class="qd-tiles">${tiles.map(([n, l, k]) => `<div class="tile2 ${k}"><b>${esc(n)}</b><span>${esc(l)}</span></div>`).join('')}</div>
      <div class="qd-grid">
        <section class="qd-card"><h3>Fact-check loop</h3>
          <div class="qbar" role="img" aria-label="Fact-check results">${bar(L.passed_first_try, 'g', 'passed first time')}${bar(L.repaired, 'b', 'repaired')}${bar(L.needs_review, 'r', 'flagged')}</div>
          <ul class="legend"><li><i class="g"></i>${L.passed_first_try} passed first time</li><li><i class="b"></i>${L.repaired} fixed by the repair loop</li><li><i class="r"></i>${L.needs_review} flagged for review</li></ul>
          <p class="mut psm">Every draft is checked by rules and by a second model. Problems go back to the writer (up to ${L.max_revisions} rounds); ${L.repair_rounds} repair round${L.repair_rounds === 1 ? '' : 's'} ${L.repair_rounds === 1 ? 'was' : 'were'} needed here.</p></section>
        <section class="qd-card"><h3>Conversation balance</h3>
          <div class="duo" role="img" aria-label="Speaking time"><i class="a" style="--w:${(sp.A.share * 100).toFixed(1)}%"></i><i class="b" style="--w:${(sp.B.share * 100).toFixed(1)}%"></i></div>
          <div class="duo-l"><span><i class="a"></i>${esc(sp.A.name)} ${pct(sp.A.share)}</span><span><i class="b"></i>${esc(sp.B.name)} ${pct(sp.B.share)}</span></div>
          <div class="metr">${metr.map(([n, l, k]) => `<span class="${k}"><b>${esc(n)}</b>${esc(l)}</span>`).join('')}</div></section>
        <section class="qd-card wide"><h3>Segments</h3><div class="tscroll"><table class="qd-table"><thead><tr><th>#</th><th>Segment</th><th>Length</th><th>Check</th><th>Drafts</th><th>Open issues</th></tr></thead><tbody>${rows}</tbody></table></div></section>
        ${cov ? `<section class="qd-card wide"><h3>Chapter coverage <small>${cov.body_units} paragraphs</small></h3><div class="tiles-h">${tiles2}</div>
          <ul class="legend"><li><i class="ct covered"></i>covered</li><li><i class="ct thin"></i>thin</li><li><i class="ct missing"></i>not planned</li></ul>
          ${cov.comparable ? '' : '<p class="mut psm">The chapter and the podcast use different scripts, so this shows which paragraphs were planned into a segment, not word overlap.</p>'}</section>` : ''}
        <section class="qd-card wide"><h3>All checks</h3>${checks}<p class="pp-acts"><button class="btn small" data-q="text">Full text report</button><button class="btn small" data-q="deep" title="Re-checks every line with the checker model. Uses model credits.">Deep fact-check…</button></p></section>
        <section class="qd-card"><h3>Cost and usage</h3><dl class="kv"><dt>Model calls</dt><dd>${c.llm_calls} · ${c.tokens.toLocaleString()} tokens</dd><dt>Voice requests</dt><dd>${c.tts_calls} · ${c.tts_chars.toLocaleString()} characters${c.tts_cache_hits ? ` · ${c.tts_cache_hits} reused` : ''}</dd>${c.mt_calls ? `<dt>Translations</dt><dd>${c.mt_calls}</dd>` : ''}<dt>Model cost</dt><dd>${c.usd ? '$' + c.usd.toFixed(4) : '$0'}</dd></dl></section>
        <section class="qd-card"><h3>Extras</h3><dl class="kv"><dt>Study pack</dt><dd>${lrn && lrn.kept ? `${lrn.kept.quiz} quiz · ${lrn.kept.glossary} terms · ${lrn.kept.takeaways} takeaways${lrn.dropped ? ` · ${lrn.dropped} removed by the check` : ''}` : 'not created yet'}</dd>
          <dt>Listener questions</dt><dd>${ak.questions ? `${ak.questions} · ${ak.grounded} answered from the chapter · ${ak.declined} declined as off-topic${ak.needs_review ? ` · ${ak.needs_review} flagged` : ''}` : 'none yet'}</dd></dl></section>
      </div></div>`;
    if (reduced()) { $$('.rg-fg[data-to]', box).forEach((el) => { el.setAttribute('stroke-dashoffset', el.dataset.to); }); box.classList.add('go'); return; }
    requestAnimationFrame(() => requestAnimationFrame(() => {
      box.classList.add('go');
      $$('.rg-fg[data-to]', box).forEach((el) => { el.setAttribute('stroke-dashoffset', el.dataset.to); });
      $$('[data-count]', box).forEach((el) => countUp(el, Number(el.dataset.count)));
    }));
  }

  function countUp(el, to) {
    const t0 = performance.now(), dur = 900;
    const step = (t) => { const k = Math.min(1, (t - t0) / dur); el.textContent = Math.round(to * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }

  function onQualityClick(e) {
    const q = e.target.closest('[data-q]');
    if (q) {
      if (q.dataset.q === 'refresh') loadInsights();
      else if (q.dataset.q === 'text') showQuality(S.pid, false);
      else if (q.dataset.q === 'deep') showQuality(S.pid, true);
      return;
    }
    const row = e.target.closest('[data-segjump]');
    if (row) {
      showTab('episode');
      requestAnimationFrame(() => $(`#seg-${row.dataset.segjump}`)?.scrollIntoView({ block: 'start' }));
    }
  }
})();
