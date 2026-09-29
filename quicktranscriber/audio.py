"""Audio decoding (any format, via the FFmpeg libraries bundled in PyAV)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16000
PEAKS_PER_SECOND = 10

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".webm",
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".3gp", ".amr", ".aiff", ".aif", ".caf", ".mpeg", ".mpga",
}


def probe(path: Path) -> dict:
    import av

    with av.open(str(path)) as container:
        streams = [s for s in container.streams if s.type == "audio"]
        if not streams:
            raise ValueError("This file has no audio track.")
        duration = float(container.duration / 1_000_000) if container.duration else 0.0
        created = container.metadata.get("creation_time") or streams[0].metadata.get("creation_time")
        return {"duration": duration, "created": created, "codec": streams[0].codec_context.name}


def convert_to_wav(
    src: Path,
    dst: Path,
    progress: Optional[Callable[[float], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    """Decode ``src`` to 16 kHz mono 16-bit WAV while computing waveform peaks.

    Streams the audio so even multi-hour recordings use little memory.
    """
    import av

    total = 0.0
    try:
        total = probe(src)["duration"]
    except Exception:  # noqa: BLE001 - duration is only used for progress
        pass

    peaks: list[float] = []
    bucket = SAMPLE_RATE // PEAKS_PER_SECOND
    pending = np.zeros(0, dtype=np.float32)
    written = 0
    sum_sq = 0.0
    tmp = dst.with_suffix(".tmp.wav")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

    with av.open(str(src)) as container, sf.SoundFile(
        str(tmp), mode="w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16", format="WAV"
    ) as out:
        stream = next(s for s in container.streams if s.type == "audio")
        stream.thread_type = "AUTO"

        def emit(frames) -> None:
            nonlocal pending, written, sum_sq
            for frame in frames:
                data = frame.to_ndarray().reshape(-1).astype(np.float32)
                if not len(data):
                    continue
                np.clip(data, -1.0, 1.0, out=data)
                out.write(data)
                written += len(data)
                sum_sq += float(np.dot(data, data))
                pending = np.concatenate([pending, np.abs(data)])
                n = len(pending) // bucket
                if n:
                    peaks.extend(pending[: n * bucket].reshape(n, bucket).max(axis=1).tolist())
                    pending = pending[n * bucket :]

        for i, packet in enumerate(container.demux(stream)):
            try:
                for frame in packet.decode():
                    emit(resampler.resample(frame))
            except av.error.InvalidDataError:
                continue  # skip a corrupt packet rather than failing the whole file
            if i % 200 == 0:
                if cancelled and cancelled():
                    raise InterruptedError("cancelled")
                if progress and total:
                    progress(min(0.99, written / SAMPLE_RATE / total))
        emit(resampler.resample(None))
        if len(pending):
            peaks.append(float(pending.max()))

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise ValueError("No audio could be decoded from this file.")
    tmp.replace(dst)
    duration = written / SAMPLE_RATE
    rms = math.sqrt(sum_sq / written)
    return {"duration": duration, "peaks": _scale_peaks(peaks), "rms": rms}


def _scale_peaks(peaks: list[float]) -> list[int]:
    """Normalise peaks to 0-100 with a gentle curve so quiet speech is still visible."""
    if not peaks:
        return []
    arr = np.asarray(peaks, dtype=np.float32)
    ref = float(np.percentile(arr, 99.5)) or 1.0
    arr = np.clip(arr / ref, 0, 1) ** 0.7
    return [int(round(v * 100)) for v in arr]


def save_peaks(path: Path, peaks: list[int]) -> None:
    path.write_text(json.dumps({"per_second": PEAKS_PER_SECOND, "peaks": peaks}), "utf-8")


def load_wav(path: Path, start: float = 0.0, end: Optional[float] = None) -> np.ndarray:
    with sf.SoundFile(str(path)) as f:
        f.seek(int(start * f.samplerate))
        frames = -1 if end is None else max(0, int((end - start) * f.samplerate))
        data = f.read(frames, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data


def write_clip(src_wav: Path, dst: Path, start: float, end: float) -> None:
    data = load_wav(src_wav, start, end)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), data, SAMPLE_RATE, subtype="PCM_16")


def duration_of(path: Path) -> float:
    with sf.SoundFile(str(path)) as f:
        return f.frames / f.samplerate
