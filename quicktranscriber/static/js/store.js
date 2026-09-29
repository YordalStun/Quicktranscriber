// Global app state: status, settings and live job progress.
import { api } from './api.js';

const listeners = new Set();
export const store = {
  status: null,
  settings: null,
  languages: {},
  templates: [],
  jobs: [],
  downloads: [],
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit(what) {
  for (const fn of listeners) {
    try { fn(what); } catch (e) { console.error(e); }
  }
}

export async function loadStatus() {
  store.status = await api.get('/api/status');
  emit('status');
  return store.status;
}

export async function loadSettings() {
  const s = await api.get('/api/settings');
  store.settings = s.settings;
  store.languages = s.languages;
  store.templates = s.templates;
  document.documentElement.dataset.theme = store.settings.theme === 'light' ? 'light' : 'dark';
  emit('settings');
  return s;
}

export async function saveSettings(changes) {
  const r = await api.put('/api/settings', changes);
  store.settings = r.settings;
  document.documentElement.dataset.theme = store.settings.theme === 'light' ? 'light' : 'dark';
  emit('settings');
  return r.settings;
}

// Poll jobs + downloads: fast while something is happening, slow otherwise.
let pollTimer = null;
let lastActive = false;
let finishedJobs = new Set();

export async function pollJobs() {
  clearTimeout(pollTimer);
  try {
    const r = await api.get('/api/jobs');
    const prevRunning = new Map(store.jobs.filter((j) => j.status === 'running' || j.status === 'queued').map((j) => [j.id, j]));
    store.jobs = r.jobs;
    store.downloads = r.downloads;
    for (const j of r.jobs) {
      if (prevRunning.has(j.id) && ['done', 'error', 'cancelled'].includes(j.status) && !finishedJobs.has(j.id)) {
        finishedJobs.add(j.id);
        emit({ type: 'job-finished', job: j });
      }
    }
    // jobs that vanished from the list finished too
    for (const [id, j] of prevRunning) {
      if (!r.jobs.find((x) => x.id === id) && !finishedJobs.has(id)) {
        finishedJobs.add(id);
        emit({ type: 'job-finished', job: { ...j, status: 'done' } });
      }
    }
    emit('jobs');
    lastActive = r.jobs.some((j) => j.status === 'running' || j.status === 'queued') ||
      r.downloads.some((d) => ['queued', 'downloading', 'extracting'].includes(d.status));
  } catch (e) {
    lastActive = false;
  }
  pollTimer = setTimeout(pollJobs, lastActive ? 1000 : 4000);
}

export function kickPoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollJobs, 150);
}

export function activeJobFor(meetingId) {
  return store.jobs.find((j) => j.meeting_id === meetingId && (j.status === 'running' || j.status === 'queued'));
}

export function languageName(code) {
  return store.languages[code] || code || '';
}
