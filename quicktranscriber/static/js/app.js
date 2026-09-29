// App shell: sidebar, router, job tray, drag & drop, shortcuts.
import { icon } from './icons.js';
import { esc, fmtEta } from './util.js';
import { store, subscribe, loadStatus, loadSettings, pollJobs } from './store.js';
import { toast, progressBar } from './ui.js';
import { openImport } from './views/importer.js';

const routes = {
  '': () => import('./views/library.js'),
  m: () => import('./views/meeting.js'),
  record: () => import('./views/record.js'),
  actions: () => import('./views/actions.js'),
  speakers: () => import('./views/speakers.js'),
  models: () => import('./views/models.js'),
  settings: () => import('./views/settings.js'),
};

const NAV = [
  { route: '', label: 'Meetings', icon: 'wave' },
  { route: 'actions', label: 'Action items', icon: 'listChecks' },
  { route: 'speakers', label: 'Speakers', icon: 'users' },
  { route: 'models', label: 'AI models', icon: 'layers' },
  { route: 'settings', label: 'Settings', icon: 'settings' },
];

let cleanup = null;
let currentRoute = null;
let navToken = 0;
let rendering = Promise.resolve(); // the page being opened right now

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  return { name: parts[0] || '', args: parts.slice(1), params };
}

async function route() {
  const token = ++navToken;
  // Let a page that is still opening finish first, so it can be closed properly - otherwise
  // a double-click could leave a hidden copy of a meeting (and its audio) running.
  await rendering;
  if (token !== navToken) return; // a newer navigation takes over
  const { name, args, params } = parseHash();
  const loader = routes[name] || routes[''];
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  currentRoute = name;
  renderSidebar();
  const view = document.getElementById('view');
  view.className = 'view';
  view.innerHTML = '';
  let opened;
  rendering = new Promise((resolve) => { opened = resolve; });
  try {
    const mod = await loader();
    cleanup = (await mod.render(view, { args, params })) || null;
  } catch (e) {
    console.error(e);
    view.innerHTML = `<div class="empty"><div class="big-icon">${icon('alert', 34)}</div><h2>Something went wrong</h2><p>${esc(e.message)}</p></div>`;
  } finally {
    opened();
  }
  renderTray();
}

function renderSidebar() {
  const sb = document.getElementById('sidebar');
  const st = store.status;
  const running = store.jobs.filter((j) => j.status === 'running' || j.status === 'queued').length;
  const hw = st?.hardware;
  const gpu = hw?.gpus?.[0];
  const accel = st?.device === 'cuda' ? `${gpu?.name?.replace(/NVIDIA |GeForce /g, '') || 'GPU'} · GPU` : hw ? `${hw.cores}-core CPU · ${hw.ram_gb} GB` : '';
  sb.innerHTML = `
    <div class="brand"><div class="brand-mark"><span><i></i><i></i><i></i><i></i><i></i></span></div>
      <div class="brand-name">QuickTranscriber<small>Private meeting notes</small></div></div>
    <a class="nav-item nav-record" href="#/record"><span class="dot"></span><span>Record</span></a>
    ${NAV.map((n) => `<a class="nav-item ${currentRoute === n.route || (n.route === '' && currentRoute === 'm') ? 'active' : ''}" href="#/${n.route}">
      ${icon(n.icon)}<span>${n.label}</span>${n.route === '' && running ? `<span class="count">${running}</span>` : ''}</a>`).join('')}
    <div class="sidebar-foot">
      <div class="privacy-badge">${icon('shield', 18)}<div>100% offline<small>Nothing leaves this computer</small></div></div>
      ${accel ? `<div class="hw-chip" title="${esc(hw.cpu)}">${icon(st?.device === 'cuda' ? 'zap' : 'cpu', 14)}<span>${esc(accel)}</span></div>` : ''}
    </div>`;
}

