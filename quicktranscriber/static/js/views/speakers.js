// Speaker library: the voices QuickTranscriber has learned, and how to improve them.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtDate, fmtDuration } from '../util.js';
import { avatar, modal, toast, errorToast, confirmDialog, promptDialog, playClip, menu } from '../ui.js';

const STRENGTH = { none: 'No voice samples yet', weak: 'Weak - add more samples', good: 'Good', strong: 'Strong' };

export async function render(root, { args }) {
  let data;
  async function load() {
    try { data = await api.get('/api/speakers'); } catch (e) { errorToast(e); data = { speakers: [] }; }
  }
  await load();

  function draw() {
    const people = data.speakers;
    root.innerHTML = `
      <div class="page-head"><div><div class="eyebrow">Voice recognition</div><h1>Speakers</h1>
        <p>People QuickTranscriber recognises by voice. It learns every time you confirm who's speaking.</p></div>
        <div class="spacer"></div><button class="btn primary" id="add">${icon('plus', 16)} Add person</button></div>
      ${!data.models_installed ? `<div class="callout warn" style="margin-bottom:16px">${icon('alert')}<div><b>Speaker recognition model not downloaded</b>It downloads automatically the first time you process a meeting, or <a href="#/models">get it now</a>.</div></div>` : ''}
      <div class="callout" style="margin-bottom:20px">${icon('info')}<div><b>How learning works</b>
        After a meeting, open the <i>Speakers</i> tab and name each voice (or confirm suggestions). A few clean clips of their voice are saved here.
        Next time they speak, they're named automatically. If recognition goes wrong, open the person and remove clips that aren't them - or clips marked ⚠ that don't sound like the rest.</div></div>
      ${people.length ? `<div class="people-grid">${people.map((p) => `<div class="person" data-id="${p.id}">
          <div class="top">${avatar(p.name, p.color, 'lg')}<div style="min-width:0;flex:1"><h3>${esc(p.name)}</h3>
            <div class="small faint">${p.meetings} meeting${p.meetings === 1 ? '' : 's'} · ${p.active_samples} sample${p.active_samples === 1 ? '' : 's'} · ${fmtDuration(p.seconds)}</div></div></div>
          <div class="strength ${p.strength}"><i></i><i></i><i></i></div>
          <div class="small" style="margin-top:6px;color:${p.strength === 'weak' ? 'var(--warn)' : p.strength === 'strong' ? 'var(--good)' : 'var(--text-3)'}">Voiceprint: ${STRENGTH[p.strength]}</div>
        </div>`).join('')}</div>`
        : `<div class="empty"><div class="big-icon">${icon('users', 34)}</div><h2>No one here yet</h2><p>Name the speakers in any meeting and they'll appear here.<br>You can also add someone and record a short voice sample.</p></div>`}`;
    root.querySelector('#add').onclick = addPerson;
    root.querySelectorAll('.person').forEach((c) => c.addEventListener('click', () => openPerson(+c.dataset.id)));
  }

  async function addPerson() {
    const name = await promptDialog('Add a person', { label: 'Name', placeholder: 'e.g. Rhian Davies', ok: 'Add' });
    if (!name) return;
    try {
      const r = await api.post('/api/speakers', { name });
      await load();
      draw();
      openPerson(r.id, true);
    } catch (e) { errorToast(e); }
  }

  async function openPerson(id, suggestEnroll = false) {
    const p = data.speakers.find((x) => x.id === id);
    if (!p) return;
    let samples = [], meetings = [];
    try {
      [samples, meetings] = await Promise.all([api.get(`/api/speakers/${id}/samples`), api.get(`/api/speakers/${id}/meetings`)]);
    } catch (e) { errorToast(e); }
    const outliers = samples.filter((s) => s.outlier).length;
    const inactive = samples.filter((s) => !s.active).length;
    const body = `
      <div class="row" style="margin-bottom:16px">${avatar(p.name, p.color, 'lg')}<div style="flex:1"><div style="font-weight:700;font-size:17px">${esc(p.name)}</div>
        <div class="small faint">Voiceprint: ${STRENGTH[p.strength]}${p.consistency != null ? ` · consistency ${Math.round(p.consistency * 100)}%` : ''}</div></div>
        <input type="color" value="${esc(p.color)}" data-color title="Colour" style="width:36px;height:36px;border:0;background:none;cursor:pointer"></div>
      ${outliers ? `<div class="callout warn" style="margin-bottom:12px">${icon('alert')}<div style="flex:1"><b>${outliers} clip${outliers > 1 ? 's' : ''} may not be ${esc(p.name)}</b>They sound different from the others. Listen, then remove the wrong ones.</div><button class="btn sm" data-cleanup>Remove all ${outliers}</button></div>` : ''}
      ${inactive ? `<div class="callout" style="margin-bottom:12px">${icon('info')}<div>${inactive} clip(s) were learned with a different voice model and are being converted.</div></div>` : ''}
      ${suggestEnroll || !samples.length ? `<div class="callout accent" style="margin-bottom:12px">${icon('mic')}<div style="flex:1"><b>Teach ${esc(p.name)}'s voice</b>Record them reading for about 20 seconds - or just name them in a meeting.</div><button class="btn sm primary" data-enroll>${icon('mic', 14)} Record sample</button></div>` : ''}
      <div class="row" style="margin:6px 0 8px"><div class="eyebrow" style="flex:1">Voice samples (${samples.length})</div>${samples.length ? `<button class="btn ghost sm" data-enroll>${icon('mic', 14)} Add sample</button>` : ''}</div>
      <div data-samples>${samples.map((s) => `<div class="sample ${s.outlier ? 'outlier' : ''} ${s.active ? '' : 'inactive'}" data-sid="${s.id}">
          ${s.has_clip ? `<button class="mini-play" data-play="${s.id}">${icon('play', 14)}</button>` : `<span></span>`}
          <div style="min-width:0"><div class="small" style="font-weight:600">${s.source === 'enroll' ? 'Recorded sample' : s.source === 'correction' ? 'From a correction' : esc(s.meeting_title || 'Deleted meeting')}
            ${s.outlier ? `<span class="badge warn">${icon('alert', 11)} doesn't sound like the others</span>` : ''}</div>
            <div class="faint small">${fmtDuration(s.duration)} · ${esc(fmtDate(s.created_at))}${s.score != null ? ` · match ${Math.round(s.score * 100)}%` : ''}</div></div>
          <button class="btn ghost sm icon-only" data-del="${s.id}" title="Remove this sample">${icon('trash', 14)}</button></div>`).join('') || '<div class="faint small">No samples yet.</div>'}</div>
      ${meetings.length ? `<div class="eyebrow" style="margin:16px 0 8px">Meetings</div>${meetings.slice(0, 12).map((m) => `<a class="row small" style="padding:6px 8px;border-radius:8px;color:var(--text)" href="#/m/${esc(m.id)}" data-nav>${icon('wave', 14)}<span style="flex:1">${esc(m.title)}</span><span class="faint">${esc(fmtDate(m.recorded_at))}</span></a>`).join('')}` : ''}`;
    await modal({
      title: 'Speaker', wide: true, body,
      onOpen: (m, close) => {
        m.querySelectorAll('[data-nav]').forEach((a) => a.addEventListener('click', () => close()));
        m.querySelector('[data-color]').addEventListener('change', async (e) => {
          await api.patch(`/api/speakers/${id}`, { color: e.target.value });
          await load(); draw();
        });
        m.addEventListener('click', async (e) => {
          const b = e.target.closest('button');
          if (!b) return;
          try {
            if (b.dataset.play) playClip(`/api/samples/${b.dataset.play}/audio`, b);
            else if (b.dataset.del) {
              await api.del(`/api/samples/${b.dataset.del}`);
              b.closest('.sample').remove();
              toast('Sample removed', 'good', 1500);
              await load(); draw();
            } else if (b.dataset.cleanup !== undefined) {
              const r = await api.post(`/api/speakers/${id}/cleanup`);
              toast(`Removed ${r.removed} sample(s)`, 'good');
              close(); await load(); draw(); openPerson(id);
            } else if (b.dataset.enroll !== undefined) {
              close();
              await enroll(p);
              await load(); draw(); openPerson(id);
            }
          } catch (err) { errorToast(err); }
        });
      },
      actions: [
        { label: 'More', icon: 'more', kind: 'ghost', left: true, onClick: (m, btn) => { personMenu(btn, p); return false; } },
        { label: 'Rename', kind: 'ghost', icon: 'edit', onClick: async () => {
          const name = await promptDialog('Rename', { value: p.name, label: 'Name' });
          if (name) { try { await api.patch(`/api/speakers/${id}`, { name }); await load(); draw(); } catch (e) { errorToast(e); } }
        } },
        { label: 'Done', kind: 'primary' },
      ],
    });
  }

  function personMenu(anchor, p) {
    const others = data.speakers.filter((x) => x.id !== p.id);
    menu(anchor, [
      others.length ? { header: `Merge ${p.name} into…` } : null,
      ...others.map((o) => ({ label: o.name, color: o.color, onClick: async () => {
        if (await confirmDialog(`Merge into ${o.name}?`, `All of ${esc(p.name)}'s voice samples and meetings move to ${esc(o.name)}.`, { ok: 'Merge' })) {
          await api.post(`/api/speakers/${p.id}/merge`, { into: o.id });
          document.querySelector('.overlay')?.remove();
          await load(); draw();
          toast('Merged', 'good');
        }
      } })),
      { sep: true },
      { label: `Delete ${p.name}`, icon: 'trash', danger: true, onClick: async () => {
        if (await confirmDialog(`Delete ${p.name}?`, 'Their voice samples are deleted. Meetings keep the name as plain text.', { ok: 'Delete', danger: true })) {
          await api.del(`/api/speakers/${p.id}`);
          document.querySelector('.overlay')?.remove();
          await load(); draw();
          toast('Deleted');
        }
      } },
    ]);
  }

  draw();
  if (args[0]) openPerson(+args[0]);
}

// Record a voice sample for one person (about 20 seconds of reading)
const READ_TEXT = 'The quick brown fox jumps over the lazy dog. I am recording this sample so that my voice can be recognised in meetings. We usually talk about plans, progress, problems and next steps, and we try to agree who does what and by when. Numbers like fifteen, forty two and one hundred are common too.';

export async function enroll(p) {
  let stream, rec, chunks = [], started, timer;
  const body = `<p class="muted">Ask ${esc(p.name)} to read this aloud at a normal pace (about 20 seconds):</p>
    <div class="card" style="font-size:15.5px;line-height:1.7;margin-bottom:14px">${READ_TEXT}</div>
    <div class="row"><button class="btn primary" data-go>${icon('mic', 16)} Start</button><span class="rec-time" style="font-size:26px" data-t>0:00</span><div class="level-meter" style="flex:1"><i data-level></i></div></div>`;
  return modal({
    title: `Voice sample for ${p.name}`, icon: 'mic', wide: true, body,
    onOpen: (m) => {
      const go = m.querySelector('[data-go]');
      go.onclick = async () => {
        if (rec) { rec.stop(); return; }
        try {
          stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false } });
        } catch { toast('Microphone permission denied', 'bad'); return; }
        const ac = new AudioContext();
        const an = ac.createAnalyser();
        ac.createMediaStreamSource(stream).connect(an);
        rec = new MediaRecorder(stream);
        rec.ondataavailable = (e) => chunks.push(e.data);
        rec.onstop = async () => {
          clearInterval(timer);
          stream.getTracks().forEach((t) => t.stop());
          ac.close();
          go.disabled = true;
          go.textContent = 'Learning voice…';
          const fd = new FormData();
          fd.append('file', new Blob(chunks, { type: rec.mimeType }), 'sample.webm');
          try {
            const r = await api.upload(`/api/speakers/${p.id}/enroll`, fd);
            toast(`Learned ${r.added} sample(s) of ${p.name}'s voice`, 'good');
            m.closest('.overlay').querySelector('.modal-foot .btn.primary')?.click();
          } catch (e) { errorToast(e); go.disabled = false; go.textContent = 'Try again'; rec = null; chunks = []; }
        };
        rec.start();
        started = Date.now();
        go.innerHTML = `${icon('stop', 16)} Stop`;
        timer = setInterval(() => {
          const s = (Date.now() - started) / 1000;
          m.querySelector('[data-t]').textContent = `0:${String(Math.floor(s)).padStart(2, '0')}`;
          const buf = new Float32Array(an.fftSize);
          an.getFloatTimeDomainData(buf);
          m.querySelector('[data-level]').style.width = `${Math.min(100, Math.max(...buf.map(Math.abs)) * 160)}%`;
          if (s > 45) rec.stop();
        }, 100);
      };
    },
    actions: [{ label: 'Close', kind: 'primary', onClick: () => { if (rec?.state === 'recording') { rec.onstop = null; rec.stop(); stream?.getTracks().forEach((t) => t.stop()); clearInterval(timer); } } }],
  });
}
