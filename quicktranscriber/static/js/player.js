// Audio player with a speaker-coloured waveform.
import { icon } from './icons.js';
import { fmtTime, esc } from './util.js';

const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];

export class Player {
  constructor(container, { src, duration = 0, peaksUrl, onTime }) {
    this.container = container;
    this.duration = duration;
    this.onTime = onTime;
    this.peaks = [];
    this.perSecond = 10;
    this.colors = null; // per-bar colour
    this.markers = []; // {t, color, label}
    this.audio = new Audio();
    this.audio.preload = 'metadata';
    this.audio.src = src;
    this.hoverX = null;
    this.raf = null;
    container.innerHTML = `
      <div class="controls">
        <button class="btn ghost icon-only" data-back title="Back 15 s (J)">${icon('back15', 20)}</button>
        <button class="play-btn" data-play title="Play / pause (Space)">${icon('play', 20)}</button>
        <button class="btn ghost icon-only" data-fwd title="Forward 15 s (L)">${icon('fwd15', 20)}</button>
      </div>
      <div class="wave-wrap" data-wave><canvas></canvas><div class="wave-tip" hidden></div></div>
      <div class="row">
        <div class="time" data-time>0:00 / ${fmtTime(duration)}</div>
        <button class="btn ghost sm speed-btn" data-speed title="Playback speed">1×</button>
      </div>`;
    this.canvas = container.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.tip = container.querySelector('.wave-tip');
    this.playBtn = container.querySelector('[data-play]');
    this.timeEl = container.querySelector('[data-time]');
    this.speedBtn = container.querySelector('[data-speed]');

    this.playBtn.onclick = () => this.toggle();
    container.querySelector('[data-back]').onclick = () => this.skip(-15);
    container.querySelector('[data-fwd]').onclick = () => this.skip(15);
    this.speedBtn.onclick = () => {
      const i = (SPEEDS.indexOf(this.audio.playbackRate) + 1) % SPEEDS.length;
      this.setSpeed(SPEEDS[i]);
    };
    const wave = container.querySelector('[data-wave]');
    wave.addEventListener('mousemove', (e) => {
      const r = wave.getBoundingClientRect();
      this.hoverX = e.clientX - r.left;
      const t = (this.hoverX / r.width) * this.duration;
      const mk = this.markers.find((m) => Math.abs((m.t / this.duration) * r.width - this.hoverX) < 5);
      this.tip.hidden = false;
      this.tip.style.left = `${this.hoverX}px`;
      this.tip.textContent = mk?.label ? `${fmtTime(t)} · ${mk.label}` : fmtTime(t);
      this.draw();
    });
    wave.addEventListener('mouseleave', () => { this.hoverX = null; this.tip.hidden = true; this.draw(); });
    wave.addEventListener('mousedown', (e) => {
      const r = wave.getBoundingClientRect();
      const seekTo = (x) => this.seek(Math.max(0, Math.min(1, (x - r.left) / r.width)) * this.duration);
      seekTo(e.clientX);
      const move = (ev) => seekTo(ev.clientX);
      const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    });
    this.audio.addEventListener('play', () => { this.playBtn.innerHTML = icon('pause', 20); this.loop(); });
    this.audio.addEventListener('pause', () => { this.playBtn.innerHTML = icon('play', 20); cancelAnimationFrame(this.raf); this.tick(); });
    this.audio.addEventListener('loadedmetadata', () => { if (isFinite(this.audio.duration)) this.duration = this.audio.duration || this.duration; this.tick(); });
    this.audio.addEventListener('seeked', () => this.tick());
    this.audio.addEventListener('ended', () => this.tick());
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(wave);
    try {
      const saved = parseFloat(localStorage.getItem('qt-speed'));
      if (SPEEDS.includes(saved)) this.setSpeed(saved);
    } catch { /* ignore */ }
    if (peaksUrl) {
      fetch(peaksUrl).then((r) => (r.ok ? r.json() : null)).then((d) => {
        if (d) { this.peaks = d.peaks; this.perSecond = d.per_second; this.resize(); }
      }).catch(() => {});
    }
  }

  setSpeed(rate) {
    this.audio.playbackRate = rate;
    this.speedBtn.textContent = `${rate}×`;
    try { localStorage.setItem('qt-speed', String(rate)); } catch { /* ignore */ }
  }

  get time() { return this.audio.currentTime || 0; }
  get playing() { return !this.audio.paused; }

  toggle() {
    if (this.audio.paused) this.audio.play().catch(() => {});
    else this.audio.pause();
  }

  play() { this.audio.play().catch(() => {}); }
  pause() { this.audio.pause(); }

  seek(t, play = false) {
    this.audio.currentTime = Math.max(0, Math.min(t, this.duration || t));
    this.tick();
    if (play) this.play();
  }

