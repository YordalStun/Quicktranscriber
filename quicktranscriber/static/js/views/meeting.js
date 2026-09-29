// Meeting page: notes, transcript, speakers, ask - with a synced audio player.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtDate, fmtDuration, fmtTime, fmtEta, debounce, $ } from '../util.js';
import { avatar, menu, toast, errorToast, confirmDialog, progressBar, promptDialog, modal } from '../ui.js';
import { store, subscribe, activeJobFor, kickPoll, languageName } from '../store.js';
import { Player } from '../player.js';
import { renderNotes } from './meeting-notes.js';
import { renderSpeakers, renameSpeakerDialog } from './meeting-speakers.js';
import { renderAsk } from './meeting-ask.js';
import { openExport } from './export.js';

const STAGE_ICONS = { prepare: 'wave', transcribe: 'fileText', speakers: 'users', notes: 'sparkles' };

export async function render(root, { args, params }) {
  const id = args[0];
  root.className = 'view flush';
  root.innerHTML = `<div class="empty">${icon('wave', 30)}<p>Loading…</p></div>`;
  const ctx = {
    id,
    m: null,
    player: null,
    leftTab: 'notes',
    follow: true,
    query: params.q || '',
    lastUserScroll: 0,
    activeSeg: -1,
    activeWord: null,
  };
  try {
    ctx.m = await api.get(`/api/meetings/${id}`);
  } catch (e) {
    root.innerHTML = `<div class="empty"><div class="big-icon">${icon('alert', 34)}</div><h2>Meeting not found</h2><p><a href="#/">Back to meetings</a></p></div>`;
    return;
  }
  try { ctx.leftTab = localStorage.getItem('qt-left-tab') || 'notes'; } catch { /* ignore */ }

  ctx.colorOf = (key) => ctx.m.speakers.find((s) => s.key === key)?.color || '#8b5cf6';
  ctx.nameOf = (key) => ctx.m.speakers.find((s) => s.key === key)?.name || key || 'Speaker';
  ctx.seek = (t, play = true) => ctx.player?.seek(t, play);
  ctx.reload = async (full = false) => {
    ctx.m = await api.get(`/api/meetings/${id}`);
    if (full) layout(); else refresh();
  };
  ctx.setMeeting = (m) => { ctx.m = m; refresh(); };

  function layout() {
    const job = activeJobFor(id);
    const hasTranscript = ctx.m.segments.length > 0;
    root.innerHTML = `
      <div class="meeting-head" id="mhead"></div>
      ${!hasTranscript && (job || ['queued', 'processing'].includes(ctx.m.status)) ? `<div class="pane-body" id="live" style="padding:28px"></div>` : `
      <div class="meeting-body" id="mbody">
        <section class="pane" id="left">
          <div class="pane-tabs" id="ltabs"></div>
          <div class="pane-body" id="lbody"></div>
        </section>
        <section class="pane" id="right" style="position:relative">
          <div class="pane-tools" id="ttools"></div>
          <div class="pane-body" id="tbody"><div class="transcript" id="transcript"></div></div>
          <button class="btn sm follow-toggle" id="follow" hidden>${icon('target', 14)} Follow playback</button>
        </section>
      </div>`}
      ${ctx.m.has_audio ? '<div class="player" id="player"></div>' : ''}`;
    renderHead();
    if (ctx.player) { ctx.player.destroy(); ctx.player = null; }
    if (ctx.m.has_audio) {
      ctx.player = new Player($('#player', root), {
        src: `/api/meetings/${id}/audio`,
        duration: ctx.m.duration,
        peaksUrl: `/api/meetings/${id}/peaks`,
        onTime: onTime,
      });
      paintPlayer();
    }
    if ($('#live', root)) renderLive();
    else {
      renderLeftTabs();
      renderLeft();
      renderTranscriptTools();
      renderTranscript();
      bindTranscript();
    }
    if (params.t) {
      const t = +params.t;
      setTimeout(() => { ctx.seek(t, false); scrollToTime(t, true); }, 150);
      params.t = null;
    }
  }

  function refresh() {
    const hasBody = !!$('#mbody', root);
    const hasTranscript = ctx.m.segments.length > 0;
    if (!hasBody && hasTranscript) { layout(); return; }
    renderHead();
    if (hasBody) {
      renderLeftTabs();
      renderLeft();
      renderTranscript();
    } else renderLive();
    paintPlayer();
  }

  function paintPlayer() {
    if (!ctx.player) return;
    ctx.player.setSpeakerColors(ctx.m.segments, ctx.colorOf);
    const markers = (ctx.m.notes?.chapters || []).map((c) => ({ t: c.t, color: '#22d3ee', label: c.title }));
    for (const b of ctx.m.options?.bookmarks || []) markers.push({ t: b, color: '#fbbf24', label: 'Bookmark' });
    ctx.player.setMarkers(markers);
  }

  // ---------------- header ----------------
  function renderHead() {
    const m = ctx.m;
    const head = $('#mhead', root);
    const langs = m.language ? languageName(m.language) : '';
    head.innerHTML = `
      <a class="btn ghost icon-only" href="#/" title="Back">${icon('arrowLeft')}</a>
      <div class="titles">
        <h1 class="meeting-title" contenteditable="true" spellcheck="false" id="mtitle">${esc(m.title)}</h1>
        <div class="meeting-meta">
          <span>${icon('calendar', 14)} ${esc(fmtDate(m.recorded_at || m.created_at, { long: true }))}</span>
          ${m.duration ? `<span>${icon('clock', 14)} ${fmtDuration(m.duration)}</span>` : ''}
          ${m.speakers.length ? `<span>${icon('users', 14)} ${m.speakers.length} ${m.speakers.length === 1 ? 'speaker' : 'speakers'}</span>` : ''}
          ${langs ? `<span>${icon('globe', 14)} ${esc(langs)}</span>` : ''}
          ${m.stats?.whisper_model ? `<span title="Transcribed with">${icon('cpu', 14)} ${esc(m.stats.whisper_model)}${m.stats.device === 'cuda' ? ' · GPU' : ''}</span>` : ''}
          ${m.status === 'error' ? `<span class="badge bad">${icon('alert', 12)} ${esc(m.error || 'Failed')}</span>` : ''}
        </div>
      </div>
      <div class="btn-row">
        <button class="fav ${m.favorite ? 'on' : ''}" id="mfav" title="Star">${icon('star', 18)}</button>
        <button class="btn" id="mexport" ${m.segments.length ? '' : 'disabled'}>${icon('download', 16)} Export</button>
        <button class="btn ghost icon-only" id="mmore" title="More">${icon('more')}</button>
      </div>`;
    const t = $('#mtitle', head);
    t.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); t.blur(); } });
    t.addEventListener('blur', async () => {
      const v = t.textContent.trim();
      if (v && v !== ctx.m.title) {
        await api.patch(`/api/meetings/${id}`, { title: v });
        ctx.m.title = v;
        toast('Title saved', 'good', 1500);
      } else t.textContent = ctx.m.title;
    });
    $('#mfav', head).onclick = async (e) => {
      ctx.m.favorite = !ctx.m.favorite;
      await api.patch(`/api/meetings/${id}`, { favorite: ctx.m.favorite });
      e.currentTarget.classList.toggle('on', ctx.m.favorite);
    };
    $('#mexport', head).onclick = () => openExport(ctx.m);
    $('#mmore', head).onclick = (e) => moreMenu(e.currentTarget);
  }

  function moreMenu(anchor) {
    const job = activeJobFor(id);
    menu(anchor, [
      { header: 'Redo' },
      { label: 'Transcribe again…', icon: 'fileText', disabled: !!job, onClick: retranscribe },
      { label: 'Detect speakers again', icon: 'users', disabled: !!job || !ctx.m.segments.length, onClick: () => reprocess(['speakers'], 'Detecting speakers again') },
      { label: 'Write notes again…', icon: 'sparkles', disabled: !!job || !ctx.m.segments.length, onClick: () => import('./meeting-notes.js').then((n) => n.regenerateDialog(ctx)) },
      { sep: true },
      { label: 'Copy transcript', icon: 'copy', disabled: !ctx.m.segments.length, onClick: copyTranscript },
      { label: 'Open meeting folder', icon: 'folder', onClick: () => api.post('/api/open-folder', { meeting: id }).catch(errorToast) },
      { label: 'Keyboard shortcuts', icon: 'hash', onClick: shortcutsHelp },
      { sep: true },
      { label: 'Delete meeting', icon: 'trash', danger: true, onClick: deleteMeeting },
    ], { align: 'right' });
  }

  async function reprocess(stages, message, options) {
    try {
      await api.post(`/api/meetings/${id}/reprocess`, { stages, options });
      toast(message, 'good');
      kickPoll();
      setTimeout(() => ctx.reload(true), 400);
    } catch (e) { errorToast(e); }
  }

  async function retranscribe() {
    const models = await api.get('/api/models');
    const { optionsForm, bindOptionsForm, readOptions } = await import('./importer.js');
    await modal({
      title: 'Transcribe again', subtitle: 'Use a different model or language. Speaker names you set are kept where possible.', icon: 'refresh', wide: true,
      body: optionsForm(models, { duration: ctx.m.duration, defaults: ctx.m.options }),
      onOpen: (mm) => bindOptionsForm(mm, models),
      actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Start', kind: 'primary', icon: 'sparkles', onClick: async (mm) => {
        const opts = readOptions(mm);
        await reprocess(['transcribe', 'speakers'], 'Transcribing again', opts);
      } }],
    });
  }

  async function deleteMeeting() {
    const learned = ctx.m.learned_samples;
    const body = learned ? `<label class="row" style="margin-top:6px"><input type="checkbox" id="forget"> Also forget the ${learned} voice samples learned from this meeting</label>` : '';
    const ok = await modal({
      title: 'Delete this meeting?', subtitle: 'The recording, transcript and notes will be removed from this computer.', body,
      actions: [{ label: 'Cancel', kind: 'ghost', value: false }, { label: 'Delete', kind: 'danger', onClick: (mm) => ({ forget: !!mm.querySelector('#forget')?.checked }) }],
    });
    if (!ok) return;
    await api.del(`/api/meetings/${id}?forget_voices=${ok.forget ? 'true' : 'false'}`);
    toast('Meeting deleted');
    location.hash = '#/';
  }

  function copyTranscript() {
    const text = ctx.m.segments.map((s) => `[${fmtTime(s.start)}] ${ctx.nameOf(s.speaker)}: ${s.text}`).join('\n\n');
    navigator.clipboard.writeText(text).then(() => toast('Transcript copied', 'good'));
  }

  function shortcutsHelp() {
    modal({
      title: 'Keyboard shortcuts', icon: 'hash',
      body: `<div class="kbd-help">
        <kbd>Space</kbd><span>Play / pause</span><kbd>J</kbd> <span>Back 15 seconds</span><kbd>L</kbd><span>Forward 15 seconds</span>
        <kbd>←</kbd> <kbd>→</kbd><span>Back / forward 5 seconds</span><kbd>[</kbd> <kbd>]</kbd><span>Slower / faster</span>
        <kbd>Ctrl F</kbd><span>Search the transcript</span><kbd>Double-click</kbd><span>Edit a line of the transcript</span></div>`,
      actions: [{ label: 'Close', kind: 'primary' }],
    });
  }

  // ---------------- live processing ----------------
  function renderLive() {
    const box = $('#live', root);
    if (!box) return;
    const job = activeJobFor(id);
    const m = ctx.m;
    if (!job) {
      box.innerHTML = m.status === 'error'
        ? `<div class="live-card"><div class="callout bad">${icon('alert')}<div><b>Processing failed</b>${esc(m.error || '')}</div></div>
            <div class="btn-row" style="margin-top:14px"><button class="btn primary" id="retry">${icon('refresh', 16)} Try again</button></div></div>`
        : `<div class="live-card"><div class="row">${icon('clock')}<b>Waiting to start…</b></div></div>`;
      $('#retry', box)?.addEventListener('click', () => reprocess(['prepare', 'transcribe', 'speakers', 'notes'], 'Trying again'));
      return;
    }
    const stages = job.stages?.length ? job.stages : [{ key: 'prepare', label: 'Preparing audio', status: 'pending' }];
    const partial = (job.partial || []).slice(-5);
    box.innerHTML = `<div class="live-card">
      <div class="row"><div><div class="eyebrow">${job.status === 'queued' ? 'In the queue' : 'Processing on this computer'}</div>
        <h2 style="margin:2px 0 0;font-family:var(--font-display)">${job.status === 'queued' ? 'Waiting for the current job to finish' : esc(job.message || 'Working…')}</h2></div>
        <div class="spacer"></div><button class="btn danger sm" id="cancel">${icon('x', 14)} Cancel</button></div>
      <div class="stages">${stages.map((s) => {
        const running = s.status === 'running' || (job.stage === s.key && job.status === 'running');
        const done = s.status === 'done';
        const pct = running ? job.progress || 0 : 0;
        return `<div class="stage ${done ? 'done' : ''} ${running ? 'running' : ''}">
          <div class="dot">${icon(done ? 'check' : STAGE_ICONS[s.key] || 'sparkles', 14)}</div>
          <div><div class="name">${esc(s.label)}</div>${running ? progressBar(pct) : ''}</div>
          <div class="meta">${running ? `${Math.round(pct * 100)}%${job.eta ? ' · ' + fmtEta(job.eta) : ''}` : done ? 'Done' : ''}</div></div>`;
      }).join('')}</div>
      ${job.stage === 'transcribe' ? `<div class="live-text">${partial.length ? partial.map((p) => `<p>${esc(p)}</p>`).join('') : '<p class="faint">Listening…</p>'}</div>` : ''}
      <div class="faint small" style="margin-top:12px">You can close this page - processing continues in the background. Long meetings can take a while on slower computers.</div>
    </div>`;
    $('#cancel', box).onclick = async () => {
      if (await confirmDialog('Cancel processing?', 'Anything already finished is kept.', { ok: 'Cancel processing', danger: true, cancel: 'Keep going' })) {
        await api.post(`/api/meetings/${id}/cancel`);
        kickPoll();
      }
    };
  }

  // ---------------- left pane ----------------
  function renderLeftTabs() {
    const tabs = $('#ltabs', root);
    if (!tabs) return;
    const pending = ctx.m.speakers.filter((s) => s.status === 'suggested' || s.status === 'auto').length;
    const unknown = ctx.m.speakers.filter((s) => s.status === 'unknown').length;
    tabs.innerHTML = [
      ['notes', 'sparkles', 'Notes', ''],
      ['speakers', 'users', 'Speakers', pending ? `<span class="badge accent">${pending}</span>` : unknown && ctx.m.speakers.length > 1 ? `<span class="badge">${unknown}</span>` : ''],
      ['ask', 'message', 'Ask', ''],
    ].map(([k, ic, label, extra]) => `<button class="pane-tab ${ctx.leftTab === k ? 'on' : ''}" data-tab="${k}">${icon(ic, 15)} ${label} ${extra}</button>`).join('');
    tabs.onclick = (e) => {
      const b = e.target.closest('[data-tab]');
      if (!b) return;
      ctx.leftTab = b.dataset.tab;
      try { localStorage.setItem('qt-left-tab', ctx.leftTab); } catch { /* ignore */ }
      renderLeftTabs();
      renderLeft();
    };
  }

  function renderLeft() {
    const body = $('#lbody', root);
    if (!body) return;
    const job = activeJobFor(id);
    if (ctx.leftTab === 'speakers') renderSpeakers(body, ctx);
    else if (ctx.leftTab === 'ask') renderAsk(body, ctx);
    else renderNotes(body, ctx, job);
  }
  ctx.renderLeft = () => { renderLeftTabs(); renderLeft(); };

  // ---------------- transcript ----------------
  function renderTranscriptTools() {
    const tools = $('#ttools', root);
    tools.innerHTML = `<div class="search-box" style="min-width:0;flex:1;max-width:340px">${icon('search', 15)}<input class="input" id="tsearch" placeholder="Search transcript" value="${esc(ctx.query)}" style="padding-top:6px;padding-bottom:6px"></div>
      <span class="faint small" id="tcount"></span>
      <button class="btn ghost sm icon-only" id="tprev" title="Previous match">${icon('chevronDown', 15, '')}</button>
      <div class="spacer"></div>
      <span class="faint small">${ctx.m.segments.length} lines</span>`;
    $('#tprev', tools).style.transform = 'rotate(180deg)';
    const input = $('#tsearch', tools);
    input.addEventListener('input', debounce(() => { ctx.query = input.value.trim(); renderTranscript(); jumpMatch(0); }, 200));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); jumpMatch(e.shiftKey ? -1 : 1); } });
    $('#tprev', tools).onclick = () => jumpMatch(-1);
  }

  let matchIndex = -1;
  function jumpMatch(dir) {
    const marks = [...root.querySelectorAll('#transcript mark')];
    const count = $('#tcount', root);
    if (!ctx.query) { count.textContent = ''; return; }
    count.textContent = marks.length ? `${Math.max(1, matchIndex + 1)} of ${marks.length}` : 'No matches';
    if (!marks.length) return;
    matchIndex = dir === 0 ? 0 : (matchIndex + dir + marks.length) % marks.length;
    count.textContent = `${matchIndex + 1} of ${marks.length}`;
    marks[matchIndex].scrollIntoView({ block: 'center', behavior: 'smooth' });
    ctx.lastUserScroll = Date.now();
  }

  function highlight(text) {
    if (!ctx.query) return esc(text);
    const q = ctx.query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return esc(text).replace(new RegExp(`(${esc(q)})`, 'gi'), '<mark>$1</mark>');
  }

  function renderTranscript() {
    const box = $('#transcript', root);
    if (!box) return;
    const m = ctx.m;
    if (!m.segments.length) {
      box.innerHTML = `<div class="empty"><div class="big-icon">${icon('fileText', 30)}</div><h2>No speech found</h2><p>This recording seems to be silent or music only.</p></div>`;
      return;
    }
    const bookmarks = [...(m.options?.bookmarks || [])].sort((a, b) => a - b);
    let bi = 0;
    const parts = [];
    let prevSpeaker = null;
    m.segments.forEach((s, i) => {
      while (bi < bookmarks.length && bookmarks[bi] <= s.start) {
        parts.push(`<div class="bookmark-line" data-seek="${bookmarks[bi]}">${icon('bookmark', 13)} Bookmark · ${fmtTime(bookmarks[bi])}</div>`);
        bi++;
      }
      const name = ctx.nameOf(s.speaker);
      const color = ctx.colorOf(s.speaker);
      let text;
      if (s.edited || !s.w?.length || ctx.query) text = highlight(s.text);
      else text = s.w.map((w, j) => `<span class="w" data-i="${j}">${esc(w[2])}</span>`).join('');
      const cont = prevSpeaker === s.speaker;
      prevSpeaker = s.speaker;
      parts.push(`<div class="seg" data-i="${i}" style="--seg-color:${esc(color)}">
        <div>${cont ? '' : avatar(name, color)}</div>
        <div>
          <div class="seg-head">${cont ? '' : `<span class="seg-name" style="color:${esc(color)}" data-spk="${esc(s.speaker)}">${esc(name)}</span>`}<span class="ts link" data-seek="${s.start}">${fmtTime(s.start)}</span>${s.edited ? '<span class="edited">edited</span>' : ''}</div>
          <div class="seg-text">${text}</div>
        </div>
        <div class="seg-tools">
          <button class="btn ghost sm icon-only" data-act="play" title="Play from here">${icon('play', 13)}</button>
          <button class="btn ghost sm icon-only" data-act="edit" title="Edit text">${icon('edit', 13)}</button>
          <button class="btn ghost sm icon-only" data-act="who" title="Change speaker">${icon('user', 13)}</button>
        </div></div>`);
    });
    box.innerHTML = parts.join('');
    ctx.activeSeg = -1;
    ctx.activeWord = null;
    if (ctx.player) onTime(ctx.player.time);
    if (ctx.query) jumpMatch(0); else $('#tcount', root) && ($('#tcount', root).textContent = '');
  }

  function bindTranscript() {
    const box = $('#transcript', root);
    const scroller = $('#tbody', root);
    const followBtn = $('#follow', root);
    scroller.addEventListener('wheel', () => { ctx.lastUserScroll = Date.now(); if (ctx.player?.playing) followBtn.hidden = false; }, { passive: true });
    followBtn.onclick = () => { ctx.lastUserScroll = 0; followBtn.hidden = true; scrollToTime(ctx.player.time, true); };
    box.addEventListener('click', (e) => {
      const seekEl = e.target.closest('[data-seek]');
      if (seekEl) { ctx.seek(+seekEl.dataset.seek); return; }
      const segEl = e.target.closest('.seg');
      if (!segEl) return;
      const seg = ctx.m.segments[+segEl.dataset.i];
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act === 'play') { ctx.seek(seg.start); return; }
      if (act === 'edit') { editSegment(segEl, seg); return; }
      if (act === 'who' || e.target.closest('[data-spk]') || e.target.closest('.avatar')) { speakerMenu(e.target.closest('[data-act]') || e.target, seg); return; }
      const w = e.target.closest('.w');
      if (w && !segEl.querySelector('[contenteditable=true]')) {
        const word = seg.w[+w.dataset.i];
        if (word) ctx.seek(word[0]);
      }
    });
    box.addEventListener('dblclick', (e) => {
      const segEl = e.target.closest('.seg');
      if (segEl && e.target.closest('.seg-text')) editSegment(segEl, ctx.m.segments[+segEl.dataset.i]);
    });
  }

  function editSegment(segEl, seg) {
    const t = segEl.querySelector('.seg-text');
    if (t.isContentEditable) return;
    const original = seg.text;
    t.textContent = seg.text;
    t.contentEditable = 'true';
    t.focus();
    const range = document.createRange();
    range.selectNodeContents(t);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    const finish = async (save) => {
      t.removeEventListener('blur', onBlur);
      t.contentEditable = 'false';
      const v = t.textContent.trim();
      if (save && v && v !== original) {
        try {
          await api.patch(`/api/meetings/${id}/segments/${seg.id}`, { text: v });
          seg.text = v;
          seg.edited = 1;
          toast('Saved', 'good', 1200);
        } catch (e) { errorToast(e); }
      }
      renderTranscript();
    };
    const onBlur = () => finish(true);
    t.addEventListener('blur', onBlur);
    t.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); t.blur(); }
      if (e.key === 'Escape') { t.textContent = original; finish(false); }
    });
  }

  function speakerMenu(anchor, seg) {
    const others = ctx.m.speakers.filter((s) => s.key !== seg.speaker);
    menu(anchor, [
      { header: ctx.nameOf(seg.speaker) },
      { label: 'Name this speaker…', icon: 'edit', onClick: () => renameSpeakerDialog(ctx, seg.speaker) },
      { label: 'Play this line', icon: 'play', onClick: () => ctx.seek(seg.start) },
      { sep: true },
      { header: 'This line was said by…' },
      ...others.map((s) => ({ label: s.name, color: s.color, onClick: () => moveSegment(seg, s.key) })),
      { label: 'Someone else (new speaker)', icon: 'plus', onClick: () => moveSegment(seg, 'new') },
    ]);
  }

  async function moveSegment(seg, key) {
    const target = ctx.m.speakers.find((s) => s.key === key);
    const learn = !!target?.speaker_id && seg.end - seg.start >= 2.5;
    try {
      await api.patch(`/api/meetings/${id}/segments/${seg.id}`, { speaker: key, learn });
      await ctx.reload();
      toast(learn ? `Moved - and ${target.name}'s voiceprint learned from it` : 'Moved', 'good', 2000);
    } catch (e) { errorToast(e); }
  }

  function scrollToTime(t, force = false) {
    const i = segIndexAt(t);
    if (i < 0) return;
    const el = root.querySelector(`.seg[data-i="${i}"]`);
    if (!el) return;
    if (force || (ctx.follow && Date.now() - ctx.lastUserScroll > 5000)) {
      el.scrollIntoView({ block: 'center', behavior: force ? 'auto' : 'smooth' });
    }
  }

  function segIndexAt(t) {
    const segs = ctx.m.segments;
    let lo = 0, hi = segs.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (segs[mid].start <= t + 0.05) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  function onTime(t) {
    const box = $('#transcript', root);
    if (!box) return;
    const i = segIndexAt(t);
    if (i !== ctx.activeSeg) {
      box.querySelector('.seg.active')?.classList.remove('active');
      box.querySelectorAll('.w.spoken').forEach((w) => w.classList.remove('spoken'));
      ctx.activeSeg = i;
      const el = box.querySelector(`.seg[data-i="${i}"]`);
      if (el && t <= ctx.m.segments[i].end + 1.5) {
        el.classList.add('active');
        if (ctx.player?.playing) scrollToTime(t);
      }
      ctx.activeWord = null;
    }
    // word-by-word highlight
    const seg = ctx.m.segments[i];
    const el = box.querySelector(`.seg[data-i="${i}"]`);
    if (!seg || !el || !seg.w?.length || seg.edited || ctx.query) return;
    let wi = -1;
    for (let k = 0; k < seg.w.length; k++) { if (seg.w[k][0] <= t) wi = k; else break; }
    if (wi === ctx.activeWord) return;
    const words = el.querySelectorAll('.w');
    words.forEach((w, k) => { w.classList.toggle('spoken', k < wi); w.classList.toggle('on', k === wi && t <= seg.w[wi][1] + 0.3); });
    ctx.activeWord = wi;
    // chapters highlight in notes
    root.querySelectorAll('.chapter').forEach((c) => c.classList.remove('now'));
    const chapters = [...root.querySelectorAll('.chapter')];
    const cur = chapters.filter((c) => +c.dataset.seek <= t).pop();
    cur?.classList.add('now');
  }

  // ---------------- keyboard ----------------
  const onKey = (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f' && $('#tsearch', root)) {
      e.preventDefault();
      $('#tsearch', root).focus();
      return;
    }
    if (typing || !ctx.player || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    if (k === ' ') { e.preventDefault(); ctx.player.toggle(); }
    else if (k === 'ArrowLeft') { e.preventDefault(); ctx.player.skip(-5); }
    else if (k === 'ArrowRight') { e.preventDefault(); ctx.player.skip(5); }
    else if (k === 'j') ctx.player.skip(-15);
    else if (k === 'l') ctx.player.skip(15);
    else if (k === '[' || k === ']') {
      const speeds = [0.75, 1, 1.25, 1.5, 1.75, 2];
      const i = speeds.indexOf(ctx.player.audio.playbackRate);
      ctx.player.setSpeed(speeds[Math.max(0, Math.min(speeds.length - 1, i + (k === ']' ? 1 : -1)))]);
    }
  };
  window.addEventListener('keydown', onKey);

  // links to timestamps anywhere in the view (notes, chat)
  root.addEventListener('click', (e) => {
    const s = e.target.closest('[data-seek]');
    if (s && !e.target.closest('#transcript')) {
      e.preventDefault();
      ctx.seek(+s.dataset.seek);
      scrollToTime(+s.dataset.seek, true);
    }
  });

  layout();

  // live updates while processing
  let lastStage = null;
  const unsub = subscribe(async (what) => {
    if (what === 'jobs') {
      const job = activeJobFor(id);
      if ($('#live', root)) renderLive();
      else if (job && ctx.leftTab === 'notes') {
        const banner = root.querySelector('[data-job-banner]');
        if (banner) {
          banner.querySelector('[data-msg]').textContent = job.message || 'Working…';
          banner.querySelector('.progress i').style.width = `${Math.round((job.progress || 0) * 100)}%`;
          banner.querySelector('[data-eta]').textContent = job.eta ? fmtEta(job.eta) : '';
        } else renderLeft();
      }
      // transcript becomes available after the transcription stage
      if (job && job.stage !== lastStage) {
        const prev = lastStage;
        lastStage = job.stage;
        if (prev && ['speakers', 'notes'].includes(job.stage)) {
          ctx.m = await api.get(`/api/meetings/${id}`);
          if ($('#live', root) && ctx.m.segments.length) layout(); else refresh();
        }
      }
    }
    if (what?.type === 'job-finished' && what.job.meeting_id === id) {
      lastStage = null;
      ctx.m = await api.get(`/api/meetings/${id}`);
      layout();
    }
  });

  return () => {
    unsub();
    window.removeEventListener('keydown', onKey);
    ctx.player?.destroy();
  };
}
