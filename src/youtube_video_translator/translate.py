"""Translation: from a file you supply, or from any OpenAI-compatible chat endpoint.

Two ways to get the target-language lines:

* ``--translation FILE``: an SRT/VTT (matched to the source lines by order or
  by time), a JSON object ``{"1": "line", ...}`` (or ``{"es": {...}, "de": {...}}``),
  a JSON list of strings, or a TXT file with one line per segment.
* An LLM: any endpoint that speaks the OpenAI ``/chat/completions`` protocol
  (OpenAI, OpenRouter, DeepSeek, Groq, Together, a local Ollama or LM Studio...).
  Lines are sent in batches with their speaker, neighbours and a character
  budget that fits the original time slot, and any line that comes back too long
  is sent once more to be shortened.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from .config import ConfigError, validate_base_url
from .fit import budgets, check_translation
from .subtitles import Segment, parse_any

ProgressFn = Callable[[str], Awaitable[None]]

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "pt-BR": "Brazilian Portuguese", "fr": "French",
    "de": "German", "it": "Italian", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian",
    "tr": "Turkish", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil",
    "id": "Indonesian", "ms": "Malay", "vi": "Vietnamese", "th": "Thai", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese", "sv": "Swedish",
    "no": "Norwegian", "da": "Danish", "fi": "Finnish", "cs": "Czech", "el": "Greek", "ro": "Romanian",
    "hu": "Hungarian", "fa": "Persian", "ur": "Urdu", "tl": "Filipino",
}

SYSTEM_PROMPT = (
    "You are a professional dubbing translator. You translate video transcripts line by line so the "
    "translated speech can be dubbed over the original timing. Rules: translate the meaning naturally, as a "
    "native speaker would say it out loud, not word for word; keep each line close to its target_chars and "
    "never longer than max_chars (drop filler words rather than run long); keep names, brands and numbers; "
    "keep the speaker's tone; do not merge, split, add or drop lines; never translate the ids. Reply with "
    'only a JSON object: {"translations": {"<id>": "<translated line>", ...}}.'
)


class TranslationError(Exception):
    pass


class CueCountMismatch(TranslationError):
    """A translated SRT/VTT whose cues do not line up 1:1 with the transcript."""

    def __init__(self, cues: list[Segment]) -> None:
        super().__init__(f"The translation has {len(cues)} cues; the transcript is re-cut to match them.")
        self.cues = cues


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code) or LANGUAGE_NAMES.get(code.split("-")[0]) or code


# ======================================================================== file
def _normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def align_to_cues(segments: list[Segment], cues: list[Segment]) -> list[Segment]:
    """Re-cut the source transcript on the translation's own timing.

    Used when a translated SRT/VTT has a different number of cues than the
    transcript: each cue becomes one segment holding the source text it overlaps,
    voiced by the speaker who talks most inside it.
    """
    out: list[Segment] = []
    for i, cue in enumerate(cues, 1):
        overlaps = [(min(cue.end, s.end) - max(cue.start, s.start), s) for s in segments]
        inside = [s for ov, s in overlaps if ov > 0.05 or (cue.start <= (s.start + s.end) / 2 <= cue.end)]
        talk: dict[str, float] = {}
        for ov, s in overlaps:
            if ov > 0 and s.speaker:
                talk[s.speaker] = talk.get(s.speaker, 0.0) + ov
        nearest = min(segments, key=lambda s: abs((s.start + s.end) / 2 - (cue.start + cue.end) / 2))
        speaker = max(talk, key=talk.get) if talk else nearest.speaker  # type: ignore[arg-type]
        text = " ".join(s.text for s in inside) or nearest.text
        out.append(Segment(i, cue.start, cue.end, text, speaker))
    return out


def load_translation_file(path: Path, segments: list[Segment], language: str) -> dict[int, str]:
    """Read a user-supplied translation and return {segment id: line}."""
    if not path.exists():
        raise TranslationError(f"Translation file not found: {path}")
    raw = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    ids = [s.id for s in segments]
    if suffix == ".json":
        data = json.loads(raw)
        if isinstance(data, dict) and language in data and isinstance(data[language], (dict, list)):
            data = data[language]
        if isinstance(data, dict) and isinstance(data.get("translations"), (dict, list)):
            data = data["translations"]
        if isinstance(data, list):
            if data and isinstance(data[0], dict):  # [{"id": 1, "text": "..."}]
                return {int(row["id"]): _normalize(row.get("text") or row.get("translation")) for row in data}
            if len(data) != len(ids):
                raise TranslationError(f"{path.name} has {len(data)} lines but the transcript has {len(ids)}.")
            return {i: _normalize(t) for i, t in zip(ids, data)}
        if isinstance(data, dict):
            try:
                return {int(k): _normalize(v) for k, v in data.items()}
            except ValueError:
                raise TranslationError(
                    f"{path.name}: keys must be segment ids (\"1\", \"2\"...) or language codes."
                ) from None
        raise TranslationError(f"{path.name}: unsupported JSON shape.")
    if suffix in (".srt", ".vtt"):
        cues = parse_any(raw, suffix)
        if not cues:
            raise TranslationError(f"{path.name} contains no subtitle cues.")
        if len(cues) != len(ids):
            raise CueCountMismatch(cues)
        return {i: _normalize(c.text) for i, c in zip(ids, cues)}
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) != len(ids):
        raise TranslationError(
            f"{path.name} has {len(lines)} non-empty lines but the transcript has {len(ids)} segments. "
            "Run with --subtitles-only first and translate the generated *.source.srt line by line."
        )
    return dict(zip(ids, (_normalize(x) for x in lines)))


# ========================================================================= LLM
@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 180.0
    batch_size: int = 25

    def __repr__(self) -> str:
        return f"LLMConfig(base_url={self.base_url!r}, model={self.model!r}, api_key={'***' if self.api_key else '<unset>'})"


def llm_config(base_url: Optional[str] = None, model: Optional[str] = None) -> Optional[LLMConfig]:
    """Resolve the LLM from flags and environment. None when nothing is configured."""
    env = os.environ
    key = (env.get("TRANSLATOR_LLM_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    base = (base_url or env.get("TRANSLATOR_LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or "").strip()
    if not base and key:
        base = "https://api.openai.com/v1"
    if not base:
        return None
    try:
        base = validate_base_url(base, "TRANSLATOR_LLM_BASE_URL")
    except ConfigError as exc:
        raise TranslationError(str(exc)) from None
    chosen = (model or env.get("TRANSLATOR_LLM_MODEL") or env.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    return LLMConfig(base_url=base, model=chosen, api_key=key)


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise TranslationError("The LLM did not return JSON.")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        raise TranslationError("The LLM returned malformed JSON.") from None
    if isinstance(data, dict) and isinstance(data.get("translations"), dict):
        return data["translations"]
    if isinstance(data, dict):
        return data
    raise TranslationError("The LLM returned an unexpected JSON shape.")


async def _chat(http: httpx.AsyncClient, cfg: LLMConfig, user: str) -> str:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    body = {
        "model": cfg.model,
        "temperature": 0.3,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
    }
    last: Optional[str] = None
    for _attempt in range(3):
        try:
            resp = await http.post(f"{cfg.base_url}/chat/completions", json=body, headers=headers, timeout=cfg.timeout)
        except httpx.HTTPError as exc:
            last = f"could not reach {cfg.base_url} ({type(exc).__name__})"
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            continue
        if resp.status_code >= 400:
            detail = resp.text[:300].replace(cfg.api_key, "***") if cfg.api_key else resp.text[:300]
            raise TranslationError(f"The LLM endpoint returned HTTP {resp.status_code}: {detail}")
        try:
            return str(resp.json()["choices"][0]["message"]["content"] or "")
        except (ValueError, KeyError, IndexError, TypeError):
            raise TranslationError("The LLM endpoint returned an unexpected response (not OpenAI-compatible?).") from None
    raise TranslationError(f"The LLM endpoint failed: {last}.")


def _payload(rows: list[dict[str, Any]], context_before: list[dict[str, Any]], context_after: list[dict[str, Any]],
             source: str, target: str, title: str) -> str:
    def slim(r: dict[str, Any], budget: bool = True) -> dict[str, Any]:
        out = {"id": str(r["id"]), "speaker": r.get("speaker"), "text": r["text"]}
        if budget:
            out.update(target_chars=r["target_chars"], max_chars=r["max_chars"])
        return out

    return json.dumps(
        {
            "task": f"Translate every line in `lines` from {language_name(source)} into {language_name(target)} "
            "for dubbing. `context_before` / `context_after` are neighbouring lines for meaning only; do not "
            "translate or return them.",
            "video_title": title,
            "context_before": [slim(r, False) for r in context_before],
            "lines": [slim(r) for r in rows],
            "context_after": [slim(r, False) for r in context_after],
        },
        ensure_ascii=False,
    )


async def translate_with_llm(
    segments: list[Segment],
    *,
    source_language: str,
    target_language: str,
    cfg: LLMConfig,
    duration: Optional[float],
    max_speedup: float = 1.25,
    title: str = "",
    http: Optional[httpx.AsyncClient] = None,
    progress: Optional[ProgressFn] = None,
) -> dict[int, str]:
    rows = budgets(segments, target_language, duration, max_speedup)
    own = http is None
    http = http or httpx.AsyncClient()
    out: dict[int, str] = {}
    try:
        n = cfg.batch_size
        for i in range(0, len(rows), n):
            batch = rows[i : i + n]
            text = await _chat(http, cfg, _payload(batch, rows[max(0, i - 3) : i], rows[i + n : i + n + 2],
                                                   source_language, target_language, title))
            got = _extract_json(text)
            for r in batch:
                value = _normalize(got.get(str(r["id"])) or got.get(r["id"]) or "")  # type: ignore[call-overload]
                if value:
                    out[r["id"]] = value
            missing = [r for r in batch if r["id"] not in out]
            if missing:  # one retry for lines the model skipped
                got = _extract_json(await _chat(http, cfg, _payload(missing, [], [], source_language, target_language, title)))
                for r in missing:
                    value = _normalize(got.get(str(r["id"])) or "")
                    if value:
                        out[r["id"]] = value
            if progress:
                await progress(f"Translated {min(i + n, len(rows))}/{len(rows)} lines into {target_language}")
        still = [r["id"] for r in rows if r["id"] not in out]
        if still:
            raise TranslationError(f"The LLM did not translate segments {still[:15]}. Run the same command again.")

        report = check_translation(segments, out, target_language, duration, max_speedup)
        if report["too_long"]:
            if progress:
                await progress(f"Shortening {len(report['too_long'])} lines that are too long for their time slot")
            fix = [
                {"id": r["id"], "speaker": None, "text": r["text"], "target_chars": r["target_chars"], "max_chars": r["max_chars"]}
                for r in report["too_long"]
            ]
            prompt = json.dumps(
                {
                    "task": f"These {language_name(target_language)} dubbing lines are too long to fit the original "
                    "timing. Rewrite each one shorter, close to target_chars and never above max_chars, keeping the "
                    "meaning. Reply in the same JSON format.",
                    "lines": [{k: str(v) if k == "id" else v for k, v in f.items() if k != "speaker"} for f in fix],
                },
                ensure_ascii=False,
            )
            try:
                got = _extract_json(await _chat(http, cfg, prompt))
                for f in fix:
                    value = _normalize(got.get(str(f["id"])) or "")
                    if value and len(value) < len(out[f["id"]]):
                        out[f["id"]] = value
            except TranslationError:
                pass  # keep the longer lines; rendering speeds them up within --max-speedup
        return out
    finally:
        if own:
            await http.aclose()


async def translate_title(http: httpx.AsyncClient, cfg: LLMConfig, title: str, target: str) -> Optional[str]:
    if not title:
        return None
    try:
        prompt = json.dumps(
            {"task": f"Translate this YouTube video title into {language_name(target)}. Keep it under 100 characters.",
             "lines": [{"id": "title", "text": title}]},
            ensure_ascii=False,
        )
        got = _extract_json(await _chat(http, cfg, prompt))
        return _normalize(got.get("title"))[:100] or None
    except TranslationError:
        return None
