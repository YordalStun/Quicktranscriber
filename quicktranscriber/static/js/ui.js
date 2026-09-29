// Toasts, modals, menus and other shared UI pieces.
import { icon } from './icons.js';
import { esc, el } from './util.js';

// ---------- toasts ----------
export function toast(message, kind = 'info', ms = 3800) {
  let box = document.querySelector('.toasts');
  if (!box) { box = el('<div class="toasts"></div>'); document.body.appendChild(box); }
  const ic = { good: 'check', bad: 'alert', warn: 'alert', info: 'info' }[kind] || 'info';
  const t = el(`<div class="toast ${kind}">${icon(ic)}<div>${esc(message)}</div></div>`);
  box.appendChild(t);
  setTimeout(() => { t.style.transition = 'opacity .3s, transform .3s'; t.style.opacity = '0'; t.style.transform = 'translateX(20px)'; }, ms);
  setTimeout(() => t.remove(), ms + 350);
}

export function errorToast(err) {
  toast(err?.message || String(err), 'bad', 6000);
}

// ---------- modal ----------
export function modal({ title, subtitle = '', body = '', actions = [], wide = false, xwide = false, onOpen, dismissable = true, icon: ic }) {
  return new Promise((resolve) => {
    const overlay = el('<div class="overlay"></div>');
    const m = el(`<div class="modal ${wide ? 'wide' : ''} ${xwide ? 'xwide' : ''}" role="dialog" aria-modal="true">
      <div class="modal-head">${ic ? `<div class="big-icon" style="width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent)">${icon(ic, 20)}</div>` : ''}
        <div style="flex:1;min-width:0"><h2>${esc(title)}</h2>${subtitle ? `<p>${subtitle}</p>` : ''}</div>
        ${dismissable ? `<button class="btn ghost icon-only" data-close>${icon('x')}</button>` : ''}</div>
      <div class="modal-body"></div>
      <div class="modal-foot"></div></div>`);
    const bodyEl = m.querySelector('.modal-body');
    if (typeof body === 'string') bodyEl.innerHTML = body; else if (body) bodyEl.appendChild(body);
    if (!body) bodyEl.remove();
    const foot = m.querySelector('.modal-foot');
    if (!actions.length) foot.remove();
    let done = false;
    const close = (value) => {
      if (done) return;
      done = true;
      overlay.remove();
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === 'Escape' && dismissable) close(null);
      if (e.key === 'Enter' && !e.shiftKey && e.target.tagName !== 'TEXTAREA') {
        const primary = foot.querySelector('.btn.primary');
        if (primary && !primary.disabled && document.activeElement?.tagName !== 'BUTTON') { e.preventDefault(); primary.click(); }
      }
    };
    for (const a of actions) {
      const b = el(`<button class="btn ${a.kind || ''}">${a.icon ? icon(a.icon) : ''}${esc(a.label)}</button>`);
      if (a.left) b.style.marginRight = 'auto';
      b.addEventListener('click', async () => {
        if (a.onClick) {
          const r = await a.onClick(m, b);
          if (r === false) return;
          close(r === undefined ? a.value : r);
        } else close(a.value);
      });
      foot.appendChild(b);
    }
    m.querySelector('[data-close]')?.addEventListener('click', () => close(null));
    overlay.addEventListener('mousedown', (e) => { if (e.target === overlay && dismissable) close(null); });
    overlay.appendChild(m);
    document.body.appendChild(overlay);
    document.addEventListener('keydown', onKey);
    const first = m.querySelector('input:not([type=checkbox]):not([type=hidden]), textarea, select');
    if (first) setTimeout(() => first.focus(), 30);
    if (onOpen) onOpen(m, close);
  });
}

export function confirmDialog(title, message, { ok = 'OK', danger = false, cancel = 'Cancel' } = {}) {
  return modal({
    title, subtitle: message,
    actions: [{ label: cancel, value: false, kind: 'ghost' }, { label: ok, value: true, kind: danger ? 'danger' : 'primary' }],
  }).then((v) => !!v);
}

