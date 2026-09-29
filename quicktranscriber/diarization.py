"""Speaker diarization: working out *who spoke when*.

This is a numpy re-implementation of the pyannote 3.x pipeline, built on two
small ONNX models so it runs anywhere without PyTorch:

1. **Local segmentation** - ``pyannote/segmentation-3.0`` looks at 10 second
   windows (sliding by ``step`` seconds) and says, frame by frame, which of up to
   three "local" speakers is talking.
2. **Embeddings** - for every (window, local speaker) pair we compute a voice
   fingerprint from the audio where only that speaker is talking.
3. **Clustering** - fingerprints are grouped into global speakers with
   centroid-linkage agglomerative clustering (tiny clusters are folded into the
   nearest large one). Learned voiceprints can merge clusters that belong to
   the same known person.
4. **Reconstruction** - the overlapping windows are stitched back into one
   timeline.

Steps 1-2 are the slow part. Their output is cached in ``DiarizationData`` so
steps 3-4 can be re-run instantly, e.g. when the user says "there were
actually 3 people in this meeting".
"""

from __future__ import annotations

import dataclasses
import math
from typing import Callable, Optional, Sequence

import numpy as np

SAMPLE_RATE = 16000
WINDOW = 10 * SAMPLE_RATE  # samples per segmentation window
FRAME_STEP = 270  # samples between two segmentation frames (16.875 ms)
FRAME_SIZE = 991  # receptive field of one frame in samples
LOCAL_SPEAKERS = 3

# pyannote "powerset" classes: {}, {0}, {1}, {2}, {0,1}, {0,2}, {1,2}
POWERSET = np.array(
    [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [1, 0, 1], [0, 1, 1]],
    dtype=np.uint8,
)

# Minimum amount of audio (seconds) needed to trust a voice fingerprint.
MIN_EMBEDDING_SECONDS = 0.4

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


class Cancelled(Exception):
    """Raised when the user cancels a running job."""


@dataclasses.dataclass
class DiarizationData:
    """Cached output of the expensive analysis steps."""

    binarized: np.ndarray  # (chunks, frames, 3) uint8 local speaker activity
    embeddings: np.ndarray  # (chunks, 3, dim) float32, NaN where unavailable
    step: int  # samples between window starts
    num_samples: int
    embedding_model: str = ""

    @property
    def num_chunks(self) -> int:
        return int(self.binarized.shape[0])

    @property
    def num_frames(self) -> int:
        return int(self.binarized.shape[1])

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            binarized=self.binarized.astype(np.uint8),
            embeddings=self.embeddings.astype(np.float32),
            step=np.int64(self.step),
            num_samples=np.int64(self.num_samples),
            embedding_model=np.array(self.embedding_model),
        )

    @classmethod
    def load(cls, path: str) -> "DiarizationData":
        with np.load(path, allow_pickle=False) as z:
            return cls(
                binarized=z["binarized"],
                embeddings=z["embeddings"],
                step=int(z["step"]),
                num_samples=int(z["num_samples"]),
                embedding_model=str(z["embedding_model"]),
            )


@dataclasses.dataclass
class Turn:
    start: float
    end: float
    speaker: int


@dataclasses.dataclass
class ClusterResult:
    turns: list[Turn]
    centroids: np.ndarray  # (speakers, dim), unit length
    hard_clusters: np.ndarray  # (chunks, 3) global speaker per local speaker, -2 = none
    frame_activity: np.ndarray  # (global frames, speakers) binary
    speaking_time: list[float]  # seconds per speaker


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Segmenter:
    """Runs pyannote/segmentation-3.0 exported to ONNX."""

    def __init__(self, model_path: str, num_threads: int = 2):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, num_threads)
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            model_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def __call__(self, batch: np.ndarray) -> np.ndarray:
        """(batch, WINDOW) float32 -> (batch, frames, 3) uint8 speaker activity."""
        logits = self.session.run(None, {self.input_name: batch[:, None, :]})[0]
        return POWERSET[np.argmax(logits, axis=-1)]


