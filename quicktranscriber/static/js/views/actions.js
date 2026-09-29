// All action items from all meetings in one checklist.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, fmtDate, fmtTime } from '../util.js';
import { errorToast } from '../ui.js';

export async function render(root) {
  let items = [];
  let filter = 'open';
  let owner = '';
  try { items = await api.get('/api/action-items'); } catch (e) { errorToast(e); }

  function draw() {
    const owners = [...new Set(items.map((a) => a.owner).filter(Boolean))].sort();
    const visible = items.filter((a) => (filter === 'all' || (filter === 'open' ? !a.done : a.done)) && (!owner || a.owner === owner));
    const open = items.filter((a) => !a.done).length;
    const groups = new Map();
    for (const a of visible) {
      if (!groups.has(a.meeting_id)) groups.set(a.meeting_id, { title: a.meeting_title, date: a.date, items: [] });
      groups.get(a.meeting_id).items.push(a);
    }
    root.innerHTML = `
      <div class="page-head"><div><div class="eyebrow">Across all meetings</div><h1>Action items</h1>
        <p>${open} open · ${items.length - open} done</p></div><div class="spacer"></div>
        ${owners.length ? `<select class="select" id="owner" style="width:200px"><option value="">Everyone</option>${owners.map((o) => `<option ${o === owner ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>` : ''}
        <div class="segmented" id="filter">${[['open', 'Open'], ['done', 'Done'], ['all', 'All']].map(([v, l]) => `<button data-value="${v}" class="${v === filter ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      ${!items.length ? `<div class="empty"><div class="big-icon">${icon('listChecks', 34)}</div><h2>No action items yet</h2><p>Tasks found in your meeting notes will appear here, so nothing slips through the cracks.</p></div>`
        : !visible.length ? `<div class="empty"><div class="big-icon">${icon('check', 34)}</div><h2>All clear</h2><p>Nothing ${filter === 'open' ? 'left to do' : 'here'}.</p></div>`
        : [...groups.entries()].map(([mid, g]) => `<div class="note-block" style="margin-bottom:14px">
          <h4 style="text-transform:none;letter-spacing:0;font-size:14px;color:var(--text)"><a href="#/m/${esc(mid)}" style="color:inherit">${esc(g.title)}</a> <span class="faint small" style="font-weight:500">${esc(fmtDate(g.date))}</span></h4>
          ${g.items.map((a) => `<div class="action ${a.done ? 'done' : ''}">
            <button class="check ${a.done ? 'on' : ''}" data-mid="${esc(mid)}" data-i="${a.index}">${icon('check', 13)}</button>
            <div style="flex:1"><div class="task">${esc(a.task)}</div><div class="sub">
              ${a.owner ? `<span class="owner"><i style="background:var(--accent)">${esc(a.owner[0] || '?')}</i>${esc(a.owner)}</span>` : ''}
              ${a.due ? `<span class="badge warn">${icon('calendar', 11)} ${esc(a.due)}</span>` : ''}
              ${a.t != null ? `<a class="ts link" href="#/m/${esc(mid)}?t=${a.t}">${fmtTime(a.t)}</a>` : ''}</div></div></div>`).join('')}</div>`).join('')}`;
    root.querySelector('#filter')?.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { filter = b.dataset.value; draw(); }
    });
    root.querySelector('#owner')?.addEventListener('change', (e) => { owner = e.target.value; draw(); });
    root.querySelectorAll('[data-mid]').forEach((b) => b.addEventListener('click', async () => {
      const a = items.find((x) => x.meeting_id === b.dataset.mid && x.index === +b.dataset.i);
      a.done = !a.done;
      draw();
      try { await api.patch(`/api/meetings/${b.dataset.mid}/action-items/${b.dataset.i}`, { done: a.done }); } catch (e) { errorToast(e); }
    }));
  }
  draw();
}