function renderTray() {
  const tray = document.getElementById('tray');
  const { name, args } = parseHash();
  const items = [];
  for (const j of store.jobs) {
    if (j.status !== 'running' && j.status !== 'queued') continue;
    if (name === 'm' && args[0] === j.meeting_id) continue;
    const label = j.status === 'queued' ? 'Waiting in queue' : (j.message || j.stage_label || 'Working');
    items.push(`<div class="tray-item" data-href="#/m/${esc(j.meeting_id)}">
      <b>${esc(j.meeting_title || 'Meeting')}</b>
      <div class="row faint"><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(label)}</span><span>${j.status === 'running' ? Math.round((j.progress || 0) * 100) + '%' : ''}</span></div>
      ${progressBar(j.progress || 0, j.status === 'queued')}
      ${j.eta ? `<div class="faint small" style="margin-top:4px">${fmtEta(j.eta)}</div>` : ''}</div>`);
  }
  if (name !== 'models') {
    for (const d of store.downloads) {
      if (!['queued', 'downloading', 'extracting'].includes(d.status)) continue;
      const pct = d.total ? d.done / d.total : 0;
      items.push(`<div class="tray-item" data-href="#/models"><b>${icon('download', 14)} ${esc(d.name)}</b>
        <div class="row faint"><span style="flex:1">${d.status === 'extracting' ? 'Unpacking' : 'Downloading'}</span><span>${Math.round(pct * 100)}%</span></div>${progressBar(pct, !d.total)}</div>`);
    }
  }
  tray.innerHTML = items.slice(0, 4).join('');
}

document.getElementById('tray').addEventListener('click', (e) => {
  const it = e.target.closest('[data-href]');
  if (it) navigate(it.dataset.href);
});

// ---------- drag & drop anywhere ----------
let dragDepth = 0;
const dz = document.getElementById('dropzone');
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
window.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; dragDepth++; dz.classList.add('on'); e.preventDefault(); });
window.addEventListener('dragleave', (e) => { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) dz.classList.remove('on'); });
window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  dz.classList.remove('on');
  const files = [...e.dataTransfer.files];
  if (files.length) openImport(files);
});

// ---------- shortcuts ----------
window.addEventListener('keydown', (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (currentRoute !== '') navigate('#/');
    setTimeout(() => document.getElementById('library-search')?.focus(), 80);
  }
  if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && e.key === 'n' && currentRoute === '') {
    document.getElementById('import-btn')?.click();
  }
});

// ---------- notifications when long jobs finish ----------
subscribe((what) => {
  if (what === 'jobs' || what === 'status') { renderTray(); if (what === 'status') renderSidebar(); }
  if (what === 'jobs') {
    const running = store.jobs.filter((j) => j.status === 'running' || j.status === 'queued').length;
    const cnt = document.querySelector('.nav-item .count');
    if ((cnt ? +cnt.textContent : 0) !== running) renderSidebar();
  }
  if (what?.type === 'job-finished') {
    const j = what.job;
    const title = j.meeting_title || 'Your meeting';
    if (j.status === 'done') {
      toast(`${title} is ready`, 'good');
      if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
        const n = new Notification('Meeting ready', { body: `${title} has been transcribed.`, icon: '/static/img/icon.svg' });
        n.onclick = () => { window.focus(); navigate(`#/m/${j.meeting_id}`); };
      }
    } else if (j.status === 'error') {
      toast(`${title}: ${j.error || 'processing failed'}`, 'bad', 8000);
    }
  }
});

export function askNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}

// ---------- start ----------
async function start() {
  try {
    await loadSettings();
    try { localStorage.setItem('qt-theme', store.settings.theme); } catch { /* private mode */ }
    await loadStatus();
  } catch (e) {
    document.getElementById('view').innerHTML = `<div class="empty"><h2>Can't reach QuickTranscriber</h2><p>${esc(e.message)}</p></div>`;
    return;
  }
  renderSidebar();
  pollJobs();
  window.addEventListener('hashchange', route);
  await route();
  if (!store.settings.onboarded) {
    const { openOnboarding } = await import('./views/onboarding.js');
    openOnboarding();
  }
  setInterval(() => loadStatus().catch(() => {}), 30000);
}

start();