class EmbeddingExtractor:
    """Voice fingerprints via sherpa-onnx (WeSpeaker / 3D-Speaker / NeMo models)."""

    def __init__(self, model_path: str, num_threads: int = 2):
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=model_path, num_threads=max(1, num_threads), provider="cpu"
        )
        if not config.validate():
            raise RuntimeError(f"Invalid speaker embedding model: {model_path}")
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dim = int(self.extractor.dim)

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(samples, dtype=np.float32))
        stream.input_finished()
        emb = np.asarray(self.extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        return emb / norm if norm > 0 else emb

    def embed_long(self, samples: np.ndarray, max_seconds: float = 10.0) -> Optional[np.ndarray]:
        """Fingerprint for audio of any length (averaged over <= max_seconds pieces)."""
        min_len = int(MIN_EMBEDDING_SECONDS * SAMPLE_RATE)
        if len(samples) < min_len:
            return None
        piece = int(max_seconds * SAMPLE_RATE)
        embs = []
        for start in range(0, len(samples), piece):
            part = samples[start : start + piece]
            if len(part) >= min_len:
                embs.append(self(part))
        if not embs:
            return None
        return normalize(np.mean(embs, axis=0))


# ---------------------------------------------------------------------------
# Analysis (slow part)
# ---------------------------------------------------------------------------


def chunk_starts(num_samples: int, step: int) -> list[int]:
    if num_samples <= WINDOW:
        return [0]
    num_full = (num_samples - WINDOW) // step + 1
    starts = [i * step for i in range(num_full)]
    if (num_samples - WINDOW) % step > 0:
        starts.append(num_full * step)
    return starts


def _window(audio: np.ndarray, start: int) -> np.ndarray:
    chunk = audio[start : start + WINDOW]
    if len(chunk) < WINDOW:
        chunk = np.pad(chunk, (0, WINDOW - len(chunk)))
    return chunk


def _frames_to_sample_mask(frame_mask: np.ndarray, num_samples: int = WINDOW) -> np.ndarray:
    """Expand a per-frame mask to a per-sample mask (each sample -> nearest frame centre)."""
    idx = (np.arange(num_samples) - FRAME_SIZE // 2 + FRAME_STEP // 2) // FRAME_STEP
    idx = np.clip(idx, 0, len(frame_mask) - 1)
    return frame_mask[idx]


def analyze(
    audio: np.ndarray,
    segmenter: Segmenter,
    extractor: EmbeddingExtractor,
    step_seconds: float = 2.0,
    batch_size: int = 16,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
    embedding_model: str = "",
) -> DiarizationData:
    """Segmentation + embeddings for a whole recording (16 kHz mono float32)."""
    audio = np.asarray(audio, dtype=np.float32)
    step = max(FRAME_STEP, int(round(step_seconds * SAMPLE_RATE)))
    starts = chunk_starts(len(audio), step)
    n = len(starts)

    # 1. local segmentation, batched (cheap: ~20% of the time)
    binarized = None
    for b in range(0, n, batch_size):
        if cancelled and cancelled():
            raise Cancelled()
        batch = np.stack([_window(audio, s) for s in starts[b : b + batch_size]])
        seg = segmenter(batch)
        if binarized is None:
            binarized = np.zeros((n, seg.shape[1], LOCAL_SPEAKERS), dtype=np.uint8)
        binarized[b : b + len(batch)] = seg
        if progress:
            progress(0.2 * min(n, b + batch_size) / n, "Listening for speaker changes")

    # 2. one embedding per active local speaker (expensive)
    embeddings = np.full((n, LOCAL_SPEAKERS, extractor.dim), np.nan, dtype=np.float32)
    min_frames = int(math.ceil(MIN_EMBEDDING_SECONDS * SAMPLE_RATE / FRAME_STEP))
    for c, start in enumerate(starts):
        if cancelled and c % 8 == 0 and cancelled():
            raise Cancelled()
        seg = binarized[c]
        if not seg.any():
            continue
        chunk = _window(audio, start)
        overlap = seg.sum(axis=1) >= 2
        for s in range(LOCAL_SPEAKERS):
            mask = seg[:, s].astype(bool)
            if mask.sum() < min_frames:
                continue
            clean = mask & ~overlap
            use = clean if clean.sum() >= min_frames else mask
            samples = chunk[_frames_to_sample_mask(use)]
            if len(samples) < MIN_EMBEDDING_SECONDS * SAMPLE_RATE:
                continue
            embeddings[c, s] = extractor(samples)
        if progress and (c % 10 == 0 or c == n - 1):
            progress(0.2 + 0.8 * (c + 1) / n, "Learning voice fingerprints")

    return DiarizationData(
        binarized=binarized,
        embeddings=embeddings,
        step=step,
        num_samples=len(audio),
        embedding_model=embedding_model,
    )


# ---------------------------------------------------------------------------
# Clustering (fast part)
# ---------------------------------------------------------------------------


def normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norm = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(norm, 1e-8)


def _ahc(
    emb: np.ndarray,
    threshold: float,
    min_cluster_size: int,
    min_clusters: int,
    max_clusters: int,
    num_clusters: Optional[int],
) -> np.ndarray:
    """pyannote-style agglomerative clustering on unit-length embeddings."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import cdist

    n = len(emb)
    if n == 1:
        return np.zeros(1, dtype=int)
    min_cluster_size = min(min_cluster_size, max(1, round(0.1 * n)))
    dendrogram = linkage(emb, method="centroid", metric="euclidean")
    clusters = fcluster(dendrogram, threshold, criterion="distance") - 1
    uniq, counts = np.unique(clusters, return_counts=True)
    large = uniq[counts >= min_cluster_size]
    num_large = len(large)

    if num_large < min_clusters:
        num_clusters = min_clusters
    elif num_large > max_clusters:
        num_clusters = max_clusters

    if num_clusters is not None and num_large != num_clusters:
        # walk the dendrogram outwards from the threshold until we hit the target
        iter_dendrogram = np.copy(dendrogram)
        iter_dendrogram[:, 2] = np.arange(n - 1)
        best_iter, best_num = n - 1, 1
        for it in np.argsort(np.abs(dendrogram[:, 2] - threshold)):
            if iter_dendrogram[it, 3] < min_cluster_size:
                continue
            cl = fcluster(iter_dendrogram, it, criterion="distance") - 1
            u, cnt = np.unique(cl, return_counts=True)
            nl = int(np.sum(cnt >= min_cluster_size))
            if abs(nl - num_clusters) < abs(best_num - num_clusters):
                best_iter, best_num = it, nl
            if nl == num_clusters:
                break
        clusters = fcluster(iter_dendrogram, best_iter, criterion="distance") - 1
        uniq, counts = np.unique(clusters, return_counts=True)
        large = uniq[counts >= min_cluster_size]

    if len(large) == 0:
        return np.zeros(n, dtype=int)
    small = uniq[counts < min_cluster_size]
    if len(small):
        large_c = normalize(np.vstack([emb[clusters == k].mean(0) for k in large]))
        small_c = normalize(np.vstack([emb[clusters == k].mean(0) for k in small]))
        nearest = np.argmin(cdist(large_c, small_c, metric="cosine"), axis=0)
        for i, k in enumerate(small):
            clusters[clusters == k] = large[nearest[i]]
    _, clusters = np.unique(clusters, return_inverse=True)
    return clusters


def _constrained_argmax(scores: np.ndarray) -> np.ndarray:
    """Per window, give each local speaker a different global speaker (Hungarian)."""
    from scipy.optimize import linear_sum_assignment

    num_chunks, num_local, _ = scores.shape
    hard = np.full((num_chunks, num_local), -2, dtype=np.int32)
    for c in range(num_chunks):
        rows, cols = linear_sum_assignment(scores[c], maximize=True)
        hard[c, rows] = cols
    return hard


def cluster(
    data: DiarizationData,
    num_speakers: Optional[int] = None,
    min_speakers: int = 1,
    max_speakers: int = 20,
    threshold: float = 0.75,
    min_cluster_size: int = 12,
    max_embeddings: int = 4000,
    anchors: Optional[np.ndarray] = None,
    anchor_threshold: float = 0.6,
    min_talk_seconds: float = 60.0,
) -> ClusterResult:
    """Group fingerprints into speakers and rebuild the timeline.

    ``anchors`` are learned voiceprints (unit vectors). Clusters that match the
    same voiceprint above ``anchor_threshold`` are merged, which fixes the
    classic failure of one person being split into two speakers.
    """
    emb = data.embeddings
    num_chunks = data.num_chunks
    active = data.binarized.sum(axis=1) > 0  # (chunks, 3)
    valid = ~np.isnan(emb).any(axis=2)
    chunk_idx, spk_idx = np.nonzero(active & valid)

    if len(chunk_idx) == 0:
        return _empty_result(data)

    # min cluster size was tuned for 1 s steps; scale with the step used
    step_seconds = data.step / SAMPLE_RATE
    mcs = max(2, int(round(min_cluster_size / max(step_seconds, 1.0))))

    train_chunk, train_spk = chunk_idx, spk_idx
    if len(chunk_idx) > max_embeddings:
        pick = np.linspace(0, len(chunk_idx) - 1, max_embeddings).round().astype(int)
        train_chunk, train_spk = chunk_idx[pick], spk_idx[pick]
        mcs = max(2, int(round(mcs * max_embeddings / len(chunk_idx))))
    train = normalize(emb[train_chunk, train_spk])

    if num_speakers:
        min_speakers = max_speakers = num_speakers
    min_speakers = max(1, min(min_speakers, len(train)))
    max_speakers = max(min_speakers, min(max_speakers, len(train)))

    if max_speakers < 2:
        labels = np.zeros(len(train), dtype=int)
    else:
        labels = _ahc(
            train,
            threshold,
            mcs,
            min_speakers,
            max_speakers,
            num_speakers if num_speakers else None,
        )

    centroids = normalize(np.vstack([train[labels == k].mean(0) for k in range(labels.max() + 1)]))

    # Merge clusters that match the same learned voice (only in "auto" mode)
    if anchors is not None and len(anchors) and not num_speakers and len(centroids) > 1:
        centroids = _merge_by_anchors(centroids, normalize(anchors), anchor_threshold, min_speakers)

    flat = normalize(np.nan_to_num(emb.reshape(-1, emb.shape[-1])))
    frame_seconds = FRAME_STEP / SAMPLE_RATE

    def assign(cents: np.ndarray):
        # every fingerprint (not just the training subset) goes to its nearest centroid
        scores = (flat @ cents.T).reshape(num_chunks, LOCAL_SPEAKERS, len(cents))
        scores[~valid] = -1.0
        hard_ = _constrained_argmax(scores)
        hard_[~active] = -2
        # local speakers with activity but no usable fingerprint: leave unassigned
        hard_[active & ~valid] = -2
        # drop speakers that ended up with no windows at all, renumber
        used_ = sorted({int(k) for k in np.unique(hard_) if k >= 0})
        remap = {k: i for i, k in enumerate(used_)}
        if used_:
            hard_ = np.vectorize(lambda k: remap.get(int(k), -2))(hard_).astype(np.int32)
        cents = cents[used_] if used_ else cents[:0]
        act = _reconstruct(data, hard_, max(1, len(used_)))
        return hard_, cents, act

    hard, centroids, activity = assign(centroids)
    # Tiny clusters are almost always fragments of a real speaker (a laugh, a cough,
    # crosstalk). Fold them into the nearest real speaker, like pyannote's
    # min_cluster_size but measured in speaking time.
    for _ in range(3):
        if num_speakers or len(centroids) <= max(1, min_speakers):
            break
        talk = activity.sum(axis=0) * frame_seconds
        limit = min(min_talk_seconds, 0.03 * float(talk.sum()))
        small = [k for k in range(len(centroids)) if talk[k] < limit]
        if not small or len(centroids) - len(small) < max(1, min_speakers):
            break
        keep = [k for k in range(len(centroids)) if k not in small]
        hard, centroids, activity = assign(centroids[keep])

    turns = _activity_to_turns(activity)
    # order speakers by first appearance: "Speaker 1" talks first
    order = []
    for t in sorted(turns, key=lambda t: t.start):
        if t.speaker not in order:
            order.append(t.speaker)
    order += [k for k in range(activity.shape[1]) if k not in order]
    rank = {k: i for i, k in enumerate(order)}
    turns = [Turn(t.start, t.end, rank[t.speaker]) for t in turns]
    activity = activity[:, order]
    if len(centroids):
        centroids = centroids[[k for k in order if k < len(centroids)]]
    hard = np.where(hard >= 0, np.vectorize(lambda k: rank.get(int(k), -2))(hard), -2).astype(np.int32)

    speaking = [float(activity[:, k].sum() * frame_seconds) for k in range(activity.shape[1])]
    return ClusterResult(
        turns=sorted(turns, key=lambda t: (t.start, t.speaker)),
        centroids=centroids,
        hard_clusters=hard,
        frame_activity=activity,
        speaking_time=speaking,
    )


def _merge_by_anchors(
    centroids: np.ndarray, anchors: np.ndarray, threshold: float, min_speakers: int
) -> np.ndarray:
    sims = centroids @ anchors.T  # (clusters, anchors)
    best = sims.argmax(axis=1)
    best_sim = sims.max(axis=1)
    groups: dict[int, list[int]] = {}
    for k in range(len(centroids)):
        if best_sim[k] >= threshold:
            groups.setdefault(int(best[k]), []).append(k)
    merged = [c for c in range(len(centroids))]
    new_centroids = []
    taken = set()
    for members in groups.values():
        if len(members) > 1 and len(centroids) - len(members) + 1 >= min_speakers:
            new_centroids.append(normalize(centroids[members].mean(0)))
            taken.update(members)
    for k in merged:
        if k not in taken:
            new_centroids.append(centroids[k])
    return normalize(np.vstack(new_centroids))


def _empty_result(data: DiarizationData) -> ClusterResult:
    total_frames = _total_frames(data)
    return ClusterResult(
        turns=[],
        centroids=np.zeros((0, data.embeddings.shape[-1]), dtype=np.float32),
        hard_clusters=np.full((data.num_chunks, LOCAL_SPEAKERS), -2, dtype=np.int32),
        frame_activity=np.zeros((total_frames, 0), dtype=np.uint8),
        speaking_time=[],
    )


def _chunk_offsets(data: DiarizationData) -> np.ndarray:
    starts = np.array(chunk_starts(data.num_samples, data.step))
    return np.round(starts / FRAME_STEP).astype(int)


def _total_frames(data: DiarizationData) -> int:
    offsets = _chunk_offsets(data)
    return int(offsets[-1] + data.num_frames)


def speaker_count(data: DiarizationData) -> np.ndarray:
    """Number of simultaneous speakers per global frame (overlap-averaged)."""
    offsets = _chunk_offsets(data)
    total = _total_frames(data)
    acc = np.zeros(total, dtype=np.float32)
    cnt = np.zeros(total, dtype=np.float32)
    per_chunk = data.binarized.sum(axis=2).astype(np.float32)
    for c, off in enumerate(offsets):
        acc[off : off + data.num_frames] += per_chunk[c]
        cnt[off : off + data.num_frames] += 1
    return np.rint(acc / np.maximum(cnt, 1)).astype(np.int32)


def _reconstruct(data: DiarizationData, hard: np.ndarray, num_speakers: int) -> np.ndarray:
    offsets = _chunk_offsets(data)
    total = _total_frames(data)
    activations = np.zeros((total, num_speakers), dtype=np.float32)
    for c, off in enumerate(offsets):
        seg = data.binarized[c]
        for k in np.unique(hard[c]):
            if k < 0:
                continue
            activations[off : off + data.num_frames, k] += seg[:, hard[c] == k].max(axis=1)
    count = np.minimum(speaker_count(data), num_speakers)
    binary = np.zeros_like(activations, dtype=np.uint8)
    order = np.argsort(-activations, axis=1)
    for rank in range(int(count.max()) if len(count) else 0):
        rows = np.nonzero(count > rank)[0]
        cols = order[rows, rank]
        ok = activations[rows, cols] > 0
        binary[rows[ok], cols[ok]] = 1
    # trim padding beyond the end of the audio
    last = int(math.ceil(data.num_samples / FRAME_STEP))
    return binary[:last]


def _activity_to_turns(
    activity: np.ndarray, min_duration_on: float = 0.1, min_duration_off: float = 0.1
) -> list[Turn]:
    fs = FRAME_STEP / SAMPLE_RATE
    centre = (FRAME_SIZE / 2) / SAMPLE_RATE
    turns: list[Turn] = []
    for k in range(activity.shape[1]):
        col = activity[:, k].astype(np.int8)
        if not col.any():
            continue
        edges = np.diff(np.concatenate([[0], col, [0]]))
        on = np.nonzero(edges == 1)[0]
        off = np.nonzero(edges == -1)[0]
        spans = []
        for a, b in zip(on, off):
            start = a * fs + centre - fs / 2
            end = b * fs + centre - fs / 2
            if spans and start - spans[-1][1] < min_duration_off:
                spans[-1][1] = end
            else:
                spans.append([start, end])
        for start, end in spans:
            if end - start >= min_duration_on:
                turns.append(Turn(round(max(0.0, start), 3), round(end, 3), k))
    return turns


def speaker_segments_embeddings(
    audio: np.ndarray,
    turns: Sequence[Turn],
    extractor: EmbeddingExtractor,
    min_seconds: float = 1.0,
    max_seconds: float = 12.0,
    cancelled: Optional[CancelFn] = None,
) -> list[tuple[int, np.ndarray]]:
    """Fingerprint each sufficiently long turn (used for learning + corrections)."""
    out = []
    for i, t in enumerate(turns):
        if cancelled and i % 16 == 0 and cancelled():
            raise Cancelled()
        dur = t.end - t.start
        if dur < min_seconds:
            continue
        mid = (t.start + t.end) / 2
        half = min(dur, max_seconds) / 2
        a = int((mid - half) * SAMPLE_RATE)
        b = int((mid + half) * SAMPLE_RATE)
        emb = extractor.embed_long(audio[a:b])
        if emb is not None:
            out.append((i, emb))
    return out
