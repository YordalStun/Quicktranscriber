"""Talking to a local AI model.

Three kinds of backend are supported, all running on this computer:

* ``builtin`` - llama.cpp's llama-server, downloaded into the app folder (default)
* ``ollama``  - an existing Ollama installation
* ``openai``  - any OpenAI-compatible local server (LM Studio, Jan, vLLM, ...)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import requests

from .. import catalog, hardware, paths, settings
from . import runtime

log = logging.getLogger("qt.llm")

THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
GEMMA_THOUGHT_RE = re.compile(r"<\|channel\>thought.*?<channel\|>", re.S)


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """No AI model is set up yet (not an error in the user's data)."""


def clean_output(text: str) -> str:
    text = THINK_RE.sub("", text or "")
    text = GEMMA_THOUGHT_RE.sub("", text)
    if "<think>" in text and "</think>" not in text:  # reasoning cut off
        text = text.split("<think>")[0]
    return text.strip()


def parse_json(text: str) -> Any:
    text = clean_output(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        snippet = re.sub(r",\s*([}\]])", r"\1", snippet)  # trailing commas
        try:
            return json.loads(snippet)
        except ValueError:
            pass
    raise LLMError("The AI model did not return valid structured notes.")


# ---------------------------------------------------------------------------
# Model resolution for the built-in engine
# ---------------------------------------------------------------------------


def custom_models() -> list[dict[str, Any]]:
    """GGUF files the user dropped into models/llm (outside catalog folders)."""
    out = []
    catalog_dirs = {m["key"] for m in catalog.LLM}
    if not paths.LLM_MODELS.exists():
        return out
    for f in sorted(paths.LLM_MODELS.rglob("*.gguf")):
        rel = f.relative_to(paths.LLM_MODELS)
        if rel.parts[0] in catalog_dirs:
            continue
        if f.name.startswith("mmproj") or ".part" in f.name or re.search(r"-0000[2-9]-of-", f.name):
            continue
        out.append({"key": "file:" + rel.as_posix(), "name": f.stem, "path": f, "size": f.stat().st_size})
    return out


def model_file(key: str) -> Optional[Path]:
    if key.startswith("file:"):
        p = paths.LLM_MODELS / key[5:]
        return p if p.exists() else None
    entry = catalog.llm(key)
    if not entry:
        return None
    p = paths.LLM_MODELS / key / entry["files"][0]["path"]
    return p if p.exists() else None


def installed_llm_keys() -> list[str]:
    keys = [m["key"] for m in catalog.LLM if model_file(m["key"])]
    keys += [m["key"] for m in custom_models()]
    return keys


def default_context(model_key: str, gpu: bool) -> int:
    configured = int(settings.get("llm_context") or 0)
    entry = catalog.llm(model_key) if not model_key.startswith("file:") else None
    max_ctx = entry.get("context", 32768) if entry else 32768
    if configured > 0:
        return min(configured, max_ctx)
    hw = hardware.info()
    if gpu and hw["vram_gb"] >= 16:
        ctx = 32768
    elif gpu and hw["vram_gb"] >= 8:
        ctx = 16384
    elif hw["ram_gb"] >= 15 or hw.get("apple_silicon"):
        ctx = 16384
    elif hw["ram_gb"] >= 7:
        ctx = 8192
    else:
        ctx = 4096
    return min(ctx, max_ctx)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class Backend:
    name = "backend"
    supports_schema = True
    presence_penalty = 0.6  # discourages small models from repeating themselves

    def __init__(self, model: str):
        self.model = model

    # to implement ------------------------------------------------------------
    def prepare(self, cancelled=None, on_status=None) -> None: ...

    def context_size(self) -> int:
        return 8192

    def count_tokens(self, text: str) -> int:
        return int(len(text) / 3.6) + 1

    def _request(self, messages: list[dict], schema: Optional[dict], max_tokens: int, temperature: float,
                 stream: bool) -> requests.Response:
        raise NotImplementedError

    def _parse(self, data: dict) -> str:
        raise NotImplementedError

    def _parse_stream(self, resp: requests.Response,
                      on_progress: Optional[Callable[[float], None]] = None) -> Iterator[str]:
        """Yield text pieces; ``on_progress`` gets prompt-reading progress (0-1) if the engine reports it."""
        raise NotImplementedError

    def describe(self) -> str:
        return self.model

    def comfortable_tokens(self) -> int:
        return 8000

    # shared ----------------------------------------------------------------------
    def chat(self, messages: list[dict], schema: Optional[dict] = None, max_tokens: int = 1500,
             temperature: float = 0.3, on_token: Optional[Callable[[str], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             on_progress: Optional[Callable[[float], None]] = None) -> str:
        stream = on_token is not None or cancelled is not None or on_progress is not None
        use_schema = schema if self.supports_schema else None
        msgs = messages
        if schema and not use_schema:
            msgs = _with_json_instruction(messages, schema)
        try:
            resp = self._request(msgs, use_schema, max_tokens, temperature, stream)
        except requests.RequestException as exc:
            raise LLMError(f"Could not reach the AI engine: {exc}") from exc
        if resp.status_code >= 400 and use_schema is not None:
            # engine could not handle the schema: fall back to plain JSON instructions
            log.warning("Schema request failed (%s): %s", resp.status_code, resp.text[:300])
            resp = self._request(_with_json_instruction(messages, schema), None, max_tokens, temperature, stream)
        if resp.status_code >= 400:
            raise LLMError(f"AI engine error {resp.status_code}: {resp.text[:300]}")
        if not stream:
            resp.encoding = "utf-8"
            return clean_output(self._parse(resp.json()))
        out = []
        for piece in self._parse_stream(resp, on_progress):
            if cancelled and cancelled():
                resp.close()
                raise InterruptedError("cancelled")
            out.append(piece)
            if on_token:
                on_token(piece)
        return clean_output("".join(out))

    def chat_json(self, messages: list[dict], schema: dict, max_tokens: int = 2000, temperature: float = 0.2,
                  cancelled=None, on_token=None, on_progress=None) -> Any:
        text = self.chat(messages, schema, max_tokens, temperature, on_token=on_token, cancelled=cancelled,
                         on_progress=on_progress)
        try:
            return parse_json(text)
        except LLMError:
            # one retry, asking the model to fix its answer
            retry = messages + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": "That was not valid JSON. Reply again with only the JSON object."},
            ]
            text = self.chat(retry, schema, max_tokens, 0.1, cancelled=cancelled)
            return parse_json(text)


def _with_json_instruction(messages: list[dict], schema: dict) -> list[dict]:
    msgs = [dict(m) for m in messages]
    hint = ("\n\nRespond with ONLY a JSON object (no markdown, no commentary) that follows this JSON schema:\n"
            + json.dumps(schema))
    msgs[-1]["content"] = msgs[-1]["content"] + hint
    return msgs


def _sse_lines(resp: requests.Response) -> Iterator[dict]:
    resp.encoding = "utf-8"  # event streams often omit the charset (requests would guess Latin-1)
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            yield json.loads(line)
        except ValueError:
            continue


class OpenAICompatible(Backend):
    """llama-server and other OpenAI-style local servers."""

    name = "openai"

    def __init__(self, model: str, base_url: str, api_key: str = ""):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.extra: dict[str, Any] = {}

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _endpoint(self) -> str:
        return self.base_url + "/chat/completions"

    def _request(self, messages, schema, max_tokens, temperature, stream):
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": 0.9,
            "presence_penalty": self.presence_penalty,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        body.update(self.extra)
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            }
        return requests.post(self._endpoint(), json=body, headers=self._headers(), stream=stream, timeout=(10, 1800))

    def _parse(self, data: dict) -> str:
        msg = data["choices"][0]["message"]
        return msg.get("content") or ""

    def _parse_stream(self, resp, on_progress=None):
        writing = False
        for event in _sse_lines(resp):
            prog = event.get("prompt_progress")  # llama-server with return_progress
            if prog and on_progress and not writing:
                total, cache = prog.get("total") or 0, prog.get("cache") or 0
                if total > cache:
                    on_progress(min(1.0, max(0.0, ((prog.get("processed") or 0) - cache) / (total - cache))))
            choices = event.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    writing = True
                    yield piece

    def prepare(self, cancelled=None, on_status=None) -> None:
        try:
            r = requests.get(self.base_url + "/models", headers=self._headers(), timeout=5)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise LLMUnavailable(f"Could not connect to the local AI server at {self.base_url}.") from exc
        if not self.model:
            models = r.json().get("data") or []
            if not models:
                raise LLMUnavailable("The local AI server has no model loaded.")
            self.model = models[0]["id"]

    def context_size(self) -> int:
        return int(settings.get("llm_context") or 8192)


class BuiltinLlama(OpenAICompatible):
    name = "builtin"

    def __init__(self, model_key: str):
        super().__init__(model_key, "")
        self.model_key = model_key
        self.path = model_file(model_key)
        engine = runtime.installed() or {}
        self.gpu = settings.get("llm_device") != "cpu" and engine.get("backend") != "cpu"
        self.ctx = default_context(model_key, self.gpu)
        self.thinking = bool(settings.get("llm_thinking"))
        self.extra = {
            "chat_template_kwargs": {
                "enable_thinking": self.thinking,
                "reasoning_effort": "medium" if self.thinking else "low",
            },
            "cache_prompt": True,
            "return_progress": True,  # report prompt reading progress while streaming
        }

    def describe(self) -> str:
        entry = catalog.llm(self.model_key)
        return entry["name"] if entry else Path(self.model_key[5:]).stem

    def comfortable_tokens(self) -> int:
        """Small models take better notes from shorter pieces of a meeting."""
        entry = catalog.llm(self.model_key)
        tier = entry["tier"] if entry else "balanced"
        return {"light": 3500, "balanced": 6000, "quality": 12000, "best": 20000}.get(tier, 6000)

    def prepare(self, cancelled=None, on_status=None) -> None:
        if not self.path:
            raise LLMUnavailable("The selected AI model is not downloaded yet.")
        if not runtime.installed():
            raise LLMUnavailable("The AI engine is not installed yet.")
        from ..hardware import cpu_threads

        try:
            url = runtime.server.ensure(self.path, self.ctx, self.gpu, cpu_threads(), cancelled, on_status)
        except runtime.RuntimeError_ as exc:
            raise LLMError(str(exc)) from exc
        self.base_url = url + "/v1"
        self.root = url

    def context_size(self) -> int:
        return runtime.server.n_ctx or self.ctx

    def count_tokens(self, text: str) -> int:
        try:
            r = requests.post(self.root + "/tokenize", json={"content": text}, timeout=60)
            r.raise_for_status()
            return len(r.json().get("tokens", []))
        except (requests.RequestException, AttributeError, ValueError):
            return super().count_tokens(text)

    def chat(self, *args, **kwargs) -> str:
        runtime.server.touch()
        try:
            return super().chat(*args, **kwargs)
        finally:
            runtime.server.touch()


class Ollama(Backend):
    name = "ollama"

    def __init__(self, model: str, base_url: str):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.ctx = int(settings.get("llm_context") or 16384)

    def prepare(self, cancelled=None, on_status=None) -> None:
        try:
            r = requests.get(self.base_url + "/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise LLMUnavailable("Ollama is not running on this computer.") from exc
        names = [m["name"] for m in r.json().get("models", [])]
        if not names:
            raise LLMUnavailable("Ollama has no models. Pull one with 'ollama pull <model>'.")
        if not self.model or (self.model not in names and self.model + ":latest" not in names):
            self.model = names[0]

    def context_size(self) -> int:
        return self.ctx

    def _request(self, messages, schema, max_tokens, temperature, stream):
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "think": bool(settings.get("llm_thinking")),
            "options": {"temperature": temperature, "num_ctx": self.ctx, "num_predict": max_tokens,
                        "presence_penalty": self.presence_penalty},
        }
        if schema is not None:
            body["format"] = schema
        return requests.post(self.base_url + "/api/chat", json=body, stream=stream, timeout=(10, 1800))

    def _parse(self, data: dict) -> str:
        return (data.get("message") or {}).get("content", "")

    def _parse_stream(self, resp, on_progress=None):
        resp.encoding = "utf-8"
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            piece = (event.get("message") or {}).get("content")
            if piece:
                yield piece
            if event.get("done"):
                break


def get_backend(model: Optional[str] = None) -> Backend:
    s = settings.load()
    kind = s["llm_backend"]
    if kind == "ollama":
        return Ollama(model or s["llm_model"], s["ollama_url"])
    if kind == "openai":
        return OpenAICompatible(model or s["llm_model"], s["openai_url"], s["openai_key"])
    key = model or s["llm_model"]
    if not key or not model_file(key):
        installed_keys = installed_llm_keys()
        if not installed_keys:
            raise LLMUnavailable("No AI model is downloaded yet.")
        key = key if key in installed_keys else installed_keys[0]
    return BuiltinLlama(key)


def status() -> dict[str, Any]:
    s = settings.load()
    info: dict[str, Any] = {"backend": s["llm_backend"], "model": s["llm_model"]}
    if s["llm_backend"] == "builtin":
        rt = runtime.installed()
        info.update(
            engine_installed=bool(rt),
            engine=rt,
            installed_models=installed_llm_keys(),
            running=runtime.server.running(),
            ready=bool(rt) and bool(installed_llm_keys()),
            preferred_engine=runtime.preferred_backend(),
        )
    else:
        try:
            backend = get_backend()
            backend.prepare()
            info.update(ready=True, model=backend.model)
        except LLMError as exc:
            info.update(ready=False, reason=str(exc))
    return info