export function promptDialog(title, { value = '', placeholder = '', label = '', ok = 'Save', hint = '' } = {}) {
  const body = `<div class="field">${label ? `<label>${esc(label)}</label>` : ''}<input class="input" value="${esc(value)}" placeholder="${esc(placeholder)}">${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
  return modal({
    title, body,
    actions: [{ label: 'Cancel', value: null, kind: 'ghost' },
      { label: ok, kind: 'primary', onClick: (m) => m.querySelector('input').value.trim() || false }],
  });
}

// ---------- context menu ----------
let openMenu = null;
export function closeMenu() { openMenu?.remove(); openMenu = null; }
document.addEventListener('mousedown', (e) => { if (openMenu && !openMenu.contains(e.target)) closeMenu(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
window.addEventListener('blur', closeMenu);

/**
 * items: [{label, icon, onClick, danger, disabled, hint}] | {sep:true} | {header:'...'} | {html:'...'}
 */
export function menu(anchor, items, { align = 'left' } = {}) {
  closeMenu();
  const m = el('<div class="menu" role="menu"></div>');
  for (const it of items) {
    if (!it) continue;
    if (it.sep) { m.appendChild(el('<hr>')); continue; }
    if (it.header) { m.appendChild(el(`<div class="mh">${esc(it.header)}</div>`)); continue; }
    if (it.html) { const d = el(`<div class="mi">${it.html}</div>`); m.appendChild(d); continue; }
    const b = el(`<button class="${it.danger ? 'danger' : ''}" ${it.disabled ? 'disabled style="opacity:.5"' : ''}>${it.icon ? icon(it.icon, 16) : (it.color ? `<i style="width:10px;height:10px;border-radius:50%;background:${esc(it.color)};display:inline-block"></i>` : '')}<span style="flex:1">${esc(it.label)}</span>${it.hint ? `<span class="faint small">${esc(it.hint)}</span>` : ''}${it.checked ? icon('check', 15) : ''}</button>`);
    b.addEventListener('click', () => { closeMenu(); if (!it.disabled) it.onClick?.(); });
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, bottom: anchor.y, top: anchor.y };
  const mw = m.offsetWidth, mh = m.offsetHeight;
  let x = align === 'right' ? r.right - mw : r.left;
  let y = r.bottom + 6;
  if (x + mw > innerWidth - 8) x = innerWidth - mw - 8;
  if (y + mh > innerHeight - 8) y = Math.max(8, r.top - mh - 6);
  m.style.left = `${Math.max(8, x)}px`;
  m.style.top = `${y}px`;
  openMenu = m;
  return m;
}

// ---------- misc ----------
export function progressBar(value, indeterminate = false) {
  return `<div class="progress ${indeterminate ? 'indeterminate' : ''}"><i style="width:${Math.round((value || 0) * 100)}%"></i></div>`;
}

export function avatar(name, color, cls = '') {
  const init = String(name || '?').trim();
  const text = /^speaker\s+\d+/i.test(init) ? init.replace(/\D+/g, '') : (init.split(/\s+/).map((p) => p[0]).slice(0, 2).join('') || '?');
  return `<div class="avatar ${cls}" style="background:${esc(color || '#8b5cf6')}" title="${esc(name)}">${esc(text.toUpperCase())}</div>`;
}

export function switchEl(checked, attrs = '') {
  return `<label class="switch"><input type="checkbox" ${checked ? 'checked' : ''} ${attrs}><span class="track"></span></label>`;
}

export function segmented(options, value, attrs = '') {
  return `<div class="segmented" ${attrs}>${options.map((o) => `<button type="button" data-value="${esc(o.value)}" class="${String(o.value) === String(value) ? 'on' : ''}">${esc(o.label)}</button>`).join('')}</div>`;
}

export function bindSegmented(root, onChange) {
  root.querySelectorAll('.segmented').forEach((seg) => {
    seg.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-value]');
      if (!b) return;
      seg.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
      onChange?.(seg, b.dataset.value);
    });
  });
}

export function segValue(seg) {
  return seg.querySelector('button.on')?.dataset.value;
}

// Shared hidden audio element for playing short voice clips
let clipAudio;
export function playClip(url, btn) {
  if (!clipAudio) clipAudio = new Audio();
  const same = clipAudio.dataset.url === url && !clipAudio.paused;
  clipAudio.pause();
  document.querySelectorAll('.mini-play.playing').forEach((b) => { b.classList.remove('playing'); b.innerHTML = icon('play', 14); });
  if (same) return;
  clipAudio.src = url;
  clipAudio.dataset.url = url;
  clipAudio.play().catch(() => toast('Could not play this clip', 'bad'));
  if (btn) {
    btn.classList.add('playing');
    btn.innerHTML = icon('pause', 14);
    clipAudio.onended = () => { btn.classList.remove('playing'); btn.innerHTML = icon('play', 14); };
  }
}
