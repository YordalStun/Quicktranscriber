// First-run setup: language, quality level, downloads.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtBytes, fmtDuration } from '../util.js';
import { modal, progressBar, errorToast, toast } from '../ui.js';
import { store, saveSettings, loadStatus, kickPoll, subscribe } from '../store.js';

function guessLanguage() {
  const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
  return store.languages[nav] ? nav : 'en';
}

function presets(models) {
  const hw = models.hardware;
  const rec = models.recommend;
  const w = (k) => models.whisper.find((m) => m.key === k);
  const l = (k) => models.llm.find((m) => m.key === k);
  const bigLlm = hw.vram_gb >= 9 ? 'gemma4-12b' : hw.vram_gb >= 7 || hw.ram_gb >= 15 ? 'qwen3.5-9b' : 'qwen3.5-4b';
  const list = [
    { id: 'fast', name: 'Fast', desc: 'Quick results on older or low-power laptops. Pick the meeting language for best accuracy.', whisper: 'small', llm: 'qwen3.5-2b' },
    { id: 'balanced', name: 'Balanced', desc: 'Very accurate transcripts and good notes. Recommended for most computers.', whisper: 'large-v3-turbo', llm: 'qwen3.5-4b' },
    { id: 'best', name: 'Best quality', desc: 'The most accurate transcripts and the most detailed notes. Slow without an NVIDIA GPU.', whisper: 'large-v3', llm: bigLlm },
  ];
  for (const p of list) {
    p.w = w(p.whisper);
    p.l = l(p.llm);
    p.size = (p.w.installed ? 0 : p.w.size) + (p.l.installed ? 0 : p.l.size) + 47e6;
    p.minutes = p.w.minutes_per_hour + estimateNotesMinutes(p.l, hw);
    p.fits = p.l.fits;
  }
  const recommended = rec.whisper === 'large-v3' && hw.vram_gb >= 5.5 ? 'best' : hw.ram_gb < 7 ? 'fast' : 'balanced';
  return { list, recommended };
}

function estimateNotesMinutes(llm, hw) {
  // very rough: tokens/s from model size and hardware
  const gb = llm.size / 1e9;
  const gpu = hw.vram_gb >= llm.ram_gb;
  const speed = gpu ? 60 / Math.max(1, gb) * 3 : Math.max(1.5, (hw.cores * 2.2) / Math.max(1, gb));
  return Math.round(Math.min(90, 12000 / speed / 60 + 2));
}

