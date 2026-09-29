// Small helpers shared by all views.

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function fmtTime(sec, forceHours = false) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(h || forceHours ? 2 : 1, '0'), ss = String(s).padStart(2, '0');
  return h || forceHours ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  if (h) return m ? `${h} h ${m} min` : `${h} h`;
  return `${m} min`;
}

export function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return '';
  sec = Math.max(0, Math.round(sec));
  if (sec < 45) return 'less than a minute left';
  if (sec < 3600) return `about ${Math.round(sec / 60)} min left`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return `about ${h} h ${m} min left`;
}

export function fmtBytes(n) {
  if (!n) return '0 MB';
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e10 ? 0 : 1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
}

export function fmtDate(ts, opts = {}) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (opts.timeOnly) return time;
  const sameDay = d.toDateString() === now.toDateString();
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (sameDay) return `Today, ${time}`;
  if (d.toDateString() === yest.toDateString()) return `Yesterday, ${time}`;
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString([], { weekday: opts.long ? 'long' : 'short', day: 'numeric', month: 'short', year: sameYear ? undefined : 'numeric' }) + `, ${time}`;
}

export function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return d.toLocaleDateString([], { weekday: 'long' });
  return d.toLocaleDateString([], { month: 'long', year: 'numeric' });
}

export function initials(name) {
  const parts = String(name || '?').replace(/[^\p{L}\p{N} ]/gu, '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  if (/^speaker$/i.test(parts[0]) && parts[1]) return parts[1].slice(0, 2);
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function throttle(fn, ms = 100) {
  let last = 0, t;
  return (...a) => {
    const now = Date.now();
    clearTimeout(t);
    if (now - last >= ms) { last = now; fn(...a); } else { t = setTimeout(() => { last = Date.now(); fn(...a); }, ms - (now - last)); }
  };
}

export function $(sel, root = document) { return root.querySelector(sel); }
export function $$(sel, root = document) { return [...root.querySelectorAll(sel)]; }

export function html(strings, ...vals) {
  return strings.reduce((out, s, i) => out + s + (i < vals.length ? (vals[i] ?? '') : ''), '');
}

export function el(markup) {
  const t = document.createElement('template');
  t.innerHTML = markup.trim();
  return t.content.firstElementChild;
}

export function dots(n, max = 5, cls = '') {
  return `<span class="dotbar ${cls}">${Array.from({ length: max }, (_, i) => `<i class="${i < n ? 'on' : ''}"></i>`).join('')}</span>`;
}

// Turn [00:12:34] or 12:34 in AI answers into clickable timestamps
export function linkTimestamps(text) {
  return esc(text).replace(/\[?\b((\d{1,2}):)?(\d{1,2}):(\d{2})\b\]?/g, (m, _h, h, mi, s) => {
    const sec = (+h || 0) * 3600 + (+mi) * 60 + (+s);
    return `<a class="ts" data-seek="${sec}">${m.replace(/[\[\]]/g, '')}</a>`;
  });
}

export function parseTime(str) {
  const m = String(str).match(/(?:(\d+):)?(\d{1,2}):(\d{2})/);
  if (!m) return null;
  return (+m[1] || 0) * 3600 + (+m[2]) * 60 + (+m[3]);
}

export function download(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