  skip(d) { this.seek(this.time + d); }

  // colour each waveform bar by the speaker talking at that moment
  setSpeakerColors(segments, colorOf) {
    this.segments = segments;
    this.colorOf = colorOf;
    this.buildColors();
    this.draw();
  }

  setMarkers(markers) {
    this.markers = markers;
    this.draw();
  }

  buildColors() {
    const w = this.canvas.width;
    if (!w || !this.barW || !this.segments) return;
    const bars = Math.floor(w / (this.barW + this.gap));
    const out = new Array(bars).fill(null);
    const segs = this.segments;
    let j = 0;
    for (let i = 0; i < bars; i++) {
      const t = ((i + 0.5) / bars) * this.duration;
      while (j < segs.length - 1 && segs[j].end < t) j++;
      const s = segs[j];
      if (s && t >= s.start - 0.5 && t <= s.end + 0.5) out[i] = this.colorOf(s.speaker);
    }
    this.colors = out;
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = this.canvas.getBoundingClientRect();
    if (!r.width) return;
    this.canvas.width = Math.round(r.width * dpr);
    this.canvas.height = Math.round(r.height * dpr);
    this.dpr = dpr;
    this.barW = Math.max(2, Math.round(2 * dpr));
    this.gap = Math.max(1, Math.round(1 * dpr));
    this.buildColors();
    this.draw();
  }

  draw() {
    const { ctx, canvas } = this;
    const W = canvas.width, H = canvas.height;
    if (!W) return;
    ctx.clearRect(0, 0, W, H);
    const styles = getComputedStyle(document.documentElement);
    const dim = styles.getPropertyValue('--border-strong').trim() || '#343a50';
    const accent = styles.getPropertyValue('--accent').trim() || '#8b5cf6';
    const step = this.barW + this.gap;
    const bars = Math.floor(W / step);
    const progress = this.duration ? this.time / this.duration : 0;
    const peaks = this.peaks;
    const mid = H * 0.55;
    for (let i = 0; i < bars; i++) {
      let v = 0.04;
      if (peaks.length) {
        const a = Math.floor((i / bars) * peaks.length), b = Math.max(a + 1, Math.floor(((i + 1) / bars) * peaks.length));
        let mx = 0;
        for (let k = a; k < b && k < peaks.length; k++) mx = Math.max(mx, peaks[k]);
        v = Math.max(0.04, mx / 100);
      }
      const h = v * (H * 0.82);
      const played = i / bars < progress;
      const color = this.colors?.[i];
      ctx.fillStyle = color || (played ? accent : dim);
      const alpha = played ? 1 : color ? 0.36 : 0.8;
      const x = i * step;
      ctx.globalAlpha = alpha;
      ctx.fillRect(x, mid - h * 0.62, this.barW, h * 0.62);
      ctx.globalAlpha = alpha * 0.5;
      ctx.fillRect(x, mid + this.gap, this.barW, h * 0.38);
    }
    ctx.globalAlpha = 1;
    // markers (chapters, bookmarks)
    for (const m of this.markers) {
      const x = (m.t / (this.duration || 1)) * W;
      ctx.fillStyle = m.color || '#fbbf24';
      ctx.fillRect(x - this.dpr * 0.5, 0, this.dpr * 1.5, H * 0.14);
      ctx.beginPath();
      ctx.arc(x + this.dpr * 0.25, H * 0.14, 2.5 * this.dpr, 0, Math.PI * 2);
      ctx.fill();
    }
    // playhead
    const px = progress * W;
    ctx.fillStyle = styles.getPropertyValue('--text').trim() || '#fff';
    ctx.fillRect(px - this.dpr, 0, 2 * this.dpr, H);
    if (this.hoverX != null) {
      ctx.globalAlpha = 0.5;
      ctx.fillRect(this.hoverX * this.dpr - this.dpr * 0.5, 0, this.dpr, H);
      ctx.globalAlpha = 1;
    }
  }

  tick() {
    this.timeEl.textContent = `${fmtTime(this.time)} / ${fmtTime(this.duration)}`;
    this.draw();
    this.onTime?.(this.time);
  }

  loop() {
    cancelAnimationFrame(this.raf);
    let last = 0;
    const f = (ts) => {
      if (!this.container.isConnected) { this.destroy(); return; } // its page was closed
      if (ts - last > 60) { last = ts; this.tick(); }
      if (!this.audio.paused) this.raf = requestAnimationFrame(f);
    };
    this.raf = requestAnimationFrame(f);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    this.audio.pause();
    this.audio.removeAttribute('src');
    this.audio.load();
  }
}

export function markerLabel(text) { return esc(text); }
