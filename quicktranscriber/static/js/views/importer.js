// "Transcribe a file" dialog, also used after recording.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtBytes, fmtDuration } from '../util.js';
import { modal, toast, errorToast } from '../ui.js';
import { store, kickPoll } from '../store.js';

export function fileDuration(file) {
  return new Promise((resolve) => {
    const a = document.createElement('audio');
    const url = URL.createObjectURL(file);
    const done = (v) => { URL.revokeObjectURL(url); resolve(v); };
    a.preload = 'metadata';
    a.onloadedmetadata = () => done(isFinite(a.duration) ? a.duration : 0);
    a.onerror = () => done(0);
    setTimeout(() => done(0), 4000);
    a.src = url;
  });
}

const TPL_ICONS = { users: 'users', gavel: 'gavel', zap: 'zap', kanban: 'kanban', message: 'message', briefcase: 'briefcase', mic: 'mic', book: 'book', lightbulb: 'lightbulb' };

export function optionsForm(models, { duration = 0, defaults = {} } = {}) {
  const s = store.settings;
  const langOpts = Object.entries(store.languages).map(([k, v]) => `<option value="${k}" ${k === (defaults.language ?? s.language) ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const current = defaults.whisper_model || store.status?.whisper_model;
  const whisperOpts = models.whisper.map((m) => {
    const est = duration ? ` · ~${fmtDuration(duration / 3600 * m.minutes_per_hour * 60)}` : ` · ~${m.minutes_per_hour} min per hour`;
    return `<option value="${m.key}" ${m.key === current ? 'selected' : ''}>${esc(m.name)}${m.installed ? '' : ' (will download ' + fmtBytes(m.size) + ')'}${est}</option>`;
  }).join('');
  const tplCur = defaults.template || s.notes_template;
  const templates = store.templates.map((t) => `<button type="button" class="choice ${t.id === tplCur ? 'on' : ''}" data-tpl="${t.id}">
      <b>${icon(TPL_ICONS[t.icon] || 'fileText', 15)} ${esc(t.name)}</b><span>${esc(t.description)}</span></button>`).join('');
  const llmInstalled = [...models.llm.filter((m) => m.installed), ...models.custom_llm];
  const llmCur = defaults.llm_model || s.llm_model;
  const llmOpts = llmInstalled.map((m) => `<option value="${m.key}" ${m.key === llmCur ? 'selected' : ''}>${esc(m.name)}</option>`).join('');
  const speakersOpts = [['0', 'Detect automatically'], ['1', 'Just one person'], ...Array.from({ length: 11 }, (_, i) => [String(i + 2), `${i + 2} people`])]
    .map(([v, l]) => `<option value="${v}" ${String(defaults.num_speakers || 0) === v ? 'selected' : ''}>${l}</option>`).join('');
  const notesOn = defaults.notes ?? s.auto_notes;
  const noLlm = s.llm_backend === 'builtin' && !llmInstalled.length;
  return `
    <div class="grid" style="grid-template-columns:1fr 1fr;gap:14px">
      <div class="field"><label>Language spoken</label><select class="select" name="language">${langOpts}</select>
        <div class="hint" data-lang-hint></div></div>
      <div class="field"><label>How many people?</label><select class="select" name="num_speakers">${speakersOpts}</select>
        <div class="hint">Telling us helps separate the voices.</div></div>
    </div>
    <div class="field"><label>Transcription model</label><select class="select" name="whisper_model">${whisperOpts}</select>
      <div class="hint">Bigger models are more accurate but slower. Times are estimates for this computer.</div></div>
    <div class="field">
      <div class="row"><label class="label" style="flex:1">Write meeting notes with AI</label>
        <label class="switch"><input type="checkbox" name="notes" ${notesOn ? 'checked' : ''}><span class="track"></span></label></div>
      ${noLlm ? `<div class="callout warn small" style="margin-top:6px">${icon('info')}<div>No AI model downloaded yet - you'll get the transcript now and can add notes later. <a href="#/models" data-close-modal>Choose an AI model</a></div></div>` : ''}
    </div>
    <div data-notes-opts ${notesOn ? '' : 'hidden'}>
      <div class="label" style="margin-bottom:8px">Notes style</div>
      <div class="choices" data-templates>${templates}</div>
    </div>
    <details style="margin-top:16px"><summary class="muted" style="cursor:pointer;font-weight:600">More options</summary>
      <div style="margin-top:14px">
        <div class="field"><label>Names & special words</label><input class="input" name="vocabulary" value="${esc(defaults.vocabulary ?? s.vocabulary)}" placeholder="e.g. Siân, Kubernetes, OKRs, Llanelli">
          <div class="hint">Helps spell names and jargon correctly. Known speakers are added automatically.</div></div>
        <div class="grid" style="grid-template-columns:1fr 1fr;gap:14px">
          <div class="field"><label>AI model for notes</label><select class="select" name="llm_model">${llmOpts || '<option value="">(none downloaded)</option>'}</select></div>
          <div class="field"><label>Level of detail</label><select class="select" name="notes_detail">
            ${[['brief', 'Brief'], ['standard', 'Standard'], ['detailed', 'Detailed']].map(([v, l]) => `<option value="${v}" ${v === (defaults.notes_detail || s.notes_detail) ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
        </div>
        <div class="field"><label>Extra instructions for the notes</label><input class="input" name="instructions" value="${esc(defaults.instructions ?? s.notes_instructions)}" placeholder="e.g. Focus on budget decisions; use British spelling"></div>
      </div></details>`;
}

export function bindOptionsForm(root, models) {
  root.querySelector('[data-templates]')?.addEventListener('click', (e) => {
    const c = e.target.closest('[data-tpl]');
    if (!c) return;
    root.querySelectorAll('[data-tpl]').forEach((x) => x.classList.toggle('on', x === c));
  });
  const notes = root.querySelector('[name=notes]');
  notes?.addEventListener('change', () => { root.querySelector('[data-notes-opts]').hidden = !notes.checked; });
  const lang = root.querySelector('[name=language]');
  const model = root.querySelector('[name=whisper_model]');
  const hint = root.querySelector('[data-lang-hint]');
  const update = () => {
    const m = models.whisper.find((x) => x.key === model.value);
    if (lang.value === 'auto' && m && m.accuracy <= 3) {
      hint.innerHTML = `<span style="color:var(--warn)">Small models often guess the language wrong - picking it is safer.</span>`;
    } else if (m?.english_only && lang.value !== 'en' && lang.value !== 'auto') {
      hint.innerHTML = `<span style="color:var(--warn)">${esc(m.name)} only understands English.</span>`;
    } else hint.textContent = '';
  };
  lang?.addEventListener('change', update);
  model?.addEventListener('change', update);
  update();
  root.querySelectorAll('[data-close-modal]').forEach((a) => a.addEventListener('click', () => root.closest('.overlay')?.remove()));
}

export function readOptions(root) {
  const v = (n) => root.querySelector(`[name=${n}]`);
  return {
    language: v('language').value,
    num_speakers: +v('num_speakers').value,
    whisper_model: v('whisper_model').value,
    notes: v('notes').checked,
    template: root.querySelector('[data-tpl].on')?.dataset.tpl || store.settings.notes_template,
    vocabulary: v('vocabulary').value,
    llm_model: v('llm_model').value,
    notes_detail: v('notes_detail').value,
    instructions: v('instructions').value,
    diarization: +v('num_speakers').value !== 1,
  };
}

export async function openImport(files) {
  files = files.filter((f) => f.size > 0);
  if (!files.length) return;
  let models;
  try { models = await api.get('/api/models'); } catch (e) { errorToast(e); return; }
  const durations = await Promise.all(files.map(fileDuration));
  const total = durations.reduce((a, b) => a + b, 0);
  const list = files.map((f, i) => `<div class="row" style="padding:8px 10px;background:var(--bg-2);border-radius:10px;margin-bottom:6px">
      ${icon('fileText', 16)}<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
      <span class="faint small">${durations[i] ? fmtDuration(durations[i]) + ' · ' : ''}${fmtBytes(f.size)}</span></div>`).join('');
  const body = `<div style="margin-bottom:14px">${list}</div>
    ${files.length === 1 ? `<div class="field"><label>Title <span class="faint">(optional - AI will suggest one)</span></label><input class="input" name="title" placeholder="${esc(files[0].name.replace(/\.[^.]+$/, ''))}"></div>` : ''}
    ${optionsForm(models, { duration: total })}
    <div data-upload hidden style="margin-top:12px"></div>`;
  await modal({
    title: files.length === 1 ? 'Transcribe recording' : `Transcribe ${files.length} recordings`,
    subtitle: 'Processed privately on this computer.',
    icon: 'upload',
    wide: true,
    body,
    onOpen: (m) => bindOptionsForm(m, models),
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      {
        label: files.length === 1 ? 'Start' : `Start all ${files.length}`, kind: 'primary', icon: 'sparkles',
        onClick: async (m, btn) => {
          const opts = readOptions(m);
          const title = m.querySelector('[name=title]')?.value.trim() || '';
          btn.disabled = true;
          const up = m.querySelector('[data-upload]');
          up.hidden = false;
          const ids = [];
          try {
            for (let i = 0; i < files.length; i++) {
              up.innerHTML = `<div class="small muted" style="margin-bottom:6px">Adding ${esc(files[i].name)}…</div><div class="progress"><i style="width:0%"></i></div>`;
              const fd = new FormData();
              fd.append('file', files[i]);
              fd.append('options', JSON.stringify(opts));
              fd.append('title', files.length === 1 ? title : '');
              fd.append('last_modified', String(files[i].lastModified || 0));
              const r = await api.upload('/api/meetings/upload', fd, (p) => { up.querySelector('i').style.width = `${Math.round(p * 100)}%`; });
              ids.push(r.id);
            }
          } catch (e) {
            errorToast(e);
            btn.disabled = false;
            return false;
          }
          kickPoll();
          const { askNotificationPermission, navigate } = await import('../app.js');
          askNotificationPermission();
          toast(files.length === 1 ? 'Processing started' : `${files.length} recordings queued`, 'good');
          navigate(ids.length === 1 ? `#/m/${ids[0]}` : '#/');
          return true;
        },
      },
    ],
  });
}

export function pickFiles() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.accept = 'audio/*,video/*,.m4a,.mp3,.wav,.ogg,.opus,.flac,.webm,.mp4,.mov,.mkv,.wma,.aac,.amr,.3gp';
  inp.onchange = () => openImport([...inp.files]);
  inp.click();
}
