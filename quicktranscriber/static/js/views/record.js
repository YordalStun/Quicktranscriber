// Recorder: microphone and/or computer audio, streamed to disk in chunks.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtTime } from '../util.js';
import { toast, errorToast, modal, confirmDialog } from '../ui.js';
import { store, kickPoll } from '../store.js';

let session = null; // survives navigating away while recording

function pickMime() {
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/mp4;codecs=mp4a.40.2'];
  return types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
}

export async function render(root) {
  root.innerHTML = `<div class="recorder">
    <div class="page-head" style="justify-content:center;text-align:center;flex-direction:column;align-items:center;margin-bottom:0">
      <div class="eyebrow">Recorder</div><h1>Record a meeting</h1>
      <p>Audio is saved to this computer every few seconds, so even very long meetings are safe.</p></div>
    <div class="rec-stage"><div class="rec-ring" id="ring1"></div><div class="rec-ring r2" id="ring2"></div>
      <button class="rec-btn" id="recbtn" title="Start recording"><span class="core"></span></button></div>
    <div class="rec-time" id="rtime">0:00</div>
    <div class="rec-status" id="rstatus">Ready when you are</div>
    <canvas class="rec-wave" id="rwave"></canvas>
    <div class="btn-row" style="justify-content:center" id="rcontrols"></div>
    <div class="marks" id="rmarks"></div>
    <div class="rec-options" id="ropts">
      <div class="card"><div class="field" style="margin:0"><label>Microphone</label><select class="select" id="mic"><option value="">Default microphone</option></select>
        <div class="level-meter" style="margin-top:10px"><i id="level"></i></div><div class="hint" style="margin-top:6px">Speak to test the level.</div></div></div>
      <div class="card"><div class="row"><div style="flex:1"><div class="label">Also record computer audio</div>
        <div class="hint">For Teams, Zoom or Meet calls: captures the other people too. You'll be asked which screen or tab to share - tick <b>“Share audio”</b>.</div></div>
        <label class="switch"><input type="checkbox" id="sysaudio"><span class="track"></span></label></div></div>
    </div>
  </div>`;

  const btn = root.querySelector('#recbtn');
  const canvas = root.querySelector('#rwave');
  const g = canvas.getContext('2d');
  const levelEl = root.querySelector('#level');
  const timeEl = root.querySelector('#rtime');
  const statusEl = root.querySelector('#rstatus');
  const controls = root.querySelector('#rcontrols');
  const micSel = root.querySelector('#mic');
  const sysChk = root.querySelector('#sysaudio');
  let preview = null; // mic preview when idle
  let raf = null;
  const history = [];

  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    statusEl.innerHTML = `<span style="color:var(--bad)">This browser can't record audio. Use Microsoft Edge or Google Chrome.</span>`;
    btn.disabled = true;
    return;
  }

  async function listMics() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const mics = devices.filter((d) => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications');
      const saved = localStorage.getItem('qt-mic') || '';
      micSel.innerHTML = '<option value="">Default microphone</option>' + mics.map((d, i) => `<option value="${esc(d.deviceId)}" ${d.deviceId === saved ? 'selected' : ''}>${esc(d.label || `Microphone ${i + 1}`)}</option>`).join('');
    } catch { /* ignore */ }
  }

  async function startPreview() {
    stopPreview();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints(false) });
      const ac = new AudioContext();
      const an = ac.createAnalyser();
      an.fftSize = 2048;
      ac.createMediaStreamSource(stream).connect(an);
      preview = { stream, ac, an };
      await listMics();
    } catch (e) {
      statusEl.innerHTML = `<span style="color:var(--warn)">${icon('alert', 14)} Microphone access was blocked. Allow it in the browser's address bar, then reload.</span>`;
    }
  }

  function stopPreview() {
    if (!preview) return;
    preview.stream.getTracks().forEach((t) => t.stop());
    preview.ac.close();
    preview = null;
  }

  function micConstraints(withSystem) {
    const id = micSel.value;
    return {
      deviceId: id ? { exact: id } : undefined,
      echoCancellation: withSystem,
      noiseSuppression: false,
      autoGainControl: true,
      channelCount: 1,
    };
  }

  // ---------- drawing ----------
  function draw() {
    const an = session?.an || preview?.an;
    const dpr = devicePixelRatio || 1;
    const W = canvas.clientWidth * dpr, H = canvas.clientHeight * dpr;
    if (canvas.width !== W) { canvas.width = W; canvas.height = H; }
    let level = 0;
    if (an) {
      const buf = new Float32Array(an.fftSize);
      an.getFloatTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v));
      level = Math.min(1, Math.pow(peak, 0.6) * 1.2);
    }
    levelEl.style.width = `${Math.round(level * 100)}%`;
    const pulse = session?.paused ? 0 : level; // rings stay still while paused
    root.querySelector('#ring1').style.transform = `scale(${1 + pulse * 0.18})`;
    root.querySelector('#ring2').style.transform = `scale(${1 + pulse * 0.3})`;
    if (session && !session.paused) { history.push(level); if (history.length > 600) history.shift(); }
    else if (!session) { history.push(level * 0.6); if (history.length > 600) history.shift(); }
    g.clearRect(0, 0, W, H);
    const styles = getComputedStyle(document.documentElement);
    const barW = 3 * dpr, gap = 2 * dpr;
    const n = Math.floor(W / (barW + gap));
    const data = history.slice(-n);
    const grad = g.createLinearGradient(0, 0, W, 0);
    grad.addColorStop(0, styles.getPropertyValue('--accent').trim() || '#8b5cf6');
    grad.addColorStop(1, styles.getPropertyValue('--accent-2').trim() || '#22d3ee');
    g.fillStyle = session ? grad : (styles.getPropertyValue('--border-strong').trim() || '#444');
    data.forEach((v, i) => {
      const h = Math.max(2 * dpr, v * H * 0.85);
      g.fillRect(W - (data.length - i) * (barW + gap), (H - h) / 2, barW, h);
    });
    if (session) {
      const t = session.elapsed();
      timeEl.textContent = fmtTime(t);
    }
    raf = requestAnimationFrame(draw);
  }

  // ---------- recording ----------
  async function start() {
    stopPreview();
    const withSystem = sysChk.checked;
    let mic, display;
    try {
      mic = await navigator.mediaDevices.getUserMedia({ audio: micConstraints(withSystem) });
      if (withSystem) {
        display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }, systemAudio: 'include' });
        if (!display.getAudioTracks().length) {
          display.getTracks().forEach((t) => t.stop());
          mic.getTracks().forEach((t) => t.stop());
          toast('No computer audio was shared - tick “Share audio” when choosing what to share.', 'warn', 7000);
          startPreview();
          return;
        }
        display.getVideoTracks().forEach((t) => t.stop()); // we only need the sound
      }
    } catch (e) {
      mic?.getTracks().forEach((t) => t.stop());
      errorToast(e.name === 'NotAllowedError' ? new Error('Permission was denied.') : e);
      startPreview();
      return;
    }
    try { localStorage.setItem('qt-mic', micSel.value); } catch { /* ignore */ }
    const ac = new AudioContext();
    const dest = ac.createMediaStreamDestination();
    const an = ac.createAnalyser();
    an.fftSize = 2048;
    const micSrc = ac.createMediaStreamSource(mic);
    micSrc.connect(dest);
    micSrc.connect(an);
    if (display) {
      const sys = ac.createMediaStreamSource(new MediaStream(display.getAudioTracks()));
      sys.connect(dest);
      sys.connect(an);
    }
    const mime = pickMime();
    let rec;
    try {
      rec = new MediaRecorder(dest.stream, mime ? { mimeType: mime, audioBitsPerSecond: 64000 } : undefined);
    } catch (e) { errorToast(e); return; }
    let rid;
    try { rid = (await api.post('/api/recordings', { mime: rec.mimeType || mime })).id; } catch (e) { errorToast(e); return; }

    session = {
      id: rid, rec, ac, an, mic, display, mime: rec.mimeType || mime,
      startedAt: Date.now(), pausedTotal: 0, pausedAt: null, paused: false,
      seq: 0, queue: [], uploading: false, uploaded: 0, failed: 0, bookmarks: [],
      elapsed() { return ((this.paused ? this.pausedAt : Date.now()) - this.startedAt - this.pausedTotal) / 1000; },
    };
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size) { session.queue.push({ seq: session.seq++, blob: e.data }); pump(); }
    };
    rec.start(4000);
    try { session.wake = await navigator.wakeLock?.request('screen'); } catch { /* not supported */ }
    // stop if the user stops sharing
    display?.getAudioTracks()[0]?.addEventListener('ended', () => session && toast('Computer audio sharing stopped - still recording the microphone', 'warn', 6000));
    window.addEventListener('beforeunload', warnUnload);
    updateUI();
  }

  async function pump() {
    if (!session || session.uploading) return;
    session.uploading = true;
    while (session && session.queue.length) {
      const item = session.queue[0];
      try {
        await fetch(`/api/recordings/${session.id}/chunk?seq=${item.seq}`, { method: 'POST', headers: { 'X-QT': '1' }, body: item.blob })
          .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); });
        session.uploaded += item.blob.size;
        session.queue.shift();
        session.failed = 0;
      } catch {
        session.failed++;
        await new Promise((r) => setTimeout(r, Math.min(10000, 1000 * session.failed)));
      }
    }
    if (session) session.uploading = false;
    updateStatus();
  }

  function warnUnload(e) { e.preventDefault(); e.returnValue = 'A recording is in progress.'; }

  function togglePause() {
    if (!session) return;
    if (session.paused) {
      session.rec.resume();
      session.pausedTotal += Date.now() - session.pausedAt;
      session.paused = false;
    } else {
      session.rec.pause();
      session.pausedAt = Date.now();
      session.paused = true;
    }
    updateUI();
  }

  function bookmark() {
    if (!session) return;
    const t = Math.round(session.elapsed());
    session.bookmarks.push(t);
    root.querySelector('#rmarks').insertAdjacentHTML('beforeend', `<span class="badge warn">${icon('bookmark', 11)} ${fmtTime(t)}</span>`);
    toast(`Bookmark at ${fmtTime(t)}`, 'good', 1500);
  }

  async function stop(discard = false) {
    if (!session) return;
    const s = session;
    await new Promise((resolve) => {
      s.rec.onstop = resolve;
      try { s.rec.stop(); } catch { resolve(); }
    });
    s.mic.getTracks().forEach((t) => t.stop());
    s.display?.getTracks().forEach((t) => t.stop());
    s.ac.close();
    s.wake?.release?.().catch(() => {});
    window.removeEventListener('beforeunload', warnUnload);
    statusEl.textContent = 'Saving the last seconds…';
    while (s.queue.length || s.uploading) { await pump(); await new Promise((r) => setTimeout(r, 300)); }
    const length = s.elapsed();
    session = null;
    updateUI();
    if (discard) {
      await api.del(`/api/recordings/${s.id}`).catch(() => {});
      toast('Recording discarded');
      startPreview();
      return;
    }
    await finishDialog(s, length);
  }

  async function finishDialog(s, length) {
    let models = null;
    try { models = await api.get('/api/models'); } catch { /* use defaults */ }
    const { optionsForm, bindOptionsForm, readOptions } = await import('./importer.js');
    const res = await modal({
      title: 'Recording saved', subtitle: `${fmtTime(length)} recorded. Now let's turn it into notes.`, icon: 'check', wide: true, dismissable: false,
      body: `<div class="field"><label>Title <span class="faint">(optional - AI will suggest one)</span></label><input class="input" name="title" placeholder="e.g. Weekly team meeting"></div>${models ? optionsForm(models, { duration: length }) : ''}`,
      onOpen: (m) => models && bindOptionsForm(m, models),
      actions: [
        { label: 'Delete recording', kind: 'ghost danger', left: true, onClick: async () => (await confirmDialog('Delete this recording?', 'It can’t be recovered.', { ok: 'Delete', danger: true })) ? 'discard' : false },
        { label: 'Process later', kind: 'ghost', value: 'later' },
        { label: 'Transcribe now', kind: 'primary', icon: 'sparkles', onClick: (m) => ({ title: m.querySelector('[name=title]').value.trim(), options: models ? readOptions(m) : {} }) },
      ],
    });
    if (res === 'discard') {
      await api.del(`/api/recordings/${s.id}`).catch(() => {});
      toast('Recording deleted');
      startPreview();
      return;
    }
    if (res === 'later') {
      toast('Saved. You can process it from the Meetings page.', 'good');
      location.hash = '#/';
      return;
    }
    try {
      const options = { ...(res?.options || {}), bookmarks: s.bookmarks };
      const r = await api.post(`/api/recordings/${s.id}/finish`, { title: res?.title || '', options });
      kickPoll();
      const { askNotificationPermission } = await import('../app.js');
      askNotificationPermission();
      location.hash = `#/m/${r.id}`;
    } catch (e) { errorToast(e); }
  }

  function updateStatus() {
    if (!session) return;
    const mb = (session.uploaded / 1e6).toFixed(1);
    const pending = session.queue.length;
    statusEl.innerHTML = `${session.paused ? `${icon('pause', 14)} Paused` : '<span class="rec-live"></span> Recording'}${session.display ? ` · ${icon('monitor', 14)} mic + computer audio` : ` · ${icon('mic', 14)} microphone`}
      · <span title="Saved to disk as you record">${pending > 1 ? `${icon('alert', 13)} saving…` : `${icon('check', 13)} ${mb} MB saved`}</span>`;
  }

  function updateUI() {
    const rec = !!session;
    btn.classList.toggle('recording', rec);
    btn.classList.toggle('paused', rec && session.paused);
    root.querySelector('.rec-stage').classList.toggle('paused', rec && session.paused);
    btn.title = rec ? 'Stop and save' : 'Start recording';
    root.querySelector('#ropts').hidden = rec;
    if (rec) {
      controls.innerHTML = `<button class="btn" id="pause">${icon(session.paused ? 'play' : 'pause', 16)} ${session.paused ? 'Resume' : 'Pause'}</button>
        <button class="btn" id="mark" title="Bookmark this moment (B)">${icon('bookmark', 16)} Bookmark</button>
        <button class="btn danger" id="discard">${icon('trash', 16)} Discard</button>`;
      controls.querySelector('#pause').onclick = togglePause;
      controls.querySelector('#mark').onclick = bookmark;
      controls.querySelector('#discard').onclick = async () => {
        if (await confirmDialog('Discard this recording?', 'Everything recorded so far will be deleted.', { ok: 'Discard', danger: true })) stop(true);
      };
      updateStatus();
    } else {
      controls.innerHTML = '';
      root.querySelector('#rmarks').innerHTML = '';
      timeEl.textContent = '0:00';
      statusEl.textContent = 'Ready when you are';
    }
  }

  btn.onclick = () => (session ? stop(false) : start());
  micSel.onchange = () => { if (!session) startPreview(); };
  const onKey = (e) => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
    if (e.key === 'b' && session) bookmark();
  };
  window.addEventListener('keydown', onKey);

  if (session) updateUI(); else startPreview();
  raf = requestAnimationFrame(draw);
  const statusTimer = setInterval(updateStatus, 1000);

  return () => {
    cancelAnimationFrame(raf);
    clearInterval(statusTimer);
    window.removeEventListener('keydown', onKey);
    stopPreview();
    if (session) toast('Still recording in the background - go back to Record to stop.', 'warn', 6000);
  };
}

export function isRecording() { return !!session; }
