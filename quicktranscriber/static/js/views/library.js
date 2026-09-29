// Meetings library: all recordings, search, import.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtDate, fmtDuration, dayLabel, debounce, fmtTime, fmtEta } from '../util.js';
import { avatar, progressBar, errorToast, toast, confirmDialog, menu } from '../ui.js';
import { store, subscribe, activeJobFor } from '../store.js';
import { pickFiles } from './importer.js';

const STAGE_TEXT = { prepare: 'Preparing audio', transcribe: 'Transcribing', speakers: 'Recognising speakers', notes: 'Writing notes' };

export async function render(root) {
  root.innerHTML = `
    <div class="page-head">
      <div><div class="eyebrow">Your library</div><h1>Meetings</h1></div>
      <div class="spacer"></div>
      <div class="search-box">${icon('search', 16)}<input class="input" id="library-search" placeholder="Search every word of every meeting" autocomplete="off"><kbd>Ctrl K</kbd></div>
      <div class="segmented" id="filter"><button class="on" data-value="all">All</button><button data-value="fav">${icon('star', 14)} Starred</button></div>
    </div>
    <div id="recover"></div>
    <div class="hero-actions" id="hero">
      <div class="hero-tile" id="record-tile"><div class="ic">${icon('mic', 24)}</div><div><h3>Record a meeting</h3><p>Microphone, or computer audio for online calls</p></div></div>
      <div class="hero-tile alt" id="import-btn"><div class="ic">${icon('upload', 24)}</div><div><h3>Transcribe a file</h3><p>Or drop audio/video anywhere - MP3, M4A, WAV, MP4…</p></div></div>
    </div>
    <div id="list"></div>`;
  root.querySelector('#record-tile').onclick = () => { location.hash = '#/record'; };
  root.querySelector('#import-btn').onclick = pickFiles;

  let meetings = [];
  let filter = 'all';
  let query = '';
  const list = root.querySelector('#list');

  async function load() {
    try {
      meetings = await api.get(`/api/meetings${filter === 'fav' ? '?favorites=true' : ''}`);
    } catch (e) { errorToast(e); return; }
    if (!query) draw();
  }

  function card(m) {
    const job = activeJobFor(m.id);
    const processing = job || ['queued', 'processing'].includes(m.status);
    let status = '';
    if (processing) {
      const stage = job?.stage ? (STAGE_TEXT[job.stage] || job.stage) : 'Waiting in queue';
      const pct = job?.status === 'running' ? Math.round((job.progress || 0) * 100) : null;
      status = `<div class="status-line">${icon('sparkles', 14)} ${esc(stage)}${pct != null ? ` · ${pct}%` : ''}${job?.eta ? ` · ${esc(fmtEta(job.eta))}` : ''}${progressBar(job?.progress || 0, !job || job.status === 'queued')}</div>`;
    } else if (m.status === 'error') {
      status = `<div class="status-line" style="color:var(--bad)">${icon('alert', 14)} ${esc(m.error || 'Processing failed')}</div>`;
    } else if (m.status === 'cancelled') {
      status = `<div class="status-line" style="color:var(--text-3)">${icon('x', 14)} Cancelled</div>`;
    }
    const spk = (m.speakers || []).slice(0, 5);
    return `<div class="meeting-card" data-id="${esc(m.id)}">
      <div class="thumb">${icon(m.source === 'recording' ? 'mic' : 'wave', 22)}</div>
      <div style="min-width:0">
        <div class="title"><span>${esc(m.title)}</span>${m.notes_status === 'ready' ? `<span class="badge accent">${icon('sparkles', 12)} Notes</span>` : ''}</div>
        <div class="meta"><span>${esc(fmtDate(m.recorded_at || m.created_at))}</span>${m.duration ? `<span>${icon('clock', 13)} ${fmtDuration(m.duration)}</span>` : ''}${spk.length ? `<span>${icon('users', 13)} ${m.speakers.length}</span>` : ''}</div>
        ${m.summary && !processing ? `<div class="summary">${esc(m.summary)}</div>` : ''}
        ${status}
      </div>
      <div class="right">
        <div class="row"><button class="fav ${m.favorite ? 'on' : ''}" data-fav title="Star">${icon('star', 16)}</button>
          <button class="fav" data-more title="More">${icon('more', 16)}</button></div>
        <div class="avatars">${spk.map((s) => avatar(s.name, s.color)).join('')}</div>
      </div></div>`;
  }

  function draw() {
    root.querySelector('#hero').hidden = false;
    if (!meetings.length) {
      list.innerHTML = filter === 'fav'
        ? `<div class="empty"><div class="big-icon">${icon('star', 34)}</div><h2>No starred meetings</h2><p>Star meetings to find them quickly.</p></div>`
        : `<div class="empty"><div class="big-icon">${icon('wave', 34)}</div><h2>No meetings yet</h2><p>Record a meeting or drop an audio file here.<br>Everything is processed on this computer - nothing is uploaded anywhere.</p></div>`;
      return;
    }
    let out = '';
    let lastDay = '';
    for (const m of meetings) {
      const day = dayLabel(m.recorded_at || m.created_at);
      if (day !== lastDay) { out += `<div class="eyebrow day-label">${esc(day)}</div>`; lastDay = day; }
      out += card(m);
    }
    list.innerHTML = `<div class="meeting-list">${out}</div>`;
  }

  async function doSearch() {
    query = root.querySelector('#library-search').value.trim();
    if (!query) { draw(); return; }
    root.querySelector('#hero').hidden = true;
    let hits = [];
    try { hits = await api.get(`/api/search?q=${encodeURIComponent(query)}`); } catch (e) { errorToast(e); }
    const titleHits = meetings.filter((m) => m.title.toLowerCase().includes(query.toLowerCase()) && !hits.find((h) => h.meeting_id === m.id));
    if (!hits.length && !titleHits.length) {
      list.innerHTML = `<div class="empty"><div class="big-icon">${icon('search', 30)}</div><h2>No matches</h2><p>Nothing was said about “${esc(query)}” in your meetings.</p></div>`;
      return;
    }
    const snip = (s) => esc(s).replace(/\[\[/g, '<mark>').replace(/\]\]/g, '</mark>');
    list.innerHTML = titleHits.map(card).join('') + hits.map((h) => `<div class="search-hit">
        <h4 data-open="${esc(h.meeting_id)}">${esc(h.title)} <span class="faint small" style="font-weight:500">· ${esc(fmtDate(h.date))}</span></h4>
        ${h.hits.map((x) => `<div class="hit" data-open="${esc(h.meeting_id)}" data-t="${x.start}"><span class="ts">${fmtTime(x.start)}</span><span>${snip(x.snippet)}</span></div>`).join('')}
      </div>`).join('');
  }

  list.addEventListener('click', async (e) => {
    const open = e.target.closest('[data-open]');
    if (open) {
      location.hash = `#/m/${open.dataset.open}${open.dataset.t ? `?t=${open.dataset.t}&q=${encodeURIComponent(query)}` : ''}`;
      return;
    }
    const cardEl = e.target.closest('.meeting-card');
    if (!cardEl) return;
    const id = cardEl.dataset.id;
    const m = meetings.find((x) => x.id === id);
    if (e.target.closest('[data-fav]')) {
      e.stopPropagation();
      await api.patch(`/api/meetings/${id}`, { favorite: !m.favorite });
      m.favorite = !m.favorite;
      e.target.closest('[data-fav]').classList.toggle('on', m.favorite);
      return;
    }
    if (e.target.closest('[data-more]')) {
      e.stopPropagation();
      menu(e.target.closest('[data-more]'), [
        { label: 'Open', icon: 'chevronRight', onClick: () => { location.hash = `#/m/${id}`; } },
        { label: 'Export as text', icon: 'download', onClick: () => { location.href = `/api/meetings/${id}/export?format=txt`; } },
        { sep: true },
        { label: 'Delete meeting', icon: 'trash', danger: true, onClick: async () => {
          if (await confirmDialog('Delete this meeting?', `“${esc(m.title)}” and its audio will be removed from this computer. Voices learned from it stay in your speaker library.`, { ok: 'Delete', danger: true })) {
            await api.del(`/api/meetings/${id}`);
            toast('Meeting deleted');
            load();
          }
        } },
      ], { align: 'right' });
      return;
    }
    location.hash = `#/m/${id}`;
  });

  root.querySelector('#library-search').addEventListener('input', debounce(doSearch, 250));
  root.querySelector('#filter').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    filter = b.dataset.value;
    root.querySelectorAll('#filter button').forEach((x) => x.classList.toggle('on', x === b));
    load();
  });

  // Offer to rescue recordings interrupted by a crash or closed window
  try {
    const unfinished = await api.get('/api/recordings/unfinished');
    if (unfinished.length) {
      const r = unfinished[0];
      const box = root.querySelector('#recover');
      box.innerHTML = `<div class="callout warn" style="margin-bottom:18px">${icon('alert')}<div style="flex:1"><b>An unfinished recording was found</b>
        Started ${esc(fmtDate(r.started))}${r.bytes ? ` · ${(r.bytes / 1e6).toFixed(1)} MB saved` : ''}. It was saved safely while recording.</div>
        <div class="btn-row"><button class="btn sm primary" data-rescue>Process it</button><button class="btn sm ghost" data-discard>Discard</button></div></div>`;
      box.querySelector('[data-rescue]').onclick = async () => {
        try {
          const res = await api.post(`/api/recordings/${r.id}/finish`, { options: {} });
          location.hash = `#/m/${res.id}`;
        } catch (e) { errorToast(e); }
      };
      box.querySelector('[data-discard]').onclick = async () => {
        if (await confirmDialog('Discard this recording?', 'It will be permanently deleted.', { ok: 'Discard', danger: true })) {
          await api.del(`/api/recordings/${r.id}`);
          box.innerHTML = '';
        }
      };
    }
  } catch { /* ignore */ }

  await load();
  let lastSig = '';
  const unsub = subscribe((what) => {
    if (what === 'jobs' && !query) {
      const sig = JSON.stringify(store.jobs.map((j) => [j.id, j.status, Math.round((j.progress || 0) * 50), j.stage]));
      if (sig !== lastSig) { lastSig = sig; draw(); }
    }
    if (what?.type === 'job-finished') load();
  });
  return () => unsub();
}
