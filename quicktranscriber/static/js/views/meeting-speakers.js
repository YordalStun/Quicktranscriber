// Speakers panel of a meeting: naming, confirming, merging, re-grouping, stats.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtDuration } from '../util.js';
import { avatar, modal, toast, errorToast, menu, confirmDialog } from '../ui.js';

let library = [];

async function loadLibrary() {
  try { library = (await api.get('/api/speakers')).speakers; } catch { library = []; }
  return library;
}

function statusBadge(s) {
  const pct = Math.round((s.confidence || 0) * 100);
  switch (s.status) {
    case 'auto': return `<span class="badge accent" title="Recognised from your speaker library">${icon('sparkles', 11)} Recognised · ${pct}%</span>`;
    case 'confirmed': return `<span class="badge good">${icon('check', 11)} Confirmed</span>`;
    case 'named': return '<span class="badge">Named for this meeting</span>';
    case 'suggested': return `<span class="badge warn">Maybe ${esc(s.suggested_name)}?</span>`;
    default: return '<span class="badge">Unknown voice</span>';
  }
}

function longestSegment(ctx, key) {
  let best = null;
  for (const seg of ctx.m.segments) {
    if (seg.speaker === key && (!best || seg.end - seg.start > best.end - best.start)) best = seg;
  }
  return best;
}

function stats(ctx) {
  const per = {};
  for (const s of ctx.m.speakers) per[s.key] = { turns: 0, words: 0, time: 0, longest: 0 };
  let prev = null;
  let runStart = 0;
  let runEnd = 0;
  for (const seg of ctx.m.segments) {
    const p = per[seg.speaker] || (per[seg.speaker] = { turns: 0, words: 0, time: 0, longest: 0 });
    if (seg.speaker !== prev) {
      if (prev && per[prev]) per[prev].longest = Math.max(per[prev].longest, runEnd - runStart);
      p.turns++;
      runStart = seg.start;
      prev = seg.speaker;
    }
    runEnd = seg.end;
    p.words += seg.text.split(/\s+/).filter(Boolean).length;
    p.time += seg.end - seg.start;
  }
  if (prev && per[prev]) per[prev].longest = Math.max(per[prev].longest, runEnd - runStart);
  return per;
}

function donut(parts) {
  const total = parts.reduce((a, p) => a + p.value, 0) || 1;
  let acc = 0;
  const R = 52, C = 2 * Math.PI * R;
  const arcs = parts.map((p) => {
    const len = (p.value / total) * C;
    const seg = `<circle r="${R}" cx="70" cy="70" fill="none" stroke="${esc(p.color)}" stroke-width="20" stroke-dasharray="${Math.max(0, len - 2)} ${C}" stroke-dashoffset="${-acc}" transform="rotate(-90 70 70)"><title>${esc(p.label)}</title></circle>`;
    acc += len;
    return seg;
  }).join('');
  return `<svg width="140" height="140" viewBox="0 0 140 140">${arcs}<text x="70" y="66" text-anchor="middle" fill="currentColor" font-size="20" font-weight="700">${parts.length}</text><text x="70" y="86" text-anchor="middle" fill="var(--text-3)" font-size="11">${parts.length === 1 ? 'speaker' : 'speakers'}</text></svg>`;
}

