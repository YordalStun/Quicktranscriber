// Notes panel: summary, key points, decisions, action items, chapters.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtTime, fmtEta, fmtDuration } from '../util.js';
import { modal, toast, errorToast, progressBar } from '../ui.js';
import { store, kickPoll } from '../store.js';

function ownerChip(owner, ctx) {
  if (!owner) return '';
  const spk = ctx.m.speakers.find((s) => s.name.toLowerCase() === owner.toLowerCase());
  const color = spk?.color || 'var(--text-3)';
  const init = owner.replace(/^speaker\s+/i, '').split(/\s+/).map((p) => p[0]).slice(0, 2).join('').toUpperCase();
  return `<span class="owner"><i style="background:${esc(color)}">${esc(init)}</i>${esc(owner)}</span>`;
}

const tsLink = (t) => (t == null ? '' : `<a class="ts link" data-seek="${t}">${fmtTime(t)}</a>`);

export function renderNotes(body, ctx, job) {
  const m = ctx.m;
  const n = m.notes;
  const meta = m.notes_meta || {};
  const notesRunning = job && (job.stage === 'notes' || (job.stages || []).some((s) => s.key === 'notes' && s.status !== 'done'));
  let banner = '';
  if (job) {
    banner = `<div class="callout accent" data-job-banner style="margin-bottom:14px">${icon('sparkles')}<div style="flex:1">
      <b data-msg>${esc(job.message || 'Working…')}</b>${progressBar(job.progress || 0, job.status === 'queued')}<div class="faint small" data-eta style="margin-top:4px">${job.eta ? fmtEta(job.eta) : ''}</div></div></div>`;
  }
  if (!n) {
    let inner;
    if (notesRunning) {
      inner = `<div class="empty" style="padding:30px 10px"><div class="big-icon">${icon('sparkles', 32)}</div><h2>Writing your notes…</h2><p>The AI is reading the whole transcript. You can read and play the transcript meanwhile.</p></div>`;
    } else if (meta.status === 'unavailable') {
      inner = `<div class="card pad-lg" style="text-align:center"><div class="big-icon" style="width:64px;height:64px;margin:0 auto 14px;border-radius:20px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent)">${icon('sparkles', 28)}</div>
        <h3>Get AI meeting notes</h3><p class="muted">${esc(meta.reason || 'Choose a local AI model to write summaries, decisions and action items.')}</p>
        <div class="btn-row" style="justify-content:center;margin-top:14px"><a class="btn primary" href="#/models">${icon('download', 16)} Choose an AI model</a><button class="btn" data-generate>${icon('refresh', 16)} Try again</button></div></div>`;
    } else if (meta.status === 'failed') {
      inner = `<div class="card pad-lg" style="text-align:center"><div class="big-icon" style="width:64px;height:64px;margin:0 auto 14px;border-radius:20px;display:grid;place-items:center;background:var(--bad-soft, var(--accent-soft));color:var(--bad, var(--accent))">${icon('alert', 28)}</div>
        <h3>The notes couldn't be written</h3><p class="muted">${esc(meta.reason || '')}</p><p class="faint small">The transcript is fine. Try again, or choose another AI model.</p>
        <div class="btn-row" style="justify-content:center;margin-top:14px"><button class="btn primary" data-generate>${icon('refresh', 16)} Try again</button><a class="btn" href="#/models">${icon('layers', 16)} AI models</a></div></div>`;
    } else if (meta.status === 'empty') {
      inner = `<div class="empty"><h2>Nothing to summarise</h2><p>${esc(meta.reason || '')}</p></div>`;
    } else {
      inner = `<div class="card pad-lg" style="text-align:center"><h3>No notes yet</h3><p class="muted">Let the AI write a summary, decisions, action items and chapters.</p>
        <div class="btn-row" style="justify-content:center;margin-top:12px"><button class="btn primary" data-generate>${icon('sparkles', 16)} Write notes</button></div></div>`;
    }
    body.innerHTML = banner + inner;
    body.querySelector('[data-generate]')?.addEventListener('click', () => regenerateDialog(ctx));
    return;
  }

  const blocks = [];
  if (meta.last_error && !job) {
    blocks.push(`<div class="callout bad" style="margin-bottom:14px">${icon('alert')}<div><b>Rewriting the notes failed</b>${esc(meta.last_error)}</div></div>`);
  }
  if (n.summary) {
    blocks.push(`<div class="note-block summary" data-block="summary"><h4>${icon('sparkles', 14)} Summary</h4>
      ${n.summary.split(/\n+/).map((p) => `<p>${esc(p)}</p>`).join('')}${editBtn('summary')}</div>`);
  }
  if (n.action_items?.length) {
    const done = n.action_items.filter((a) => a.done).length;
    blocks.push(`<div class="note-block" data-block="action_items"><h4>${icon('listChecks', 14)} Action items <span class="count">${done}/${n.action_items.length}</span></h4>
      ${n.action_items.map((a, i) => `<div class="action ${a.done ? 'done' : ''}" data-ai="${i}">
        <button class="check ${a.done ? 'on' : ''}" data-toggle="${i}" title="Mark done">${icon('check', 13)}</button>
        <div style="flex:1"><div class="task">${esc(a.task)}</div>
        <div class="sub">${ownerChip(a.owner, ctx)}${a.due ? `<span class="badge warn">${icon('calendar', 11)} ${esc(a.due)}</span>` : ''}${tsLink(a.t)}</div></div></div>`).join('')}
      ${editBtn('action_items')}</div>`);
  }
  const list = (key, title, ic, cls = '') => {
    if (!n[key]?.length) return;
    blocks.push(`<div class="note-block" data-block="${key}"><h4>${icon(ic, 14)} ${title} <span class="count">${n[key].length}</span></h4>
      <ul class="note-list ${cls}">${n[key].map((x) => `<li><span>${esc(x)}</span></li>`).join('')}</ul>${editBtn(key)}</div>`);
  };
  list('key_points', 'Key points', 'zap');
  list('decisions', 'Decisions', 'check', 'decisions');
  list('open_questions', 'Open questions', 'help', 'questions');
  for (const [i, s] of (n.sections || []).entries()) {
    blocks.push(`<div class="note-block" data-block="section:${i}"><h4>${icon('bookmark', 14)} ${esc(s.title)}</h4>
      <ul class="note-list">${s.items.map((x) => `<li><span>${esc(x)}</span></li>`).join('')}</ul>${editBtn(`section:${i}`)}</div>`);
  }
  if (n.chapters?.length) {
    blocks.push(`<div class="note-block" data-block="chapters"><h4>${icon('bookmark', 14)} Chapters</h4>
      ${n.chapters.map((c) => `<div class="chapter" data-seek="${c.t}"><span class="ts link">${fmtTime(c.t, ctx.m.duration >= 3600)}</span><div><b>${esc(c.title)}</b>${c.summary ? `<p>${esc(c.summary)}</p>` : ''}</div></div>`).join('')}</div>`);
  }
  if (!blocks.length) blocks.push('<div class="card muted">The AI could not find anything to note in this meeting.</div>');
  const tpl = store.templates.find((t) => t.id === meta.template)?.name;
  body.innerHTML = `${banner}<div class="notes">${blocks.join('')}
    <div class="notes-foot">${icon('sparkles', 13)} <span>Written by ${esc(meta.model || 'AI')}${tpl ? ` · ${esc(tpl)}` : ''}${meta.seconds ? ` · took ${fmtDuration(meta.seconds)}` : ''}${meta.parts > 1 ? ` · read in ${meta.parts} parts` : ''}${meta.cut_short ? ' · shortened to fit' : ''}</span>
      <span class="spacer"></span>
      <button class="btn ghost sm" data-copy>${icon('copy', 14)} Copy</button>
      <button class="btn ghost sm" data-regen ${job ? 'disabled' : ''}>${icon('refresh', 14)} Rewrite…</button></div>
    <div class="faint small" style="padding:0 4px">AI notes can contain mistakes - check important details against the transcript.</div></div>`;

  body.querySelectorAll('[data-toggle]').forEach((b) => b.addEventListener('click', async () => {
    const i = +b.dataset.toggle;
    const a = n.action_items[i];
    a.done = !a.done;
    renderNotes(body, ctx, job);
    try { await api.patch(`/api/meetings/${ctx.id}/action-items/${i}`, { done: a.done }); } catch (e) { errorToast(e); }
  }));
  body.querySelector('[data-regen]')?.addEventListener('click', () => regenerateDialog(ctx));
  body.querySelector('[data-copy]')?.addEventListener('click', () => {
    navigator.clipboard.writeText(notesAsText(ctx)).then(() => toast('Notes copied - paste them anywhere', 'good'));
  });
  body.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => editBlock(body, ctx, job, b.dataset.edit)));
}

