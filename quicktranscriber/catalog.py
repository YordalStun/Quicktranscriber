"""Catalog of downloadable models.

Every model is downloaded into the app folder (models/...) on request.
Sizes are in bytes and were checked against the hosting repositories.
"""

from __future__ import annotations

from typing import Any, Optional

HF = "https://huggingface.co"
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


def _hf(repo: str, files: dict[str, int], revision: str = "main") -> list[dict[str, Any]]:
    return [
        {"url": f"{HF}/{repo}/resolve/{revision}/{name}", "path": name, "size": size}
        for name, size in files.items()
    ]


# ---------------------------------------------------------------------------
# Speech-to-text (Whisper via CTranslate2)
# ---------------------------------------------------------------------------

_WHISPER_TOKENIZER = {"tokenizer.json": 2203239, "vocabulary.txt": 459861}
_WHISPER_EN_TOKENIZER = {"tokenizer.json": 2128466, "vocabulary.txt": 422309}

WHISPER: list[dict[str, Any]] = [
    {
        "key": "base",
        "name": "Whisper Base",
        "tier": "fast",
        "summary": "Very quick drafts. Set the meeting language - auto-detect is unreliable with small models.",
        "accuracy": 2,
        "speed": 5,
        "languages": "99 languages",
        "ram_gb": 1,
        "rtf_cpu": 0.08,
        "rtf_gpu": 0.01,
        "files": _hf("Systran/faster-whisper-base", {"config.json": 2309, "model.bin": 145217532, **_WHISPER_TOKENIZER}),
    },
    {
        "key": "small",
        "name": "Whisper Small",
        "tier": "balanced",
        "summary": "Good for clear audio on slower laptops.",
        "accuracy": 3,
        "speed": 4,
        "languages": "99 languages",
        "ram_gb": 1.5,
        "rtf_cpu": 0.25,
        "rtf_gpu": 0.02,
        "files": _hf("Systran/faster-whisper-small", {"config.json": 2370, "model.bin": 483546902, **_WHISPER_TOKENIZER}),
    },
    {
        "key": "medium",
        "name": "Whisper Medium",
        "tier": "balanced",
        "summary": "Solid accuracy, slower than Turbo on most computers.",
        "accuracy": 4,
        "speed": 2,
        "languages": "99 languages",
        "ram_gb": 2.5,
        "rtf_cpu": 0.8,
        "rtf_gpu": 0.05,
        "files": _hf("Systran/faster-whisper-medium", {"config.json": 2257, "model.bin": 1527906378, **_WHISPER_TOKENIZER}),
    },
    {
        "key": "large-v3-turbo",
        "name": "Whisper Large v3 Turbo",
        "tier": "accurate",
        "summary": "Best balance: near top accuracy at a fraction of the time. Recommended.",
        "accuracy": 5,
        "speed": 3,
        "languages": "99 languages",
        "ram_gb": 2.5,
        "rtf_cpu": 0.6,
        "rtf_gpu": 0.03,
        "files": _hf(
            "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
            {
                "config.json": 2263,
                "model.bin": 1617884929,
                "preprocessor_config.json": 340,
                "tokenizer.json": 2710337,
                "vocabulary.json": 1068114,
            },
        ),
    },
    {
        "key": "large-v3",
        "name": "Whisper Large v3",
        "tier": "max",
        "summary": "Maximum accuracy, especially for accents and less common languages. Slow without an NVIDIA GPU.",
        "accuracy": 5,
        "speed": 1,
        "languages": "99 languages",
        "ram_gb": 4,
        "rtf_cpu": 1.6,
        "rtf_gpu": 0.07,
        "files": _hf(
            "Systran/faster-whisper-large-v3",
            {
                "config.json": 2394,
                "model.bin": 3087284237,
                "preprocessor_config.json": 340,
                "tokenizer.json": 2480617,
                "vocabulary.json": 1068114,
            },
        ),
    },
    {
        "key": "distil-large-v3.5",
        "name": "Distil-Whisper Large v3.5 (English)",
        "tier": "accurate",
        "summary": "English only. Close to Large accuracy, faster on long meetings.",
        "accuracy": 4,
        "speed": 3,
        "languages": "English only",
        "english_only": True,
        "ram_gb": 2.5,
        "rtf_cpu": 0.45,
        "rtf_gpu": 0.025,
        "files": _hf(
            "distil-whisper/distil-large-v3.5-ct2",
            {
                "config.json": 2690,
                "model.bin": 1512927867,
                "preprocessor_config.json": 340,
                "tokenizer.json": 2480645,
                "vocabulary.json": 1068114,
            },
        ),
    },
    {
        "key": "small.en",
        "name": "Whisper Small (English)",
        "tier": "balanced",
        "summary": "English only. A little more accurate than Small for English.",
        "accuracy": 3,
        "speed": 4,
        "languages": "English only",
        "english_only": True,
        "ram_gb": 1.5,
        "rtf_cpu": 0.25,
        "rtf_gpu": 0.02,
        "files": _hf("Systran/faster-whisper-small.en", {"config.json": 2657, "model.bin": 483545366, **_WHISPER_EN_TOKENIZER}),
    },
]