export async function openOnboarding() {
  let models;
  try { models = await api.get('/api/models'); } catch (e) { errorToast(e); return; }
  const hw = models.hardware;
  const { list, recommended } = presets(models);
  let step = 0;
  let language = store.settings.language !== 'auto' ? store.settings.language : guessLanguage();
  let preset = recommended;
  let gpu = hw.nvidia && !models.gpu_pack.installed;
  let unsub = null;

  const body = document.createElement('div');
  body.className = 'onboard';

  function dotsBar() { return `<div class="steps-dots">${[0, 1, 2, 3].map((i) => `<i class="${i <= step ? 'on' : ''}"></i>`).join('')}</div>`; }

  function draw() {
    const gpuName = hw.gpus?.[0]?.name;
    if (step === 0) {
      body.innerHTML = `${dotsBar()}<div style="text-align:center">
        <div class="welcome-art"><div class="brand-mark"><span><i></i><i></i><i></i><i></i><i></i></span></div></div>
        <h2 style="font-family:var(--font-display);font-size:26px;margin:0 0 6px">Welcome to QuickTranscriber</h2>
        <p class="muted" style="max-width:520px;margin:0 auto 20px">Record or import meetings, get accurate transcripts with who-said-what, and AI meeting notes - all <b>100% on this computer</b>. No accounts, no cloud, no subscriptions.</p></div>
        <div class="hw-card">
          <div class="hw-item"><div class="ic">${icon('cpu')}</div><div><b>${hw.cores}-core processor</b><span>${esc(hw.cpu)}</span></div></div>
          <div class="hw-item"><div class="ic">${icon('layers')}</div><div><b>${hw.ram_gb} GB memory</b><span>RAM</span></div></div>
          <div class="hw-item"><div class="ic">${icon('zap')}</div><div><b>${gpuName ? esc(gpuName.replace('NVIDIA ', '')) : 'No NVIDIA GPU'}</b><span>${gpuName ? `${hw.vram_gb} GB - great for speed` : 'Works fine - just takes longer'}</span></div></div>
        </div>
        <div class="callout good" style="margin-top:16px">${icon('shield')}<div><b>Private by design</b>The internet is only used in the next steps to download the AI models, once. After that, everything works offline.</div></div>`;
    } else if (step === 1) {
      body.innerHTML = `${dotsBar()}<h2 style="font-family:var(--font-display);margin:0 0 6px">What language are your meetings in?</h2>
        <p class="muted">Setting this makes transcripts much more accurate than guessing - for example, British accents are sometimes mistaken for Welsh by small models. You can change it for any meeting.</p>
        <select class="select" style="font-size:16px;padding:12px 14px" data-lang>${Object.entries(store.languages).map(([k, v]) => `<option value="${k}" ${k === language ? 'selected' : ''}>${esc(k === 'auto' ? 'Mixed / detect for each meeting' : v)}</option>`).join('')}</select>`;
      body.querySelector('[data-lang]').onchange = (e) => { language = e.target.value; };
    } else if (step === 2) {
      body.innerHTML = `${dotsBar()}<h2 style="font-family:var(--font-display);margin:0 0 6px">Choose your quality</h2>
        <p class="muted">Bigger models are more accurate but slower. Times are estimates for a 1-hour meeting on this computer. You can switch models any time.</p>
        <div class="grid cols-3" style="margin-top:14px">${list.map((p) => `<button type="button" class="choice preset ${p.id === preset ? 'on' : ''}" data-preset="${p.id}">
          <b>${esc(p.name)} ${p.id === recommended ? '<span class="badge accent">Recommended</span>' : ''}</b>
          <div class="big">~${fmtDuration(p.minutes * 60)}</div><span>per hour of meeting</span>
          <span style="margin-top:10px;color:var(--text-2)">${esc(p.desc)}</span>
          <span style="margin-top:10px">${icon('fileText', 12)} ${esc(p.w.name)}<br>${icon('sparkles', 12)} ${esc(p.l.name)}${p.fits ? '' : ' <span style="color:var(--warn)">(may be too big)</span>'}</span>
          <span style="margin-top:8px">${icon('download', 12)} ${p.size > 5e7 ? fmtBytes(p.size) + ' download' : 'Already downloaded'}</span></button>`).join('')}</div>
        ${hw.nvidia && !models.gpu_pack.installed ? `<label class="callout accent" style="margin-top:14px;cursor:pointer">${icon('zap')}<div style="flex:1"><b>Use my NVIDIA GPU for transcription</b>Adds about 1.3 GB of NVIDIA libraries to the app folder. Transcription gets 5-20× faster.</div>
          <input type="checkbox" data-gpu ${gpu ? 'checked' : ''} style="width:20px;height:20px"></label>` : ''}`;
      body.querySelectorAll('[data-preset]').forEach((b) => b.addEventListener('click', () => { preset = b.dataset.preset; draw(); }));
      body.querySelector('[data-gpu]')?.addEventListener('change', (e) => { gpu = e.target.checked; });
    } else {
      const downloads = store.downloads.filter((d) => d.status !== 'cancelled');
      body.innerHTML = `${dotsBar()}<h2 style="font-family:var(--font-display);margin:0 0 6px">Downloading your models</h2>
        <p class="muted">This happens once. You can start using the app right away - a meeting will simply wait until its model is ready.</p>
        <div class="stack" style="margin-top:14px">${downloads.map((d) => {
          const pct = d.total ? d.done / d.total : 0;
          return `<div class="card" style="padding:12px 14px"><div class="row"><b style="flex:1">${esc(d.name)}</b>
            <span class="small ${d.status === 'error' ? '' : 'faint'}" style="${d.status === 'error' ? 'color:var(--bad)' : ''}">${d.status === 'done' ? `${icon('check', 14)} Ready` : d.status === 'error' ? esc(d.error || 'Failed') : d.status === 'extracting' ? 'Unpacking…' : `${fmtBytes(d.done)} / ${fmtBytes(d.total)}`}</span></div>
            ${d.status === 'done' ? '' : `<div style="margin-top:8px">${progressBar(pct, !d.total)}</div>`}</div>`;
        }).join('') || '<div class="card">Everything is already downloaded.</div>'}</div>`;
    }
    const foot = document.querySelector('.onboard-foot');
    if (foot) {
      foot.querySelector('[data-back]').hidden = step === 0 || step === 3;
      foot.querySelector('[data-next]').textContent = step === 2 ? 'Download & finish' : step === 3 ? 'Start using QuickTranscriber' : 'Continue';
    }
  }

  async function startDownloads() {
    const p = list.find((x) => x.id === preset);
    await saveSettings({ language, whisper_model: p.whisper, llm_model: p.llm, llm_backend: 'builtin', onboarded: true });
    const jobs = [];
    if (!p.w.installed) jobs.push({ kind: 'whisper', key: p.whisper });
    if (!models.speaker.find((m) => m.key === 'titanet_small')?.installed) jobs.push({ kind: 'speaker', key: 'titanet_small' });
    if (!models.engine.installed) jobs.push({ kind: 'engine' });
    if (!p.l.installed) jobs.push({ kind: 'llm', key: p.llm });
    for (const j of jobs) {
      try { await api.post('/api/models/download', j); } catch (e) { errorToast(e); }
    }
    if (gpu) api.post('/api/gpu/install').catch(errorToast);
    kickPoll();
    await loadStatus();
  }

  const content = document.createElement('div');
  content.appendChild(body);
  modal({
    title: 'Set up', xwide: true, body: content, dismissable: true,
    onOpen: (m, close) => {
      m.querySelector('.modal-head').remove();
      const foot = document.createElement('div');
      foot.className = 'modal-foot onboarding-foot onboard-foot';
      foot.innerHTML = `<button class="btn ghost" data-skip style="margin-right:auto">Skip setup</button><button class="btn ghost" data-back>Back</button><button class="btn primary" data-next>Continue</button>`;
      m.appendChild(foot);
      foot.querySelector('[data-skip]').onclick = async () => { await saveSettings({ onboarded: true, language }); close(); };
      foot.querySelector('[data-back]').onclick = () => { step = Math.max(0, step - 1); draw(); };
      foot.querySelector('[data-next]').onclick = async (e) => {
        if (step === 2) {
          e.target.disabled = true;
          await startDownloads();
          e.target.disabled = false;
          step = 3;
          draw();
          unsub = subscribe((w) => { if (w === 'jobs' && step === 3) draw(); });
        } else if (step === 3) {
          unsub?.();
          close();
          toast('All set - record or drop a file to begin', 'good');
        } else { step++; draw(); }
      };
      draw();
    },
  }).then(() => unsub?.());
}