function editBtn(key) {
  return `<button class="btn ghost sm icon-only edit-btn" data-edit="${key}" title="Edit">${icon('edit', 14)}</button>`;
}

function editBlock(body, ctx, job, key) {
  const n = ctx.m.notes;
  const block = body.querySelector(`[data-block="${CSS.escape(key)}"]`);
  let value;
  let hint = 'One item per line.';
  if (key === 'summary') { value = n.summary; hint = ''; }
  else if (key === 'action_items') { value = n.action_items.map((a) => [a.task, a.owner, a.due].filter((x, i) => i === 0 || x).join(' | ')).join('\n'); hint = 'One task per line: task | owner | due date'; }
  else if (key.startsWith('section:')) value = n.sections[+key.split(':')[1]].items.join('\n');
  else value = (n[key] || []).join('\n');
  block.innerHTML = `<textarea class="input" rows="${Math.min(14, Math.max(4, value.split('\n').length + 1))}">${esc(value)}</textarea>
    <div class="row" style="margin-top:8px"><span class="hint">${hint}</span><span class="spacer"></span><button class="btn ghost sm" data-cancel>Cancel</button><button class="btn primary sm" data-save>Save</button></div>`;
  const ta = block.querySelector('textarea');
  ta.focus();
  block.querySelector('[data-cancel]').onclick = () => renderNotes(body, ctx, job);
  block.querySelector('[data-save]').onclick = async () => {
    const lines = ta.value.split('\n').map((l) => l.trim()).filter(Boolean);
    if (key === 'summary') n.summary = ta.value.trim();
    else if (key === 'action_items') {
      n.action_items = lines.map((l) => {
        const [task, owner = '', due = ''] = l.split('|').map((x) => x.trim());
        const old = n.action_items.find((a) => a.task === task);
        return { task, owner, due, t: old?.t ?? null, done: old?.done ?? false };
      });
    } else if (key.startsWith('section:')) n.sections[+key.split(':')[1]].items = lines;
    else n[key] = lines;
    try {
      await api.patch(`/api/meetings/${ctx.id}/notes`, { notes: n });
      toast('Notes saved', 'good', 1500);
    } catch (e) { errorToast(e); }
    renderNotes(body, ctx, job);
  };
}

