"""Ask questions about a meeting ("What did we decide about the budget?")."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Callable, Iterator, Optional

from .client import Backend
from .notes import SYSTEM as NOTES_SYSTEM
from .notes import fmt_time, transcript_lines

WORD = re.compile(r"[\w']+", re.U)
STOP = set("""a an the and or but if of to in on at for with from by is are was were be been being it this that
these those i you he she we they me him her us them my your our their do does did so not no yes just
about what which who whom when where why how can could would should will shall may might must have has
had there here than then too very also as into over under up down out""".split())

SYSTEM = (
    "You answer questions about a meeting using its transcript. Answer only from the transcript and "
    "notes provided; if the answer is not there, say you couldn't find it in this meeting. Be concise. "
    "Whenever you use something from the transcript, cite the time in square brackets like [00:12:34] "
    "so the user can jump to it. Use the speakers' names as they appear."
)


def _tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def retrieve(lines: list[tuple[float, str]], question: str, budget_tokens: int,
             count: Callable[[str], int], window: int = 12) -> list[tuple[float, str]]:
    """BM25 over overlapping windows of transcript lines; returns lines in time order."""
    windows = []
    for start in range(0, len(lines), window // 2):
        chunk = lines[start : start + window]
        if chunk:
            windows.append((start, chunk))
    docs = [Counter(_tokens(" ".join(l for _, l in c))) for _, c in windows]
    q = _tokens(question)
    if not q:
        q = _tokens(question + " summary")
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs) / max(1, n)
    df = Counter(t for d in docs for t in set(d))
    scores = []
    for i, d in enumerate(docs):
        length = sum(d.values())
        s = 0.0
        for t in q:
            if t not in d:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            tf = d[t]
            s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(avg, 1)))
        scores.append(s)
    order = sorted(range(n), key=lambda i: -scores[i])
    picked: set[int] = set()
    used = 0
    for i in order:
        if scores[i] <= 0 and picked:
            break
        start, chunk = windows[i]
        new = [k for k in range(start, start + len(chunk)) if k not in picked]
        cost = sum(int(len(lines[k][1]) / 3.5) + 2 for k in new)
        if used + cost > budget_tokens:
            continue
        picked.update(new)
        used += cost
    return [lines[k] for k in sorted(picked)]


def build_messages(backend: Backend, question: str, segments: list[dict], names: dict[str, str],
                   notes: Optional[dict], history: list[dict]) -> list[dict]:
    lines = transcript_lines(segments, names)
    ctx = backend.context_size()
    notes_text = ""
    if notes:
        brief = {k: notes.get(k) for k in ("title", "summary", "decisions", "action_items") if notes.get(k)}
        notes_text = json.dumps(brief, ensure_ascii=False)[:6000]
    hist = history[-6:]
    hist_tokens = sum(int(len(m["content"]) / 3.5) for m in hist)
    budget = max(800, ctx - 1200 - hist_tokens - int(len(notes_text) / 3.5) - 700)
    full = "\n".join(l for _, l in lines)
    if int(len(full) / 3.5) <= budget:
        context = full
        scope = "the full transcript"
    else:
        context = "\n...\n".join(l for _, l in retrieve(lines, question, budget, backend.count_tokens))
        scope = "the most relevant parts of the transcript"
    msgs = [{"role": "system", "content": SYSTEM}]
    intro = f"Here is {scope}:\n{context}"
    if notes_text:
        intro += f"\n\nMeeting notes (may be incomplete):\n{notes_text}"
    msgs.append({"role": "user", "content": intro})
    msgs.append({"role": "assistant", "content": "Thanks, I have read it. What would you like to know?"})
    for m in hist:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})
    return msgs
