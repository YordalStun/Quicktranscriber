// "Ask your meeting" chat, answered by the local AI with clickable timestamps.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { esc, linkTimestamps } from '../util.js';
import { errorToast } from '../ui.js';

const running = new Map(); // meeting id -> AbortController

export async function renderAsk(body, ctx) {
  let history = [];
  try { history = await api.get(`/api/meetings/${ctx.id}/chat`); } catch { /* ignore */ }
  const names = ctx.m.speakers.filter((s) => s.status !== 'unknown').map((s) => s.name);
  const who = names[0] || ctx.m.speakers[0]?.name || 'Speaker 1';
  const suggestions = [
    'What was decided?',
    'What are the next steps and who owns them?',
    `What did ${who} say?`,
    'List every date, deadline and number mentioned',
    'What disagreements or concerns came up?',
    'Summarise the last 10 minutes',
  ];
  body.style.display = 'flex';
  body.style.flexDirection = 'column';
  body.style.padding = '0';
  body.innerHTML = `<div style="flex:1;overflow-y:auto;padding:18px 20px" data-scroll>
      <div class="chat" data-chat></div>
      <div data-intro ${history.length ? 'hidden' : ''} style="margin-top:6px">
        <div class="callout accent" style="margin-bottom:14px">${icon('message')}<div><b>Ask anything about this meeting</b>Answers come from the transcript, with timestamps you can click. Runs on your computer.</div></div>
        <div class="suggestions">${suggestions.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      </div></div>
    <div class="ask-bar"><input class="input" placeholder="Ask a question about this meeting…" data-input>
      <button class="btn primary" data-send title="Send">${icon('send', 16)}</button></div>
    ${history.length ? '<div style="text-align:center;padding:0 0 8px"><button class="btn ghost sm" data-clear>Clear conversation</button></div>' : ''}`;
  const chat = body.querySelector('[data-chat]');
  const scroller = body.querySelector('[data-scroll]');
  const input = body.querySelector('[data-input]');
  const bubble = (role, content) => `<div class="msg ${role}">${role === 'assistant' ? linkTimestamps(content) : esc(content)}</div>`;
  chat.innerHTML = history.map((h) => bubble(h.role, h.content)).join('');
  scroller.scrollTop = scroller.scrollHeight;

  async function send(q) {
    q = (q || input.value).trim();
    if (!q || running.get(ctx.id)) return;
    input.value = '';
    body.querySelector('[data-intro]').hidden = true;
    chat.insertAdjacentHTML('beforeend', bubble('user', q));
    const status = document.createElement('div');
    status.className = 'msg status';
    status.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> <span>Reading the meeting…</span>';
    chat.appendChild(status);
    scroller.scrollTop = scroller.scrollHeight;
    const ac = new AbortController();
    running.set(ctx.id, ac);
    let answerEl = null;
    let text = '';
    try {
      await api.stream(`/api/meetings/${ctx.id}/ask`, { question: q }, (ev) => {
        if (ev.type === 'status') status.querySelector('span:last-child').textContent = `${ev.message}…`;
        if (ev.type === 'token') {
          if (!answerEl) {
            status.remove();
            answerEl = document.createElement('div');
            answerEl.className = 'msg assistant';
            chat.appendChild(answerEl);
          }
          text += ev.text;
          answerEl.innerHTML = linkTimestamps(text);
          scroller.scrollTop = scroller.scrollHeight;
        }
        if (ev.type === 'error') {
          status.remove();
          chat.insertAdjacentHTML('beforeend', `<div class="callout bad">${icon('alert')}<div>${esc(ev.message)}${/model|engine/i.test(ev.message) ? ' <a href="#/models">Open AI models</a>' : ''}</div></div>`);
        }
        if (ev.type === 'done' && answerEl && ev.text) answerEl.innerHTML = linkTimestamps(ev.text);
      }, ac.signal);
    } catch (e) {
      if (e.name !== 'AbortError') errorToast(e);
    } finally {
      status.remove();
      running.delete(ctx.id);
    }
  }
  body.querySelector('[data-send]').onclick = () => send();
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
  body.querySelectorAll('[data-q]').forEach((b) => b.addEventListener('click', () => send(b.dataset.q)));
  body.querySelector('[data-clear]')?.addEventListener('click', async () => {
    await api.del(`/api/meetings/${ctx.id}/chat`);
    renderAsk(body, ctx);
  });
  setTimeout(() => input.focus(), 50);
}