export function notesAsText(ctx) {
  const n = ctx.m.notes;
  const out = [`# ${ctx.m.title}`, ''];
  if (n.summary) out.push('## Summary', n.summary, '');
  const sec = (title, items) => { if (items?.length) out.push(`## ${title}`, ...items.map((x) => `- ${x}`), ''); };
  if (n.action_items?.length) out.push('## Action items', ...n.action_items.map((a) => `- [${a.done ? 'x' : ' '}] ${a.task}${a.owner ? ` (${a.owner})` : ''}${a.due ? ` - due ${a.due}` : ''}`), '');
  sec('Key points', n.key_points);
  sec('Decisions', n.decisions);
  sec('Open questions', n.open_questions);
  for (const s of n.sections || []) sec(s.title, s.items);
  if (n.chapters?.length) out.push('## Chapters', ...n.chapters.map((c) => `- ${fmtTime(c.t)} ${c.title}${c.summary ? ` - ${c.summary}` : ''}`), '');
  return out.join('\n');
}

export async function regenerateDialog(ctx) {
  let models;
  try { models = await api.get('/api/models'); } catch (e) { errorToast(e); return; }
  const opts = ctx.m.options || {};
  const s = store.settings;
  const installed = [...models.llm.filter((m) => m.installed), ...models.custom_llm];
  const builtin = s.llm_backend === 'builtin';
  if (builtin && !installed.length) {
    location.hash = '#/models';
    toast('Download an AI model first', 'warn');
    return;
  }
  const current = opts.llm_model || s.llm_model;
  const body = `
    <div class="label" style="margin-bottom:8px">Style</div>
    <div class="choices" data-templates>${store.templates.map((t) => `<button type="button" class="choice ${t.id === (opts.template || s.notes_template) ? 'on' : ''}" data-tpl="${t.id}"><b>${esc(t.name)}</b><span>${esc(t.description)}</span></button>`).join('')}</div>
    <div class="grid" style="grid-template-columns:1fr 1fr;gap:14px;margin-top:16px">
      ${builtin ? `<div class="field"><label>AI model</label><select class="select" name="llm_model">${installed.map((m) => `<option value="${m.key}" ${m.key === current ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select>
        <div class="hint">Bigger models write better notes but take longer.</div></div>` : ''}
      <div class="field"><label>Level of detail</label><select class="select" name="notes_detail">${[['brief', 'Brief'], ['standard', 'Standard'], ['detailed', 'Detailed']].map(([v, l]) => `<option value="${v}" ${v === (opts.notes_detail || s.notes_detail) ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <div class="field"><label>Write notes in</label><select class="select" name="notes_language">
        <option value="auto">Same language as the meeting</option>${Object.entries(store.languages).filter(([k]) => k !== 'auto').map(([k, v]) => `<option value="${k}" ${k === opts.notes_language ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div>
    </div>
    <div class="field"><label>Extra instructions</label><input class="input" name="instructions" value="${esc(opts.instructions || '')}" placeholder="e.g. Include every number mentioned; call Speaker 2 'the client'"></div>
    <div class="hint">Tip: name the speakers first (Speakers tab) so the notes use real names.</div>`;
  await modal({
    title: 'Write the notes again', icon: 'sparkles', wide: true, body,
    onOpen: (m) => m.querySelector('[data-templates]').addEventListener('click', (e) => {
      const c = e.target.closest('[data-tpl]');
      if (c) m.querySelectorAll('[data-tpl]').forEach((x) => x.classList.toggle('on', x === c));
    }),
    actions: [{ label: 'Cancel', kind: 'ghost' }, {
      label: 'Write notes', kind: 'primary', icon: 'sparkles', onClick: async (m) => {
        const payload = {
          template: m.querySelector('[data-tpl].on')?.dataset.tpl,
          notes_detail: m.querySelector('[name=notes_detail]').value,
          notes_language: m.querySelector('[name=notes_language]').value,
          instructions: m.querySelector('[name=instructions]').value,
        };
        const llm = m.querySelector('[name=llm_model]');
        if (llm) payload.llm_model = llm.value;
        try {
          await api.post(`/api/meetings/${ctx.id}/notes`, payload);
          kickPoll();
          toast('Writing new notes…', 'good');
          setTimeout(() => ctx.reload(), 500);
        } catch (e) { errorToast(e); return false; }
      },
    }],
  });
}
