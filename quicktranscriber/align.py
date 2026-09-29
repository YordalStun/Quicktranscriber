"""Combine *what was said* (words with timestamps) with *who said it* (speaker turns)."""

from __future__ import annotations

import bisect
from typing import Any, Optional, Sequence

SENTENCE_END = (".", "?", "!", "…", "。", "？", "！")
MAX_UTTERANCE_SECONDS = 45.0
PAUSE_SPLIT_SECONDS = 1.5


def words_from_transcript(transcript: dict[str, Any]) -> list[list]:
    """Flatten Whisper segments into [start, end, text, prob] words."""
    words: list[list] = []
    for seg in transcript.get("segments", []):
        seg_words = seg.get("words") or []
        if seg_words:
            words.extend([list(w) for w in seg_words])
        elif seg.get("text"):
            words.append([seg["start"], seg["end"], " " + seg["text"].strip(), 1.0])
    words.sort(key=lambda w: w[0])
    # Whisper occasionally emits zero-length or reversed words; keep them sane
    for w in words:
        if w[1] < w[0]:
            w[1] = w[0]
    return words


def assign_speakers(words: Sequence[list], turns: Sequence[Any]) -> list[Optional[int]]:
    """Speaker index for every word (max overlap, else nearest turn in time)."""
    if not turns:
        return [None] * len(words)
    turns = sorted(turns, key=lambda t: t.start)
    starts = [t.start for t in turns]
    out: list[Optional[int]] = []
    for w in words:
        ws, we = w[0], max(w[1], w[0] + 0.02)
        i = bisect.bisect_right(starts, we)
        best, best_ov = None, 0.0
        j = i - 1
        # look back over turns that could still overlap
        while j >= 0 and j >= i - 12:
            t = turns[j]
            ov = min(we, t.end) - max(ws, t.start)
            if ov > best_ov:
                best, best_ov = t.speaker, ov
            j -= 1
        if best is None:
            mid = (ws + we) / 2
            cand = []
            if i - 1 >= 0:
                cand.append((abs(mid - turns[i - 1].end), turns[i - 1].speaker))
            if i < len(turns):
                cand.append((abs(turns[i].start - mid), turns[i].speaker))
            if cand:
                dist, spk = min(cand)
                best = spk if dist < 2.0 else None
        out.append(best)
    # fill gaps from neighbours
    last = None
    for k, s in enumerate(out):
        if s is None:
            out[k] = last
        else:
            last = s
    nxt = None
    for k in range(len(out) - 1, -1, -1):
        if out[k] is None:
            out[k] = nxt
        else:
            nxt = out[k]
    return out


def smooth_speakers(words: Sequence[list], speakers: list[Optional[int]]) -> list[Optional[int]]:
    """Remove implausible one- or two-word speaker switches inside a sentence."""
    n = len(words)
    if n < 3:
        return speakers
    spk = list(speakers)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and spk[j + 1] == spk[i]:
            j += 1
        run_len = j - i + 1
        run_dur = words[j][1] - words[i][0]
        if 0 < i and j < n - 1 and spk[i - 1] == spk[j + 1] and spk[i] != spk[i - 1]:
            prev_text = words[i - 1][2].strip()
            ends_sentence = prev_text.endswith(SENTENCE_END) or words[j][2].strip().endswith(SENTENCE_END)
            if run_len <= 2 and run_dur < 1.0 and not ends_sentence:
                for k in range(i, j + 1):
                    spk[k] = spk[i - 1]
        i = j + 1
    # Snap speaker changes to sentence boundaries when they are 1-2 words off.
    def ends(idx: int) -> bool:
        return words[idx][2].strip().endswith(SENTENCE_END)

    for k in range(1, n):
        if spk[k] == spk[k - 1] or ends(k - 1):
            continue
        # change came too late: "...fine. So | I think" -> "So" belongs to the next speaker
        moved = False
        for shift in (1, 2):
            a = k - shift
            if a <= 0 or any(spk[m] != spk[k - 1] for m in range(a, k)):
                break
            if ends(a - 1) and not any(ends(m) for m in range(a, k)):
                for m in range(a, k):
                    spk[m] = spk[k]
                moved = True
                break
        if moved:
            continue
        # change came too early: "...and that's | fine. I think" -> "fine." belongs to the previous speaker
        for shift in (1, 2):
            b = k + shift - 1
            if b >= n - 1 or any(spk[m] != spk[k] for m in range(k, b + 1)):
                break
            if ends(b) and not any(ends(m) for m in range(k, b)):
                for m in range(k, b + 1):
                    spk[m] = spk[k - 1]
                break
    return spk


def build_utterances(words: Sequence[list], speakers: Sequence[Optional[int]]) -> list[dict[str, Any]]:
    """Group consecutive words of the same speaker into readable paragraphs."""
    utterances: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for w, s in zip(words, speakers):
        start, end = w[0], w[1]
        new = cur is None or s != cur["speaker"]
        if cur is not None and not new:
            pause = start - cur["end"]
            long_enough = cur["end"] - cur["start"] > MAX_UTTERANCE_SECONDS
            last_text = cur["words"][-1][2].strip()
            if pause > PAUSE_SPLIT_SECONDS and last_text.endswith(SENTENCE_END + (",",)):
                new = True
            elif long_enough and last_text.endswith(SENTENCE_END):
                new = True
            elif pause > 4.0:
                new = True
        if new:
            cur = {"start": start, "end": end, "speaker": s, "words": []}
            utterances.append(cur)
        cur["words"].append(w)
        cur["end"] = max(cur["end"], end)
    for u in utterances:
        u["text"] = "".join(x[2] for x in u["words"]).strip()
    return [u for u in utterances if u["text"]]


def align(transcript: dict[str, Any], turns: Sequence[Any]) -> list[dict[str, Any]]:
    words = words_from_transcript(transcript)
    if not words:
        return []
    speakers = assign_speakers(words, turns)
    if turns:
        speakers = smooth_speakers(words, speakers)
    else:
        speakers = [0] * len(words)
    return build_utterances(words, [0 if s is None else s for s in speakers])
