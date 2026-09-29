// Settings - saved instantly.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, debounce } from '../util.js';
import { toast, errorToast, confirmDialog, segmented, bindSegmented, switchEl } from '../ui.js';
import { store, saveSettings, loadStatus } from '../store.js';

export async function render(root) {
  let models = null;
  try { models = await api.get('/api/models'); } catch { /* optional */ }
  const s = store.settings;
  const st = store.status;
  const langOpts = (sel, withAuto = true, autoLabel) => Object.entries(store.languages)
    .filter(([k]) => withAuto || k !== 'auto')
    .map(([k, v]) => `<option value="${k}" ${k === sel ? 'selected' : ''}>${esc(k === 'auto' && autoLabel ? autoLabel : v)}</option>`).join('');
  const whisperOpts = (models?.whisper || []).map((m) => `<option value="${m.key}" ${m.key === (s.whisper_model || st.whisper_model) ? 'selected' : ''}>${esc(m.name)}${m.installed ? '' : ' (not downloaded)'}</option>`).join('');
  const row = (label, hint, control, stacked = false) => `<div class="set-row ${stacked ? 'stacked' : ''}"><div><span class="label">${label}</span>${hint ? `<div class="hint">${hint}</div>` : ''}</div><div class="control">${control}</div></div>`;

  root.innerHTML = `<div class="settings">
    <div class="page-head"><div><div class="eyebrow">Preferences</div><h1>Settings</h1><p>Changes are saved automatically.</p></div></div>

    <div class="set-section"><h2>Appearance</h2>
      ${row('Theme', '', segmented([{ value: 'dark', label: 'Dark' }, { value: 'light', label: 'Light' }], s.theme, 'data-key="theme"'))}
    </div>

    <div class="set-section"><h2>Transcription</h2>
      ${row('Language of your meetings', 'Choosing it is more reliable than detecting it, especially with accents and small models.', `<select class="select" data-key="language">${langOpts(s.language, true)}</select>`)}
      ${row('Speech model', 'Used for new meetings. Manage models on the AI models page.', `<select class="select" data-key="whisper_model">${whisperOpts}</select>`)}
      ${row('Accuracy', 'Careful checks each sentence several ways (beam search). Maximum also uses the previous sentences as context.', segmented([{ value: 'fast', label: 'Quick' }, { value: 'accurate', label: 'Careful' }, { value: 'max', label: 'Maximum' }], s.whisper_quality, 'data-key="whisper_quality"'))}
      ${row('Run on', st.hardware.nvidia ? 'Auto uses your NVIDIA GPU when GPU support is installed.' : 'No NVIDIA GPU detected - the processor is used.', segmented([{ value: 'auto', label: 'Auto' }, { value: 'cpu', label: 'Processor' }, { value: 'cuda', label: 'NVIDIA GPU' }], s.device, 'data-key="device"'))}
      ${row('Names & special words', 'Comma-separated. Helps spell names, places and jargon correctly in every meeting.', `<textarea class="input" rows="2" data-key="vocabulary" placeholder="e.g. Siân, Aberystwyth, Kubernetes, OKRs">${esc(s.vocabulary)}</textarea>`, true)}
    </div>

    <div class="set-section"><h2>Speakers</h2>
      ${row('Tell speakers apart', 'Works out who is speaking when.', switchEl(s.diarization, 'data-key="diarization"'))}
      ${row('Detail', 'Precise looks at the audio more closely, which takes about twice as long.', segmented([{ value: 'fast', label: 'Fast' }, { value: 'balanced', label: 'Balanced' }, { value: 'precise', label: 'Precise' }], s.speaker_detail, 'data-key="speaker_detail"'))}
      ${row('Learn from confident matches', 'When someone is recognised with high confidence, add a couple of new samples automatically. Off = only learn when you confirm.', switchEl(s.auto_learn_confident, 'data-key="auto_learn_confident"'))}
    </div>

    <div class="set-section"><h2>Meeting notes</h2>
      ${row('Write notes automatically', 'Right after transcription finishes.', switchEl(s.auto_notes, 'data-key="auto_notes"'))}
      ${row('Default style', '', `<select class="select" data-key="notes_template">${store.templates.map((t) => `<option value="${t.id}" ${t.id === s.notes_template ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select>`)}
      ${row('Level of detail', '', segmented([{ value: 'brief', label: 'Brief' }, { value: 'standard', label: 'Standard' }, { value: 'detailed', label: 'Detailed' }], s.notes_detail, 'data-key="notes_detail"'))}
      ${row('Notes language', '', `<select class="select" data-key="notes_language">${langOpts(s.notes_language, true, 'Same as the meeting')}</select>`)}
      ${row('Always include these instructions', 'e.g. “Use British spelling” or “Our team: Mark (chair), Rhian (treasurer)”.', `<textarea class="input" rows="2" data-key="notes_instructions">${esc(s.notes_instructions)}</textarea>`, true)}
      ${row('Let the AI think first', 'Reasoning models plan before writing. Can improve long, messy meetings but is much slower.', switchEl(s.llm_thinking, 'data-key="llm_thinking"'))}
    </div>

    <div class="set-section"><h2>AI engine</h2>
      ${row('Run AI models with', 'Built-in needs nothing else installed. Ollama or LM Studio can be used if you already have them.', segmented([{ value: 'builtin', label: 'Built-in' }, { value: 'ollama', label: 'Ollama' }, { value: 'openai', label: 'LM Studio / other' }], s.llm_backend, 'data-key="llm_backend"'))}
      <div data-backend="ollama" ${s.llm_backend === 'ollama' ? '' : 'hidden'}>
        ${row('Ollama address', '', `<input class="input" data-key="ollama_url" value="${esc(s.ollama_url)}">`)}
        ${row('Ollama model', 'e.g. qwen3:8b - leave empty to use the first one.', `<input class="input" data-key="llm_model_ext" value="${esc(s.llm_backend === 'ollama' ? s.llm_model : '')}">`)}
      </div>
      <div data-backend="openai" ${s.llm_backend === 'openai' ? '' : 'hidden'}>
        ${row('Server address', 'LM Studio: http://127.0.0.1:1234/v1 · Jan: http://127.0.0.1:1337/v1', `<input class="input" data-key="openai_url" value="${esc(s.openai_url)}">`)}
        ${row('Model name', 'Leave empty to use the loaded model.', `<input class="input" data-key="llm_model_ext2" value="${esc(s.llm_backend === 'openai' ? s.llm_model : '')}">`)}
        ${row('API key', 'Only if your local server needs one.', `<input class="input" type="password" data-key="openai_key" value="${esc(s.openai_key)}">`)}
      </div>
      <div data-backend="builtin" ${s.llm_backend === 'builtin' ? '' : 'hidden'}>
        ${row('Run AI on', 'GPU is much faster when the model fits in video memory.', segmented([{ value: 'auto', label: 'Auto' }, { value: 'cpu', label: 'Processor' }, { value: 'gpu', label: 'GPU' }], s.llm_device, 'data-key="llm_device"'))}
        ${row('Memory (context) size', 'How much text the AI reads at once. Bigger = fewer parts for long meetings, but more memory.', `<select class="select" data-key="llm_context">${[[0, 'Automatic'], [4096, '4K tokens'], [8192, '8K tokens'], [16384, '16K tokens'], [32768, '32K tokens'], [65536, '64K tokens']].map(([v, l]) => `<option value="${v}" ${+s.llm_context === v ? 'selected' : ''}>${l}</option>`).join('')}</select>`)}
        ${row('Custom llama-server', 'Advanced: path to your own llama-server program.', `<input class="input" data-key="llama_server_path" value="${esc(s.llama_server_path)}" placeholder="(use the built-in engine)">`)}
      </div>
    </div>

    <div class="set-section"><h2>Automatic import</h2>
      ${row('Watch a folder', 'New recordings that appear in this folder are transcribed automatically - great if your phone recordings sync to this PC (OneDrive, Google Drive, Dropbox…).', switchEl(s.watch_enabled, 'data-key="watch_enabled"'))}
      ${row('Folder', 'Paste the folder path, e.g. C:\\Users\\you\\OneDrive\\Recordings (in Explorer: click the address bar and copy).', `<input class="input" data-key="watch_folder" value="${esc(s.watch_folder)}" placeholder="Folder to watch">`, true)}
      <div class="small faint" id="watch-status" style="padding:8px 4px"></div>
    </div>

    <div class="set-section"><h2>Storage & privacy</h2>
      ${row('Keep original audio files', 'Keeps the file you imported next to the converted copy.', switchEl(s.keep_original_audio, 'data-key="keep_original_audio"'))}
      ${row('App folder', `Everything - meetings, voices, models - is stored here:<br><code style="font-size:12px">${esc(st.data_folder)}</code><br>Delete the folder to remove the app completely.`, `<div class="btn-row"><button class="btn" data-open="data">${icon('folder', 16)} Open data folder</button></div>`)}
      <div class="callout good" style="margin-top:12px">${icon('shield')}<div><b>Private by design</b>Audio, transcripts, notes and voiceprints never leave this computer. The internet is only used when you download a model.</div></div>
    </div>

    <div class="set-section"><h2>About</h2>
      ${row(`QuickTranscriber ${esc(st.version)}`, 'Open-source models: OpenAI Whisper, pyannote, NVIDIA TitaNet, llama.cpp and the model you choose.', `<div class="btn-row"><button class="btn" data-onboard>${icon('wand', 16)} Setup guide</button><button class="btn danger" data-quit>${icon('logout', 16)} Quit</button></div>`)}
    </div>
  </div>`;

  const saved = debounce(() => toast('Saved', 'good', 1200), 400);
  async function watchStatus() {
    const box = root.querySelector('#watch-status');
    if (!box) return;
    try {
      const w = await api.get('/api/watch');
      if (!w.folder) { box.textContent = ''; return; }
      if (!w.exists) { box.innerHTML = `<span style="color:var(--warn)">${icon('alert', 13)} This folder doesn't exist.</span>`; return; }
      box.innerHTML = `${icon(w.enabled ? 'check' : 'info', 13)} ${w.enabled ? 'Watching' : 'Not watching'} · ${w.files} audio file${w.files === 1 ? '' : 's'} in the folder. `
        + (w.files ? `<a href="#" data-import-existing>Also transcribe the files already there</a>` : '');
      box.querySelector('[data-import-existing]')?.addEventListener('click', async (e) => {
        e.preventDefault();
        if (!w.enabled) { toast('Turn on “Watch a folder” first', 'warn'); return; }
        if (!(await confirmDialog('Transcribe existing files?', `All ${w.files} audio files in the folder will be added to the queue.`, { ok: 'Transcribe all' }))) return;
        await api.post('/api/watch/import-existing');
        toast('Importing - they will appear in Meetings shortly', 'good');
      });
    } catch { box.textContent = ''; }
  }
  async function save(key, value) {
    try {
      if (key === 'llm_model_ext' || key === 'llm_model_ext2') key = 'llm_model';
      if (key === 'theme') { try { localStorage.setItem('qt-theme', value); } catch { /* ignore */ } }
      await saveSettings({ [key]: value });
      if (['whisper_model', 'device', 'llm_backend'].includes(key)) await loadStatus();
      if (key.startsWith('watch_')) watchStatus();
      saved();
    } catch (e) { errorToast(e); }
  }

  bindSegmented(root, (seg, value) => {
    const key = seg.dataset.key;
    save(key, value);
    if (key === 'llm_backend') root.querySelectorAll('[data-backend]').forEach((d) => { d.hidden = d.dataset.backend !== value; });
  });
  root.querySelectorAll('select[data-key]').forEach((el) => el.addEventListener('change', () => save(el.dataset.key, el.dataset.key === 'llm_context' ? +el.value : el.value)));
  root.querySelectorAll('input[type=checkbox][data-key]').forEach((el) => el.addEventListener('change', () => save(el.dataset.key, el.checked)));
  root.querySelectorAll('input.input[data-key], textarea[data-key]').forEach((el) => el.addEventListener('change', () => save(el.dataset.key, el.value.trim())));
  watchStatus();
  root.querySelector('[data-open]').onclick = () => api.post('/api/open-folder', { what: 'data' }).catch(errorToast);
  root.querySelector('[data-onboard]').onclick = async () => (await import('./onboarding.js')).openOnboarding();
  root.querySelector('[data-quit]').onclick = async () => {
    if (!(await confirmDialog('Quit QuickTranscriber?', 'Processing in progress will continue next time you start the app.', { ok: 'Quit', danger: true }))) return;
    await api.post('/api/shutdown').catch(() => {});
    document.body.innerHTML = '<div class="empty" style="padding-top:20vh"><h2>QuickTranscriber has stopped</h2><p>You can close this window. Start it again from the app folder.</p></div>';
  };
}