export async function renderSpeakers(body, ctx) {
  const m = ctx.m;
  await loadLibrary();
  const totalTalk = m.speakers.reduce((a, s) => a + (s.talk_time || 0), 0) || 1;
  const st = stats(ctx);
  const needsHelp = m.speakers.some((s) => ['auto', 'suggested'].includes(s.status));
  const unknown = m.speakers.filter((s) => s.status === 'unknown').length;
  const n = m.speakers.length;
  const chosen = m.options?.num_speakers || 0;
  const sorted = [...m.speakers].sort((a, b) => (b.talk_time || 0) - (a.talk_time || 0));
  const totalWords = Object.values(st).reduce((a, s) => a + s.words, 0);
  const longestRun = Object.entries(st).sort((a, b) => b[1].longest - a[1].longest)[0];

  body.innerHTML = `
    ${needsHelp ? `<div class="callout accent" style="margin-bottom:14px">${icon('sparkles')}<div style="flex:1"><b>Are these right?</b>Confirming names teaches QuickTranscriber these voices, so future meetings name people automatically.</div>
      <button class="btn primary sm" data-confirm-all>${icon('check', 14)} Looks right</button></div>`
    : unknown && n > 1 ? `<div class="callout" style="margin-bottom:14px">${icon('info')}<div><b>Who's who?</b>Give each voice a name - it will be recognised automatically next time. Click ▶ to hear them.</div></div>` : ''}
    ${m.has_diarization ? `<div class="card" style="margin-bottom:14px;padding:12px 14px"><div class="row wrap">
      <span class="small"><b>${n} ${n === 1 ? 'voice' : 'voices'} found.</b> <span class="muted">Wrong number of people?</span></span><span class="spacer"></span>
      <div class="segmented" data-regroup>${[0, 1, 2, 3, 4, 5, 6, 7, 8].map((k) => `<button data-value="${k}" class="${k === chosen ? 'on' : ''}">${k === 0 ? 'Auto' : k}</button>`).join('')}</div></div></div>` : ''}
    <div data-rows>${sorted.map((s) => row(s)).join('')}</div>
    ${n > 1 ? `<div class="card" style="margin-top:6px"><div class="card-head"><h3>${icon('gauge', 16)} Who talked the most</h3></div>
      <div class="donut-wrap">${donut(sorted.map((s) => ({ value: s.talk_time || 0, color: s.color, label: s.name })))}
        <div class="legend">${sorted.map((s) => `<div><i style="background:${esc(s.color)}"></i><span style="flex:1">${esc(s.name)}</span><b class="tabular">${Math.round((s.talk_time || 0) / totalTalk * 100)}%</b></div>`).join('')}</div></div>
      <div class="stat-grid" style="margin-top:14px">
        <div class="stat"><b>${Object.values(st).reduce((a, s) => a + s.turns, 0)}</b><span>turns taken</span></div>
        <div class="stat"><b>${totalWords.toLocaleString()}</b><span>words spoken</span></div>
        <div class="stat"><b>${Math.round(totalWords / Math.max(1, totalTalk / 60))}</b><span>words per minute</span></div>
        ${longestRun ? `<div class="stat"><b>${fmtDuration(longestRun[1].longest)}</b><span>longest monologue (${esc(ctx.nameOf(longestRun[0]))})</span></div>` : ''}
      </div></div>` : ''}
    ${m.learned_samples ? `<div class="row small faint" style="margin-top:14px">${icon('shield', 14)}<span>${m.learned_samples} voice samples were learned from this meeting.</span><span class="spacer"></span><button class="btn ghost sm" data-forget>Forget them</button></div>` : ''}`;

  function row(s) {
    const pct = Math.round((s.talk_time || 0) / totalTalk * 100);
    const info = st[s.key] || { turns: 0, words: 0 };
    let action = '';
    if (s.status === 'suggested') {
      action = `<div class="suggest">${icon('help', 16)}<span style="flex:1">Is this <b>${esc(s.suggested_name)}</b>? <span class="faint">(${Math.round(s.confidence * 100)}% match)</span></span>
        <button class="btn sm good" data-yes="${s.key}">${icon('check', 13)} Yes</button><button class="btn sm ghost" data-no="${s.key}">No</button></div>`;
    } else if (s.status === 'unknown') {
      action = `<div class="name-input"><input class="input" list="lib-names" placeholder="Who is this?" data-name-for="${s.key}">
        <button class="btn sm primary" data-save-name="${s.key}">Save</button>
        <label class="row small muted" title="Learn this voice so it is recognised in future meetings"><input type="checkbox" data-learn-for="${s.key}" checked> Remember voice</label></div>`;
    }
    return `<div class="spk-row" data-key="${s.key}">
      ${avatar(s.name, s.color, 'lg')}
      <div style="min-width:0"><div class="name">${esc(s.name)} ${statusBadge(s)}</div>
        <div class="small faint">${fmtDuration(s.talk_time || 0)} · ${pct}% of talking · ${info.turns} turns</div>
        <div class="talkbar"><i style="width:${pct}%;background:${esc(s.color)}"></i></div>
        ${action}</div>
      <div class="btn-row" style="flex-direction:column;align-items:flex-end">
        <button class="mini-play" data-hear="${s.key}" title="Hear this voice">${icon('play', 14)}</button>
        <button class="btn ghost sm icon-only" data-more="${s.key}" title="More">${icon('more', 15)}</button></div></div>`;
  }

  if (!document.getElementById('lib-names')) {
    const dl = document.createElement('datalist');
    dl.id = 'lib-names';
    document.body.appendChild(dl);
  }
  document.getElementById('lib-names').innerHTML = library.map((p) => `<option value="${esc(p.name)}">`).join('');

  const apply = (meeting, learned) => {
    ctx.setMeeting(meeting);
    if (learned) toast(`Learned ${learned} voice sample${learned === 1 ? '' : 's'} - they'll be recognised next time`, 'good');
  };

  body.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    try {
      if (t.dataset.confirmAll !== undefined) {
        const r = await api.post(`/api/meetings/${ctx.id}/speakers/confirm-all`);
        apply(r, r.learned);
      } else if (t.dataset.yes) {
        const r = await api.patch(`/api/meetings/${ctx.id}/speakers/${t.dataset.yes}`, { action: 'confirm' });
        apply(r, r.learned);
      } else if (t.dataset.no) {
        const r = await api.patch(`/api/meetings/${ctx.id}/speakers/${t.dataset.no}`, { action: 'reject' });
        apply(r);
      } else if (t.dataset.saveName) {
        const key = t.dataset.saveName;
        const name = body.querySelector(`[data-name-for="${key}"]`).value.trim();
        const learn = body.querySelector(`[data-learn-for="${key}"]`).checked;
        if (!name) { toast('Type a name first', 'warn'); return; }
        const r = await api.patch(`/api/meetings/${ctx.id}/speakers/${key}`, { name, learn });
        apply(r, r.learned);
      } else if (t.dataset.hear) {
        const seg = longestSegment(ctx, t.dataset.hear);
        if (seg) ctx.seek(seg.start);
      } else if (t.dataset.more) {
        moreMenu(t, ctx, t.dataset.more, apply);
      } else if (t.dataset.forget !== undefined) {
        if (await confirmDialog('Forget voices from this meeting?', 'Samples learned from this recording are removed from your speaker library. Names in this meeting stay.', { ok: 'Forget', danger: true })) {
          const r = await api.del(`/api/meetings/${ctx.id}/learning`);
          toast(`Removed ${r.removed} samples`);
          await ctx.reload();
        }
      }
    } catch (err) { errorToast(err); }
  };
  body.querySelectorAll('[data-name-for]').forEach((inp) => inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') body.querySelector(`[data-save-name="${inp.dataset.nameFor}"]`).click();
  }));
  body.querySelector('[data-regroup]')?.addEventListener('click', async (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const edited = ctx.m.segments.some((s) => s.edited);
    if (edited && !(await confirmDialog('Re-group the speakers?', 'Manual text edits and line moves in this transcript will be reset. Confirmed names are kept where the voices match.', { ok: 'Re-group' }))) return;
    b.parentElement.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    try {
      const r = await api.post(`/api/meetings/${ctx.id}/speakers/recluster`, { num_speakers: +b.dataset.value });
      apply(r);
      toast(+b.dataset.value ? `Re-grouped into ${r.speakers.length} speakers` : 'Speakers detected automatically', 'good');
    } catch (err) { errorToast(err); }
  });
}

