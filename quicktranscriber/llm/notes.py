"""Meeting notes from a transcript, for meetings of any length.

Short meetings are summarised in one pass. Long meetings use map-reduce: the
transcript is split into parts that fit the model's memory, notes are taken
for every part, then the partial notes are merged into the final notes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional

from .. import catalog
from .client import Backend

log = logging.getLogger("qt.notes")

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "general",
        "name": "General meeting",
        "icon": "users",
        "description": "Summary, key points, decisions, action items and chapters.",
        "focus": "",
        "sections": [],
    },
    {
        "id": "committee",
        "name": "Committee / formal minutes",
        "icon": "gavel",
        "description": "Formal minutes: proposals, votes, resolutions and actions.",
        "focus": "Write formal minutes. Pay special attention to proposals and motions, who proposed or "
                 "supported them, votes and their outcomes, formal resolutions, reports given, and matters "
                 "carried over to the next meeting.",
        "sections": ["Proposals and votes", "Reports given", "Carried over to next meeting"],
    },
    {
        "id": "standup",
        "name": "Team stand-up",
        "icon": "zap",
        "description": "What each person did, is doing next, and what blocks them.",
        "focus": "This is a short team stand-up. Focus on each person's progress, plans and blockers.",
        "sections": ["Updates by person", "Blockers"],
    },
    {
        "id": "project",
        "name": "Project update",
        "icon": "kanban",
        "description": "Progress, risks, timeline and owners.",
        "focus": "Focus on project status, progress against plans, risks and issues, dates and deadlines.",
        "sections": ["Progress", "Risks and issues", "Timeline and deadlines"],
    },
    {
        "id": "one_on_one",
        "name": "1:1 / check-in",
        "icon": "message",
        "description": "Feedback, wellbeing, goals and follow-ups.",
        "focus": "This is a one-to-one conversation. Capture feedback given in both directions, concerns, "
                 "goals and agreed follow-ups. Be tactful.",
        "sections": ["Feedback", "Goals and development"],
    },
    {
        "id": "sales",
        "name": "Client / sales call",
        "icon": "briefcase",
        "description": "Client needs, objections, pricing and next steps.",
        "focus": "This is a call with a client or customer. Capture their needs and pain points, questions, "
                 "objections, budget or pricing discussed, commitments made and next steps.",
        "sections": ["Client needs", "Objections and concerns", "Pricing and commitments"],
    },
    {
        "id": "interview",
        "name": "Interview",
        "icon": "mic",
        "description": "Questions asked, notable answers, strengths and concerns.",
        "focus": "This is an interview. Capture the main questions and the candidate's or guest's answers, "
                 "strengths, concerns and notable quotes. Stay factual and fair.",
        "sections": ["Notable answers", "Strengths", "Concerns"],
    },
    {
        "id": "lecture",
        "name": "Lecture / class",
        "icon": "book",
        "description": "Study notes: concepts, definitions, examples and homework.",
        "focus": "This is a lecture or class. Write study notes: key concepts, definitions, explanations, "
                 "examples, and any homework, deadlines or exam hints.",
        "sections": ["Key concepts and definitions", "Examples", "Homework and deadlines"],
    },
    {
        "id": "brainstorm",
        "name": "Brainstorm / workshop",
        "icon": "lightbulb",
        "description": "All ideas, themes and the favourites.",
        "focus": "This is a brainstorm. Capture every distinct idea, group them into themes, and note which "
                 "ideas got the most support.",
        "sections": ["Ideas", "Themes", "Most supported ideas"],
    },
]

DETAIL = {
    "brief": {"summary": "2-3 sentences", "points": 6, "chapters": 6, "chapter": "a few words",
              "map_out": 900, "final_out": 1600},
    "standard": {"summary": "one paragraph of 4-7 sentences", "points": 12, "chapters": 10,
                 "chapter": "one sentence", "map_out": 1400, "final_out": 2600},
    "detailed": {"summary": "two or three paragraphs", "points": 20, "chapters": 16,
                 "chapter": "two or three sentences", "map_out": 2000, "final_out": 4000},
}

SYSTEM = (
    "You are an expert meeting assistant. You write accurate, clear, well organised notes from meeting "
    "transcripts. Use only information that is in the transcript: never invent names, numbers, dates, "
    "decisions or tasks. The transcript was made by speech recognition and may contain mistakes, so "
    "interpret unclear words sensibly. Timestamps look like [01:02:03] (hours:minutes:seconds). People "
    "labelled 'Speaker 1', 'Speaker 2' etc. have not been identified: use those labels, or a name only if "
    "the transcript clearly shows who they are."
)


def template(template_id: str) -> dict[str, Any]:
    return next((t for t in TEMPLATES if t["id"] == template_id), TEMPLATES[0])


# ---------------------------------------------------------------------------
# Schemas (constrain the model to valid JSON)
# ---------------------------------------------------------------------------

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_ACTION = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "owner": {"type": "string"},
        "due": {"type": "string"},
        "time": {"type": "string"},
    },
    "required": ["task", "owner", "due", "time"],
}


def _list(limit: int, items: Optional[dict] = None) -> dict:
    return {"type": "array", "items": items or {"type": "string"}, "maxItems": limit}


def _sections_schema(titles: list[str], limit: int) -> dict:
    return {
        "type": "array",
        "maxItems": len(titles),
        "items": {
            "type": "object",
            "properties": {"title": {"type": "string", "enum": titles}, "items": _list(limit)},
            "required": ["title", "items"],
        },
    }


def map_schema(tpl: dict, detail: dict) -> dict:
    props: dict[str, Any] = {
        "summary": {"type": "string"},
        "key_points": _list(detail["points"]),
        "decisions": _list(12),
        "action_items": _list(15, _ACTION),
        "open_questions": _list(10),
        "topics": _list(8, {
            "type": "object",
            "properties": {"time": {"type": "string"}, "title": {"type": "string"}},
            "required": ["time", "title"],
        }),
    }
    required = list(props)
    if tpl["sections"]:
        props["sections"] = _sections_schema(tpl["sections"], 12)
        required.append("sections")
    return {"type": "object", "properties": props, "required": required}


def final_schema(tpl: dict, detail: dict) -> dict:
    props: dict[str, Any] = {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_points": _list(detail["points"]),
        "decisions": _list(20),
        "action_items": _list(30, _ACTION),
        "open_questions": _list(15),
        "chapters": _list(detail["chapters"], {
            "type": "object",
            "properties": {
                "time": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["time", "title", "summary"],
        }),
    }
    required = list(props)
    if tpl["sections"]:
        props["sections"] = _sections_schema(tpl["sections"], 20)
        required.append("sections")
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# Transcript formatting and chunking
# ---------------------------------------------------------------------------


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def parse_time(value: Any) -> Optional[float]:
    if value is None:
        return None
    m = re.search(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", str(value))
    if not m:
        return None
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def transcript_lines(segments: list[dict], names: dict[str, str]) -> list[tuple[float, str]]:
    lines = []
    for s in segments:
        who = names.get(s.get("speaker") or "", s.get("speaker") or "Speaker")
        lines.append((s["start"], f"[{fmt_time(s['start'])}] {who}: {s['text']}"))
    return lines


def chunk_lines(lines: list[tuple[float, str]], budget_tokens: int, count: Callable[[str], int]) -> list[list]:
    """Split transcript lines into chunks of at most ``budget_tokens`` tokens."""
    chunks: list[list] = []
    cur: list = []
    cur_tokens = 0
    for item in lines:
        t = count(item[1]) if len(item[1]) > 400 else int(len(item[1]) / 3.5) + 2
        if cur and cur_tokens + t > budget_tokens:
            chunks.append(cur)
            # carry the last line over for context
            cur = cur[-1:]
            cur_tokens = sum(int(len(x[1]) / 3.5) + 2 for x in cur)
        cur.append(item)
        cur_tokens += t
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _language_line(lang_code: Optional[str]) -> str:
    name = catalog.LANGUAGES.get(lang_code or "", None)
    if not name or lang_code == "auto":
        return "Write the notes in the same language as the transcript."
    return f"Write the notes in {name}."


def _instructions(tpl: dict, extra: str) -> str:
    parts = []
    if tpl["focus"]:
        parts.append(tpl["focus"])
    if tpl["sections"]:
        parts.append("Also fill these sections (leave a section's items empty if nothing fits): "
                     + "; ".join(tpl["sections"]) + ".")
    if extra.strip():
        parts.append("Extra instructions from the user: " + extra.strip())
    return "\n".join(parts)


def generate(
    backend: Backend,
    segments: list[dict],
    names: dict[str, str],
    duration: float,
    template_id: str = "general",
    detail_level: str = "standard",
    language: Optional[str] = None,
    extra_instructions: str = "",
    progress: Optional[Callable[[float, str], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    tpl = template(template_id)
    detail = DETAIL.get(detail_level, DETAIL["standard"])
    lang = _language_line(language)
    extra = _instructions(tpl, extra_instructions)
    lines = transcript_lines(segments, names)
    if not lines:
        raise ValueError("The transcript is empty.")

    def report(value: float, message: str) -> None:
        if progress:
            progress(value, message)

    ctx = backend.context_size()
    full_text = "\n".join(l for _, l in lines)
    total_tokens = backend.count_tokens(full_text)
    overhead = 900
    started = time.time()
    comfortable = backend.comfortable_tokens()
    single_budget = min(ctx - overhead - detail["final_out"], comfortable)
    stats = {"context": ctx, "transcript_tokens": total_tokens}

    if total_tokens <= single_budget:
        report(0.05, "Writing the notes")
        notes = _final_pass(backend, full_text, tpl, detail, lang, extra, duration, report, cancelled, direct=True)
        stats["parts"] = 1
    else:
        budget = max(1000, min(ctx - overhead - detail["map_out"], comfortable))
        # spread the transcript evenly over the parts
        parts = max(2, -(-total_tokens // budget))
        budget = min(budget, int(total_tokens / parts * 1.08) + 200)
        chunks = chunk_lines(lines, budget, backend.count_tokens)
        stats["parts"] = len(chunks)
        partials = []
        for i, chunk in enumerate(chunks):
            if cancelled and cancelled():
                raise InterruptedError("cancelled")
            report(0.02 + 0.75 * i / len(chunks), f"Reading part {i + 1} of {len(chunks)}")
            text = "\n".join(l for _, l in chunk)
            span = f"{fmt_time(chunk[0][0])} to {fmt_time(chunk[-1][0])}"
            prompt = (
                f"This is part {i + 1} of {len(chunks)} of a meeting transcript, covering {span}.\n"
                f"{extra}\n"
                "Take notes on THIS PART ONLY:\n"
                "- summary: 2-4 sentences on what was discussed\n"
                "- key_points: the important points, facts, figures and updates (each one different - never "
                "repeat an idea in other words)\n"
                "- decisions: decisions or agreements that were made (empty if none)\n"
                "- action_items: tasks someone agreed or was asked to do: task, owner (the speaker label "
                "or name), due date if one was mentioned (else empty), and the timestamp where it was said\n"
                "- open_questions: questions or issues left unresolved\n"
                "- topics: the main topics in order, each with the timestamp where it starts\n"
                f"{lang}\n\nTranscript:\n{text}"
            )
            part = backend.chat_json(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                map_schema(tpl, detail), max_tokens=detail["map_out"], cancelled=cancelled,
            )
            partials.append(_clean_partial(part))
        report(0.8, "Combining the notes")
        partials = _condense(backend, partials, ctx - overhead - detail["final_out"], tpl, detail, lang, extra,
                             cancelled)
        notes = _final_pass(backend, json.dumps(partials, ensure_ascii=False), tpl, detail, lang, extra, duration,
                            report, cancelled, direct=False)
    notes = _clean_final(notes, duration)
    stats["seconds"] = round(time.time() - started, 1)
    notes["_stats"] = stats
    return notes


def _final_pass(backend, content, tpl, detail, lang, extra, duration, report, cancelled, direct: bool) -> dict:
    what = "the full transcript of a meeting" if direct else "notes taken from consecutive parts of ONE meeting"
    tail = "Transcript:" if direct else "Notes from each part, in order (JSON):"
    prompt = (
        f"Below is {what} that lasted {fmt_time(duration)}. Write the final meeting notes.\n"
        f"{extra}\n"
        "- title: a short, specific title for this meeting (at most 8 words, no date)\n"
        f"- summary: {detail['summary']} summarising the whole meeting\n"
        "- key_points: the most important points, with concrete details (names, numbers, dates); each "
        "point must say something new - never repeat an idea in other words\n"
        "- decisions: every decision or agreement\n"
        "- action_items: every task: task, owner, due date (empty if none), timestamp where it was said\n"
        "- open_questions: questions or issues left unresolved\n"
        f"- chapters: the meeting's main parts in time order, spread across the WHOLE meeting from 00:00:00 to "
        f"{fmt_time(duration)}; each with the timestamp where it starts, a short title and {detail['chapter']} "
        "of summary\n"
        + ("" if direct else "Merge duplicates between parts, keep every concrete detail.\n")
        + f"{lang}\n\n{tail}\n{content}"
    )
    produced = [0]

    def on_token(_piece: str) -> None:
        produced[0] += 1
        if produced[0] % 20 == 0:
            base = 0.05 if direct else 0.82
            report(min(0.98, base + (0.95 - base) * produced[0] / (detail["final_out"] * 0.6)),
                   "Writing the notes")

    return backend.chat_json(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        final_schema(tpl, detail), max_tokens=detail["final_out"], cancelled=cancelled, on_token=on_token,
    )


def _condense(backend, partials, budget, tpl, detail, lang, extra, cancelled) -> list:
    """Merge partial notes in groups until they fit in one final prompt."""
    rounds = 0
    while backend.count_tokens(json.dumps(partials, ensure_ascii=False)) > budget and len(partials) > 1 and rounds < 4:
        rounds += 1
        groups: list[list] = []
        cur: list = []
        for p in partials:
            cur.append(p)
            if backend.count_tokens(json.dumps(cur, ensure_ascii=False)) > budget * 0.8 and len(cur) > 1:
                groups.append(cur[:-1])
                cur = [p]
        if cur:
            groups.append(cur)
        if len(groups) == len(partials):  # each part alone is too big: pair them up
            groups = [partials[i : i + 2] for i in range(0, len(partials), 2)]
        merged = []
        for g in groups:
            if cancelled and cancelled():
                raise InterruptedError("cancelled")
            if len(g) == 1:
                merged.append(g[0])
                continue
            prompt = (
                "Merge these notes from consecutive parts of one meeting into a single set of notes for "
                "the combined span. Remove duplicates but keep every concrete detail, decision, task and "
                f"timestamp.\n{extra}\n{lang}\n\nNotes (JSON):\n{json.dumps(g, ensure_ascii=False)}"
            )
            merged.append(_clean_partial(backend.chat_json(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                map_schema(tpl, detail), max_tokens=detail["map_out"], cancelled=cancelled,
            )))
        partials = merged
    return partials


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        if not isinstance(it, str):
            continue
        it = it.strip().strip("-•* ").strip()
        key = _norm(it)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _clean_partial(p: Any) -> dict:
    if not isinstance(p, dict):
        return {}
    out = dict(p)
    for k in ("key_points", "decisions", "open_questions"):
        out[k] = _dedupe(out.get(k) or [])
    return out


def _empty_value(v: str) -> bool:
    return _norm(v) in ("", "none", "n a", "na", "unknown", "not specified", "not mentioned", "tbd", "unassigned")


def _clean_final(n: Any, duration: float) -> dict:
    if not isinstance(n, dict):
        n = {}
    notes: dict[str, Any] = {
        "title": (n.get("title") or "").strip().strip('"').strip()[:120],
        "summary": (n.get("summary") or "").strip(),
        "key_points": _dedupe(n.get("key_points") or []),
        "decisions": _dedupe(n.get("decisions") or []),
        "open_questions": _dedupe(n.get("open_questions") or []),
    }
    actions, seen = [], set()
    for a in n.get("action_items") or []:
        if not isinstance(a, dict) or not (a.get("task") or "").strip():
            continue
        key = _norm(a["task"])
        if key in seen:
            continue
        seen.add(key)
        t = parse_time(a.get("time"))
        actions.append({
            "task": a["task"].strip(),
            "owner": "" if _empty_value(a.get("owner") or "") else (a.get("owner") or "").strip(),
            "due": "" if _empty_value(a.get("due") or "") else (a.get("due") or "").strip(),
            "t": t if t is not None and t <= duration + 5 else None,
            "done": False,
        })
    notes["action_items"] = actions
    chapters = []
    for c in n.get("chapters") or []:
        if not isinstance(c, dict) or not (c.get("title") or "").strip():
            continue
        t = parse_time(c.get("time"))
        if t is None or t > duration + 5:
            continue
        chapters.append({"t": t, "title": c["title"].strip(), "summary": (c.get("summary") or "").strip()})
    chapters.sort(key=lambda c: c["t"])
    deduped = []
    for c in chapters:
        if deduped and (abs(c["t"] - deduped[-1]["t"]) < 5 or _norm(c["title"]) == _norm(deduped[-1]["title"])):
            continue
        deduped.append(c)
    notes["chapters"] = deduped
    sections = []
    for s in n.get("sections") or []:
        if isinstance(s, dict) and s.get("title"):
            items = _dedupe(s.get("items") or [])
            if items:
                sections.append({"title": s["title"], "items": items})
    notes["sections"] = sections
    return notes