# ---------------------------------------------------------------------------
# Speaker recognition (diarization)
# ---------------------------------------------------------------------------

SEGMENTATION = {
    "key": "pyannote-segmentation-3.0",
    "name": "Speaker change detector (pyannote 3.0)",
    "files": [
        {
            "url": f"{SHERPA}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
            "path": "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
            "size": 6958444,
            "extract": True,
        }
    ],
    "model_file": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
}

SPEAKER_EMBEDDINGS: list[dict[str, Any]] = [
    {
        "key": "titanet_small",
        "name": "TitaNet Small (NVIDIA NeMo)",
        "summary": "Fast and accurate voice fingerprints. Recommended.",
        "accuracy": 4,
        "speed": 5,
        "file": "nemo_en_titanet_small.onnx",
        "size": 40257283,
        "threshold": 0.75,
        "match": {"auto": 0.62, "suggest": 0.48},
    },
    {
        "key": "titanet_large",
        "name": "TitaNet Large (NVIDIA NeMo)",
        "summary": "Slightly more accurate, about twice as slow.",
        "accuracy": 5,
        "speed": 3,
        "file": "nemo_en_titanet_large.onnx",
        "size": 101405493,
        "threshold": 0.75,
        "match": {"auto": 0.62, "suggest": 0.48},
    },
    {
        "key": "eres2net_en",
        "name": "ERes2Net (3D-Speaker, VoxCeleb)",
        "summary": "Very accurate alternative, about twice as slow.",
        "accuracy": 5,
        "speed": 3,
        "file": "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
        "size": 26485263,
        "threshold": 0.75,
        "match": {"auto": 0.62, "suggest": 0.48},
    },
]
for _m in SPEAKER_EMBEDDINGS:
    _m["files"] = [
        {"url": f"{SHERPA}/speaker-recongition-models/{_m['file']}", "path": _m["file"], "size": _m["size"]}
    ]

# ---------------------------------------------------------------------------
# Local AI models for notes (GGUF, run with llama.cpp)
# ---------------------------------------------------------------------------

LLM: list[dict[str, Any]] = [
    {
        "key": "qwen3.5-2b",
        "name": "Qwen 3.5 2B",
        "maker": "Alibaba Qwen",
        "tier": "light",
        "summary": "Runs on almost anything. Basic but useful notes.",
        "quality": 2,
        "speed": 5,
        "context": 262144,
        "thinking": "optional",
        "files": _hf("unsloth/Qwen3.5-2B-GGUF", {"Qwen3.5-2B-Q4_K_M.gguf": 1280835840}),
    },
    {
        "key": "llama3.2-3b",
        "name": "Llama 3.2 3B",
        "maker": "Meta",
        "tier": "light",
        "summary": "Small, reliable classic for older laptops.",
        "quality": 2,
        "speed": 4,
        "context": 131072,
        "files": _hf("bartowski/Llama-3.2-3B-Instruct-GGUF", {"Llama-3.2-3B-Instruct-Q4_K_M.gguf": 2019377696}),
    },
    {
        "key": "qwen3.5-4b",
        "name": "Qwen 3.5 4B",
        "maker": "Alibaba Qwen",
        "tier": "balanced",
        "summary": "Great notes for its size. Recommended for most laptops.",
        "quality": 3,
        "speed": 4,
        "context": 262144,
        "thinking": "optional",
        "files": _hf("unsloth/Qwen3.5-4B-GGUF", {"Qwen3.5-4B-Q4_K_M.gguf": 2740937888}),
    },
    {
        "key": "gemma4-e4b",
        "name": "Gemma 4 E4B",
        "maker": "Google",
        "tier": "balanced",
        "summary": "Strong writing, 140+ languages.",
        "quality": 3,
        "speed": 3,
        "context": 131072,
        "files": _hf("ggml-org/gemma-4-E4B-it-GGUF", {"gemma-4-E4B-it-Q4_0.gguf": 4590807392}),
    },
    {
        "key": "qwen3.5-9b",
        "name": "Qwen 3.5 9B",
        "maker": "Alibaba Qwen",
        "tier": "quality",
        "summary": "Detailed, accurate notes. Best with an NVIDIA GPU (8 GB+) or 16 GB RAM.",
        "quality": 4,
        "speed": 3,
        "context": 262144,
        "thinking": "optional",
        "files": _hf("unsloth/Qwen3.5-9B-GGUF", {"Qwen3.5-9B-Q4_K_M.gguf": 5680522464}),
    },
    {
        "key": "gemma4-12b",
        "name": "Gemma 4 12B",
        "maker": "Google",
        "tier": "quality",
        "summary": "Excellent summaries and multilingual meetings. Needs 16 GB RAM or a 10 GB+ GPU.",
        "quality": 4,
        "speed": 2,
        "context": 262144,
        "files": _hf("ggml-org/gemma-4-12B-it-GGUF", {"gemma-4-12B-it-Q4_0.gguf": 7219673216}),
    },
    {
        "key": "gpt-oss-20b",
        "name": "gpt-oss 20B",
        "maker": "OpenAI",
        "tier": "best",
        "summary": "Fast for its size (mixture of experts). Needs 16 GB+ GPU or 24 GB RAM.",
        "quality": 5,
        "speed": 3,
        "context": 131072,
        "files": _hf("ggml-org/gpt-oss-20b-GGUF", {"gpt-oss-20b-MXFP4.gguf": 12109566624}),
    },
    {
        "key": "gemma4-26b-a4b",
        "name": "Gemma 4 26B A4B",
        "maker": "Google",
        "tier": "best",
        "summary": "Top quality, mixture of experts so still quick. Needs 24 GB+ RAM or GPU memory.",
        "quality": 5,
        "speed": 3,
        "context": 262144,
        "files": _hf("ggml-org/gemma-4-26B-A4B-it-GGUF", {"gemma-4-26B-A4B-it-Q4_0.gguf": 14618145824}),
    },
    {
        "key": "qwen3.6-35b-a3b",
        "name": "Qwen 3.6 35B A3B",
        "maker": "Alibaba Qwen",
        "tier": "best",
        "summary": "Excellent long-meeting notes; fast mixture of experts. Needs 32 GB RAM or a 24 GB GPU.",
        "quality": 5,
        "speed": 3,
        "context": 262144,
        "thinking": "optional",
        "files": _hf("ggml-org/Qwen3.6-35B-A3B-GGUF", {"Qwen3.6-35B-A3B-Q4_K_M.gguf": 20419565568}),
    },
]