function moreMenu(anchor, ctx, key, apply) {
  const s = ctx.m.speakers.find((x) => x.key === key);
  const others = ctx.m.speakers.filter((x) => x.key !== key);
  menu(anchor, [
    { label: s.status === 'unknown' ? 'Name this speaker…' : 'Rename / change person…', icon: 'edit', onClick: () => renameSpeakerDialog(ctx, key) },
    others.length ? { header: 'Same person as…' } : null,
    ...others.map((o) => ({ label: o.name, color: o.color, onClick: async () => {
      try {
        const r = await api.post(`/api/meetings/${ctx.id}/speakers/merge`, { source: key, target: o.key });
        apply(r);
        toast(`Merged into ${o.name}`, 'good');
      } catch (e) { errorToast(e); }
    } })),
    s.speaker_id ? { sep: true } : null,
    s.speaker_id ? { label: `Not ${s.name}`, icon: 'x', onClick: async () => {
      try { apply(await api.patch(`/api/meetings/${ctx.id}/speakers/${key}`, { action: 'unlink' })); } catch (e) { errorToast(e); }
    } } : null,
  ], { align: 'right' });
}

export async function renameSpeakerDialog(ctx, key) {
  const s = ctx.m.speakers.find((x) => x.key === key);
  await loadLibrary();
  const body = `<div class="field"><label>Name</label><input class="input" list="lib-names-dlg" name="name" value="${s.status === 'unknown' ? '' : esc(s.name)}" placeholder="e.g. Mark Jones">
      <datalist id="lib-names-dlg">${library.map((p) => `<option value="${esc(p.name)}">`).join('')}</datalist>
      <div class="hint">Pick someone from your speaker library or type a new name.</div></div>
    <label class="row"><input type="checkbox" name="learn" checked> Remember this voice for future meetings</label>`;
  await modal({
    title: `Who is ${s.name}?`, icon: 'user', body,
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', onClick: async (m) => {
      const name = m.querySelector('[name=name]').value.trim();
      if (!name) return false;
      try {
        const r = await api.patch(`/api/meetings/${ctx.id}/speakers/${key}`, { name, learn: m.querySelector('[name=learn]').checked });
        ctx.setMeeting(r);
        toast(r.learned ? `Saved - learned ${r.learned} voice samples of ${name}` : 'Saved', 'good');
      } catch (e) { errorToast(e); return false; }
    } }],
  });
}
