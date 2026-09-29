// AI models: speech recognition, notes, speaker recognition, engine and hardware.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtBytes, dots, throttle } from '../util.js';
import { toast, errorToast, confirmDialog, progressBar, promptDialog } from '../ui.js';
import { store, subscribe, saveSettings, loadStatus, kickPoll } from '../store.js';

const TIERS = {
  light: ['Light', 'Runs on almost any laptop'],
  balanced: ['Balanced', 'Good notes on most computers'],
  quality: ['Quality', 'Great notes - best with 16 GB RAM or an NVIDIA GPU'],
  best: ['Best', 'Top quality - needs a powerful PC or GPU'],
};

export async function render(root) {
  let data = null;

  async function load() {
    try { data = await api.get('/api/models'); } catch (e) { errorToast(e); return; }
    draw();
  }

  function dl(d) {
    if (!d) return '';
    if (d.status === 'error') return `<div class="dl-progress" style="color:var(--bad)">${icon('alert', 14)} ${esc(d.error || 'Download failed')}</div>`;
    const pct = d.total ? d.done / d.total : 0;
    const speed = d.speed ? ` · ${fmtBytes(d.speed)}/s` : '';
    const left = d.speed && d.total ? ` · ${Math.max(1, Math.round((d.total - d.done) / d.speed / 60))} min left` : '';
    return `<div class="dl-progress">${progressBar(pct, !d.total)}<span class="tabular">${d.status === 'extracting' ? 'Unpacking…' : `${fmtBytes(d.done)} of ${fmtBytes(d.total)}${speed}${left}`}</span>
      <button class="btn ghost sm" data-cancel="${esc(d.id)}">Cancel</button></div>`;
  }

  function hardwareCard() {
    const hw = data.hardware;
    const gpu = hw.gpus?.[0];
    const pack = data.gpu_pack;
    const st = store.status;
    let accel = '';
    if (hw.nvidia) {
      if (!pack.installed) {
        const busy = pack.task?.status === 'downloading';
        accel = `<div class="callout accent" style="margin-top:12px">${icon('zap')}<div style="flex:1"><b>Speed up transcription with your NVIDIA GPU</b>
          Downloads the NVIDIA libraries (about 1.3 GB) into the app folder. Usually 5-20× faster.
          ${pack.task?.status === 'error' ? `<div style="color:var(--bad);margin-top:4px">${esc(pack.task.error || '')}</div>` : ''}</div>
          <button class="btn primary sm" data-gpu ${busy ? 'disabled' : ''}>${busy ? 'Installing…' : `${icon('download', 14)} Enable GPU`}</button></div>`;
      } else if (st?.cuda_failed) {
        accel = `<div class="callout warn" style="margin-top:12px">${icon('alert')}<div style="flex:1"><b>The GPU didn't work last time, so the processor is being used.</b>${esc(st.cuda_failed).slice(0, 240)}</div><button class="btn sm" data-gpu-retry>Try GPU again</button></div>`;
      } else {
        accel = `<div class="callout good" style="margin-top:12px">${icon('zap')}<div><b>GPU acceleration is on</b>Transcription runs on your ${esc(gpu?.name || 'NVIDIA GPU')}.</div></div>`;
      }
    } else if (!hw.apple_silicon) {
      accel = `<div class="callout" style="margin-top:12px">${icon('info')}<div>No NVIDIA graphics card found - everything runs on the processor. That works fine, it just takes longer.</div></div>`;
    }
    return `<div class="card" style="margin-bottom:22px"><div class="card-head"><h3>${icon('cpu', 16)} This computer</h3><span class="spacer"></span>
        <button class="btn ghost sm" data-open-folder>${icon('folder', 14)} Models folder · ${fmtBytes(data.disk.models)}</button></div>
      <div class="hw-card">
        <div class="hw-item"><div class="ic">${icon('cpu')}</div><div><b>${hw.cores} cores</b><span>${esc(hw.cpu)}</span></div></div>
        <div class="hw-item"><div class="ic">${icon('layers')}</div><div><b>${hw.ram_gb} GB memory</b><span>RAM</span></div></div>
        <div class="hw-item"><div class="ic">${icon('zap')}</div><div><b>${gpu ? esc(gpu.name.replace('NVIDIA ', '')) : hw.apple_silicon ? 'Apple Silicon' : 'No NVIDIA GPU'}</b><span>${gpu ? `${hw.vram_gb} GB video memory` : hw.apple_silicon ? 'Metal acceleration for AI notes' : 'Processor only'}</span></div></div>
      </div>${accel}</div>`;
  }

  function whisperSection() {
    const cur = store.status?.whisper_model;
    return `<div class="tier-title"><h3>${icon('fileText', 16)} Speech recognition</h3><span class="faint small">Turns speech into text (OpenAI Whisper). Times are estimates for 1 hour of audio on this computer.</span></div>
      <div class="stack">${data.whisper.map((m) => {
        const using = m.key === cur;
        return `<div class="model-card ${using ? 'selected' : ''}">
          <div><h4>${esc(m.name)} ${m.recommended ? '<span class="badge accent">Recommended</span>' : ''} ${using ? '<span class="badge good">In use</span>' : ''} ${m.english_only ? '<span class="badge outline">English only</span>' : ''}</h4>
            <p>${esc(m.summary)}</p>
            <div class="specs"><span>Accuracy ${dots(m.accuracy)}</span><span>Speed ${dots(m.speed, 5, 'speed')}</span>
              <span>${icon('clock', 13)} ~${m.minutes_per_hour} min per hour${m.measured ? ' (measured)' : ''}</span><span>${icon('hardDrive', 13)} ${fmtBytes(m.size)}</span></div></div>
          <div class="actions">${actionButtons('whisper', m, using)}</div>${dl(m.download)}</div>`;
      }).join('')}</div>`;
  }

  function actionButtons(kind, m, using) {
    if (m.download && m.download.status !== 'error') return '';
    if (!m.installed) return `<button class="btn ${m.recommended ? 'primary' : ''} sm" data-download="${kind}:${m.key}">${icon('download', 14)} Download</button>`;
    return `${using ? '' : `<button class="btn sm" data-use="${kind}:${m.key}">Use this</button>`}
      <button class="btn ghost sm danger" data-delete="${kind}:${m.key}">${icon('trash', 13)} Delete</button>`;
  }

  function llmSection() {
    const s = store.settings;
    const cur = s.llm_model || data.recommend.llm;
    const engine = data.engine;
    const builtin = s.llm_backend === 'builtin';
    let engineCard = '';
    if (builtin) {
      const inst = engine.installed;
      engineCard = `<div class="model-card" style="margin-bottom:12px"><div>
          <h4>${icon('cpu', 16)} AI engine (llama.cpp) ${inst ? `<span class="badge good">Installed · ${esc(inst.backend.toUpperCase())}</span>` : '<span class="badge warn">Not installed</span>'}</h4>
          <p>Runs the AI models below. The ${esc(engine.preferred === 'cuda' ? 'NVIDIA CUDA' : engine.preferred === 'metal' ? 'Apple Metal' : engine.preferred === 'vulkan' ? 'GPU (Vulkan)' : 'CPU')} version suits this computer (about 20-600 MB).</p></div>
        <div class="actions">${engine.download ? '' : inst ? `<button class="btn ghost sm" data-engine>${icon('refresh', 13)} Update</button><button class="btn ghost sm" data-engine-cpu>CPU version</button>` : `<button class="btn primary sm" data-engine>${icon('download', 14)} Install</button>`}</div>
        ${dl(engine.download)}</div>`;
    } else {
      engineCard = `<div class="callout" style="margin-bottom:12px">${icon('info')}<div>Notes use <b>${s.llm_backend === 'ollama' ? 'Ollama' : 'an OpenAI-compatible local server'}</b> (change in Settings). Models below are only for the built-in engine.</div></div>`;
    }
    const byTier = {};
    for (const m of data.llm) (byTier[m.tier] ||= []).push(m);
    return `<div class="tier-title" style="margin-top:34px"><h3>${icon('sparkles', 16)} AI models for notes</h3><span class="faint small">Write the summary, decisions and action items. Everything runs locally.</span></div>
      ${engineCard}
      ${Object.entries(TIERS).map(([tier, [name, desc]]) => byTier[tier] ? `<div class="tier-title"><h3>${name}</h3><span class="faint small">${desc}</span></div>
        <div class="stack">${byTier[tier].map((m) => {
          const using = builtin && m.key === cur && m.installed;
          return `<div class="model-card ${using ? 'selected' : ''} ${m.fits ? '' : 'nofit'}">
            <div><h4>${esc(m.name)} <span class="faint small" style="font-weight:500">by ${esc(m.maker)}</span> ${m.recommended ? '<span class="badge accent">Recommended for you</span>' : ''} ${using ? '<span class="badge good">In use</span>' : ''} ${!m.fits ? '<span class="badge warn">May be too big for this PC</span>' : m.fits_gpu && data.hardware.nvidia ? '<span class="badge outline">Fits your GPU</span>' : ''}</h4>
              <p>${esc(m.summary)}</p>
              <div class="specs"><span>Quality ${dots(m.quality)}</span><span>Speed ${dots(m.speed, 5, 'speed')}</span><span>${icon('hardDrive', 13)} ${fmtBytes(m.size)}</span><span>${icon('layers', 13)} needs ~${m.ram_gb} GB memory</span></div></div>
            <div class="actions">${actionButtons('llm', m, using)}</div>${dl(m.download)}</div>`;
        }).join('')}</div>` : '').join('')}
      <div class="tier-title"><h3>Your own models</h3><span class="faint small">Any GGUF model from Hugging Face, or drop .gguf files into the models/llm folder.</span></div>
      <div class="stack">${data.custom_llm.map((m) => `<div class="model-card ${builtin && m.key === s.llm_model ? 'selected' : ''}"><div><h4>${esc(m.name)} ${builtin && m.key === s.llm_model ? '<span class="badge good">In use</span>' : ''}</h4><div class="specs"><span>${icon('hardDrive', 13)} ${fmtBytes(m.size)}</span></div></div>
          <div class="actions">${actionButtons('llm', m, builtin && m.key === s.llm_model)}</div></div>`).join('')}
        <div class="row"><button class="btn sm" data-custom>${icon('link', 14)} Download from a Hugging Face link</button></div></div>`;
  }

  function speakerSection() {
    const cur = store.settings.speaker_model;
    return `<div class="tier-title" style="margin-top:34px"><h3>${icon('users', 16)} Speaker recognition</h3><span class="faint small">Tells voices apart and recognises people you've named.</span></div>
      <div class="stack">${data.speaker.map((m) => {
        const using = m.key === cur;
        return `<div class="model-card ${using ? 'selected' : ''}"><div><h4>${esc(m.name)} ${m.recommended ? '<span class="badge accent">Recommended</span>' : ''} ${using ? '<span class="badge good">In use</span>' : ''}</h4><p>${esc(m.summary)}</p>
          <div class="specs"><span>Accuracy ${dots(m.accuracy)}</span><span>Speed ${dots(m.speed, 5, 'speed')}</span><span>${icon('hardDrive', 13)} ${fmtBytes(m.size + 6958444)}</span></div></div>
          <div class="actions">${actionButtons('speaker', m, using)}</div>${dl(m.download)}</div>`;
      }).join('')}</div>
      <div class="hint" style="margin-top:8px">Switching model re-learns your saved voice samples automatically.</div>`;
  }

  function draw() {
    if (!data) return;
    root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Everything runs on this computer</div><h1>AI models</h1>
      <p>Download once, use offline forever. Bigger models are more accurate but slower - pick what suits your computer.</p></div></div>
      ${hardwareCard()}${whisperSection()}${llmSection()}${speakerSection()}`;
  }

  root.addEventListener('click', async (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const [kind, ...rest] = (b.dataset.download || b.dataset.use || b.dataset.delete || '').split(':');
    const key = rest.join(':');
    try {
      if (b.dataset.download) {
        await api.post('/api/models/download', { kind, key });
        toast('Download started', 'good', 1800);
        kickPoll();
        // pick a sensible default if nothing is set yet
        if (kind === 'llm' && !store.settings.llm_model) await saveSettings({ llm_model: key });
        if (kind === 'llm' && !data.engine.installed && store.settings.llm_backend === 'builtin' && !data.engine.download) {
          await api.post('/api/models/download', { kind: 'engine' }).catch(errorToast);
        }
        await load();
      } else if (b.dataset.use) {
        const field = { whisper: 'whisper_model', llm: 'llm_model', speaker: 'speaker_model' }[kind];
        const changes = { [field]: key };
        if (kind === 'llm') changes.llm_backend = 'builtin';
        await saveSettings(changes);
        await loadStatus();
        toast('Model selected', 'good', 1500);
        draw();
      } else if (b.dataset.delete) {
        if (await confirmDialog('Delete this model?', 'It will be removed from this computer. You can download it again any time.', { ok: 'Delete', danger: true })) {
          await api.del(`/api/models/${kind}/${encodeURIComponent(key)}`);
          await loadStatus();
          await load();
        }
      } else if (b.dataset.cancel) {
        await api.post(`/api/downloads/${encodeURIComponent(b.dataset.cancel)}/cancel`);
        setTimeout(load, 500);
      } else if (b.dataset.engine !== undefined || b.dataset.engineCpu !== undefined) {
        await api.post('/api/models/download', { kind: 'engine', backend: b.dataset.engineCpu !== undefined ? 'cpu' : undefined });
        kickPoll();
        await load();
      } else if (b.dataset.gpu !== undefined) {
        await api.post('/api/gpu/install');
        toast('Installing GPU support - this can take a few minutes', 'good');
        const poll = setInterval(async () => {
          await load();
          if (data?.gpu_pack.task?.status !== 'downloading') {
            clearInterval(poll);
            await loadStatus();
            if (data.gpu_pack.installed) toast('GPU acceleration enabled', 'good');
          }
        }, 3000);
        await load();
      } else if (b.dataset.gpuRetry !== undefined) {
        await api.post('/api/gpu/retry');
        await loadStatus();
        draw();
      } else if (b.dataset.custom !== undefined) {
        const url = await promptDialog('Download a model from Hugging Face', {
          label: 'Link to a .gguf file', placeholder: 'https://huggingface.co/…/model-Q4_K_M.gguf', ok: 'Download',
          hint: 'Open the model’s “Files” tab on huggingface.co, click the .gguf file you want (Q4_K_M is a good size) and copy the link.',
        });
        if (url) { await api.post('/api/models/llm/url', { url }); kickPoll(); await load(); }
      } else if (b.dataset.openFolder !== undefined) {
        await api.post('/api/open-folder', { what: 'models' });
      }
    } catch (err) { errorToast(err); }
  });

  await load();
  const refresh = throttle(load, 1500);
  let wasActive = false;
  const unsub = subscribe((what) => {
    if (what !== 'jobs') return;
    const active = store.downloads.some((d) => ['queued', 'downloading', 'extracting'].includes(d.status));
    if (active || wasActive) refresh();
    if (wasActive && !active) loadStatus();
    wasActive = active;
  });
  return () => unsub();
}
