"""Fake voice engine and fake LLM on httpx.MockTransport: no network, no API key, real ffmpeg."""

from __future__ import annotations

import io
import json
import math
import re
import struct
import wave
from typing import Any

import httpx
import pytest

from youtube_video_translator import translator

API_KEY = "sk-test-SECRET-0000"
LLM_KEY = "llm-SECRET-1111"


def tone_wav(seconds: float, freq: float = 220.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * n / rate))) for n in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return buf.getvalue()


CAPS = {
    "api_version": "1",
    "audio": {
        "models": [{"id": "expressive", "uses_reference_text": True}, {"id": "standard", "uses_reference_text": False}],
        "default_model": "expressive",
        "voice_sources": ["actor_id", "reference_audio_url"],
        "max_text_length": 5000,
    },
    "subtitles": {"points_per_second": 3, "max_upload_mb": 100, "max_duration_minutes": 90},
}

RECOGNITION = {
    "language": "es",
    "segments": [
        {"start": 0.0, "end": 2.4, "text": "Hola a todos."},
        {"start": 2.6, "end": 5.0, "text": "Hoy doblamos este video."},
        {"start": 5.4, "end": 8.0, "text": "Y cada voz suena como la original."},
    ],
}


class FakeEngine:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.keys: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.clip_seconds = 1.2
        self.fail_text: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=tone_wav(self.clip_seconds))
        assert request.headers.get("authorization") == f"Bearer {API_KEY}"
        if path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPS)
        if path.endswith("/actors"):
            return httpx.Response(200, json={"actors": [{"actor_id": "42", "name": "Narrator", "language": "en"}], "has_more": False})
        if path.endswith("/audio/generations"):
            body = json.loads(request.content)
            key = request.headers["idempotency-key"]
            self.created.append(body)
            self.keys.append(key)
            tid = f"t{len(self.created)}"
            failed = self.fail_text and self.fail_text in body["text"]
            self.tasks[tid] = {
                "task_id": tid,
                "status": "failed" if failed else "succeeded",
                "result": None if failed else {"audio_url": f"https://cdn.test/{tid}.wav"},
                "error": {"code": "synthesis_failed", "message": "boom", "retryable": True} if failed else None,
            }
            return httpx.Response(202, json={"task_id": tid, "status": "queued"})
        if path.endswith("/subtitles/recognitions"):
            self.tasks["sub1"] = {"task_id": "sub1", "status": "succeeded", "result": RECOGNITION}
            return httpx.Response(202, json={"task_id": "sub1", "status": "queued"})
        if "/tasks/" in path:
            return httpx.Response(200, json=self.tasks[path.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


SRT_TWO_SPEAKERS = """1
00:00:00,000 --> 00:00:02,500
Marco: Hola a todos, bienvenidos.

2
00:00:02,600 --> 00:00:04,000
Hoy probamos el doblaje

3
00:00:04,000 --> 00:00:05,500
con dos voces.

4
00:00:06,000 --> 00:00:09,000
Lucia: Y yo conservo mi propia voz.
"""




@pytest.fixture
def engine(monkeypatch, tmp_path):
    fake = FakeEngine()
    monkeypatch.setenv("MIRAGEA_API_KEY", API_KEY)
    monkeypatch.setenv("TRANSLATOR_OUTPUT_DIR", str(tmp_path / "out"))
    for name in ("TRANSLATOR_LLM_BASE_URL", "TRANSLATOR_LLM_API_KEY", "TRANSLATOR_LLM_MODEL", "OPENAI_API_KEY",
                 "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(translator, "_TRANSPORT", httpx.MockTransport(fake.handler))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(translator, "_SLEEP", no_sleep)
    return fake


@pytest.fixture
def sample_host(monkeypatch):
    uploads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploads.append(request.url.host)
        return httpx.Response(200, json={"success": True, "files": [{"url": f"https://d.host.test/{len(uploads)}.wav"}]})

    monkeypatch.setattr(translator, "_SAMPLE_HTTP", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return uploads


class FakeLLM:
    """OpenAI-compatible /chat/completions that 'translates' by prefixing the target language."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.skip_ids: set[str] = set()
        self.too_long_once: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers.get("authorization") == f"Bearer {LLM_KEY}"
        body = json.loads(request.content)
        self.requests.append(body)
        payload = json.loads(body["messages"][-1]["content"])
        lang = "XX"
        m = re.search(r"into (\w+)", payload["task"])
        if m:
            lang = m.group(1)[:2].upper()
        out = {}
        for line in payload["lines"]:
            lid = str(line["id"])
            if lid in self.skip_ids:
                self.skip_ids.discard(lid)
                continue
            if lid in self.too_long_once:
                self.too_long_once.discard(lid)
                out[lid] = "very long " * 40
                continue
            if "too long" in payload["task"]:
                out[lid] = "short"
                continue
            out[lid] = f"{lang}: {line['text']}"[: line.get("max_chars", 200)]
        content = "```json\n" + json.dumps({"translations": out}, ensure_ascii=False) + "\n```"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setenv("TRANSLATOR_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("TRANSLATOR_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("TRANSLATOR_LLM_MODEL", "fake-model")
    monkeypatch.setattr(translator, "_LLM_HTTP", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def srt_file(tmp_path):
    p = tmp_path / "talk.es.srt"
    p.write_text(SRT_TWO_SPEAKERS, encoding="utf-8")
    return p


@pytest.fixture
def speech_video(tmp_path):
    """A 9 s video whose audio has a low voice then a high voice (for cloning and pitch grouping)."""
    from youtube_video_translator.media import run_ffmpeg

    path = tmp_path / "talk.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=black:s=160x90:d=9",
        "-f", "lavfi", "-i", "sine=frequency=110:duration=4.5",
        "-f", "lavfi", "-i", "sine=frequency=260:duration=4.5",
        "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path),
    ])
    return path
