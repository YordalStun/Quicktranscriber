// Export dialog.
import { icon } from '../icons.js';
import { esc, download } from '../util.js';
import { modal, toast } from '../ui.js';

const FORMATS = [
  { id: 'txt', name: 'Text', ext: '.txt', icon: 'fileText', desc: 'Plain text, opens anywhere' },
  { id: 'docx', name: 'Word', ext: '.docx', icon: 'fileText', desc: 'Formatted document for Word / Google Docs' },
  { id: 'pdf', name: 'PDF', ext: '.pdf', icon: 'fileText', desc: 'Print-ready - opens a page you can save as PDF' },
  { id: 'md', name: 'Markdown', ext: '.md', icon: 'hash', desc: 'For Notion, Obsidian, GitHub…' },
  { id: 'html', name: 'Web page', ext: '.html', icon: 'globe', desc: 'A single file to share' },
  { id: 'srt', name: 'Subtitles', ext: '.srt', icon: 'monitor', desc: 'For video players & editors' },
  { id: 'vtt', name: 'WebVTT', ext: '.vtt', icon: 'monitor', desc: 'Subtitles for the web' },
  { id: 'json', name: 'JSON', ext: '.json', icon: 'layers', desc: 'Everything, for developers' },
  { id: 'zip', name: 'Everything', ext: '.zip', icon: 'folder', desc: 'All formats + the audio' },
];

export function openExport(m) {
  let fmt = 'txt';
  try { fmt = localStorage.getItem('qt-export') || 'txt'; } catch { /* ignore */ }
  const hasNotes = !!m.notes;
  const body = `<div class="choices" data-formats>${FORMATS.map((f) => `<button type="button" class="choice ${f.id === fmt ? 'on' : ''}" data-fmt="${f.id}">
      <b>${icon(f.icon, 15)} ${f.name} <span class="faint" style="font-weight:500">${f.ext}</span></b><span>${f.desc}</span></button>`).join('')}</div>
    <div class="row wrap" style="gap:18px;margin-top:18px" data-opts>
      <label class="row"><input type="checkbox" name="notes" ${hasNotes ? 'checked' : 'disabled'}> Notes</label>
      <label class="row"><input type="checkbox" name="transcript" checked> Transcript</label>
      <label class="row"><input type="checkbox" name="timestamps" checked> Timestamps</label>
      <label class="row"><input type="checkbox" name="speakers" checked> Speaker names</label>
      <label class="row" data-audio-opt><input type="checkbox" name="audio" checked> Include audio</label>
    </div>`;
  return modal({
    title: 'Export', subtitle: esc(m.title), icon: 'download', wide: true, body,
    onOpen: (el) => {
      const sync = () => {
        const f = el.querySelector('[data-fmt].on')?.dataset.fmt;
        el.querySelector('[data-audio-opt]').hidden = f !== 'zip';
      };
      el.querySelector('[data-formats]').addEventListener('click', (e) => {
        const c = e.target.closest('[data-fmt]');
        if (!c) return;
        el.querySelectorAll('[data-fmt]').forEach((x) => x.classList.toggle('on', x === c));
        sync();
      });
      el.querySelector('[data-formats]').addEventListener('dblclick', (e) => {
        if (e.target.closest('[data-fmt]')) el.querySelector('.modal-foot .btn.primary').click();
      });
      sync();
    },
    actions: [
      { label: 'Copy notes', icon: 'copy', kind: 'ghost', left: true, onClick: async () => {
        if (!m.notes) { toast('No notes yet', 'warn'); return false; }
        const { notesAsText } = await import('./meeting-notes.js');
        await navigator.clipboard.writeText(notesAsText({ m }));
        toast('Notes copied', 'good');
        return false;
      } },
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Export', kind: 'primary', icon: 'download', onClick: (el) => {
        const f = el.querySelector('[data-fmt].on')?.dataset.fmt || 'txt';
        try { localStorage.setItem('qt-export', f); } catch { /* ignore */ }
        const q = (n) => el.querySelector(`[name=${n}]`).checked;
        const params = new URLSearchParams({
          format: f === 'pdf' ? 'html' : f,
          notes: q('notes'), transcript: q('transcript'), timestamps: q('timestamps'), speakers: q('speakers'), audio: q('audio'),
        });
        const url = `/api/meetings/${m.id}/export?${params}`;
        if (f === 'pdf') {
          // open the printable page and bring up the print dialog ("Save as PDF")
          fetch(url).then((r) => r.text()).then((html) => {
            const w = window.open('', '_blank');
            if (!w) { toast('Allow pop-ups to export PDF', 'warn'); return; }
            w.document.write(html.replace('</body>', '<script>setTimeout(()=>print(),400)<\/script></body>'));
            w.document.close();
          });
        } else {
          download(url);
          toast('Exported', 'good', 2000);
        }
      } },
    ],
  });
}