TIER_ORDER = {"light": 0, "fast": 0, "balanced": 1, "accurate": 2, "quality": 2, "max": 3, "best": 3}


def size_of(entry: dict[str, Any]) -> int:
    return sum(f["size"] for f in entry.get("files", []))


def llm_ram_gb(entry: dict[str, Any]) -> float:
    """Rough memory needed to run a GGUF model with a modest context."""
    return round(size_of(entry) / 1e9 * 1.15 + 1.2, 1)


def whisper(key: str) -> Optional[dict[str, Any]]:
    return next((m for m in WHISPER if m["key"] == key), None)


def speaker_model(key: str) -> Optional[dict[str, Any]]:
    return next((m for m in SPEAKER_EMBEDDINGS if m["key"] == key), None)


def llm(key: str) -> Optional[dict[str, Any]]:
    return next((m for m in LLM if m["key"] == key), None)


def recommend(hw: dict[str, Any]) -> dict[str, str]:
    """Pick sensible defaults for this computer (the user can always choose others)."""
    tier = hw.get("tier", "cpu-mid")
    ram = hw.get("ram_gb", 8)
    vram = hw.get("vram_gb", 0)

    if tier.startswith("gpu") and vram >= 5.5:
        whisper_key = "large-v3"
    elif ram >= 5.5:
        whisper_key = "large-v3-turbo"
    else:
        whisper_key = "small"

    if vram >= 20:
        llm_key = "gemma4-26b-a4b"
    elif vram >= 14:
        llm_key = "gpt-oss-20b"
    elif vram >= 9:
        llm_key = "gemma4-12b"
    elif vram >= 7:
        llm_key = "qwen3.5-9b"
    elif ram >= 30:
        llm_key = "qwen3.6-35b-a3b" if hw.get("apple_silicon") else "qwen3.5-9b"
    elif ram >= 15:
        llm_key = "qwen3.5-9b" if hw.get("apple_silicon") else "qwen3.5-4b"
    elif ram >= 7:
        llm_key = "qwen3.5-4b"
    else:
        llm_key = "qwen3.5-2b"
    return {"whisper": whisper_key, "llm": llm_key, "speaker": "titanet_small"}


LANGUAGES = {
    "auto": "Detect automatically",
    "en": "English", "cy": "Welsh", "ga": "Irish", "gd": "Scottish Gaelic", "es": "Spanish",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "cs": "Czech", "sk": "Slovak", "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian",
    "el": "Greek", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish", "ar": "Arabic",
    "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "hi": "Hindi", "bn": "Bengali",
    "pa": "Punjabi", "ta": "Tamil", "te": "Telugu", "mr": "Marathi", "gu": "Gujarati",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian", "ms": "Malay", "tl": "Tagalog", "sw": "Swahili", "af": "Afrikaans",
    "hr": "Croatian", "sr": "Serbian", "sl": "Slovenian", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "is": "Icelandic", "ca": "Catalan", "eu": "Basque", "gl": "Galician",
}
