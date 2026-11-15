import json
from pathlib import Path

import anyio
import httpx
import pytest

from youtube_video_translator import cli, translator
from youtube_video_translator.client import MirageaError
from youtube_video_translator.media import has_video_stream, probe_duration
from youtube_video_translator.subtitles import Segment, parse_srt_or_vtt
from youtube_video_translator.translate import CueCountMismatch, TranslationError, align_to_cues, llm_config, load_translation_file

from .conftest import API_KEY, LLM_KEY

EN_SRT = """1
00:00:00,000 --> 00:00:02,500
Hi everyone, welcome.

2
00:00:02,600 --> 00:00:05,500
Today we test dubbing with two voices.

3
00:00:06,000 --> 00:00:09,000
And I keep my own voice.
"""

VOICES = ["Marco=https://ex.test/m.wav|ref m", "Lucia=actor:42"]


def run(opts):
    return anyio.run(translator.run, opts)


def opts(source, **kw):
    kw.setdefault("quiet", True)
    return translator.Options(source=str(source), **kw)


def test_srt_source_with_translation_file(engine, srt_file, tmp_path):
    tr = tmp_path / "talk.en.srt"
    tr.write_text(EN_SRT, encoding="utf-8")
    res = run(opts(srt_file, targets=["en"], source_language="es", translation_file=str(tr), voices=VOICES))
    assert res["speakers"] == ["Marco", "Lucia"]
    row = res["languages"][0]
    assert row["language"] == "en" and row["lines"] == 3 and "video" not in row
    assert Path(row["audio"]).suffix == ".mp3" and probe_duration(Path(row["audio"])) > 8.5
    cues = parse_srt_or_vtt(Path(row["subtitles"]).read_text())
    assert [c.text for c in cues][0] == "Hi everyone, welcome."
    bodies = engine.created
    assert bodies[0]["reference_audio_url"] == "https://ex.test/m.wav" and bodies[0]["reference_text"] == "ref m"
    assert any(b.get("actor_id") == "42" for b in bodies)
    assert Path(res["source_subtitles"]).exists()
    assert json.loads((Path(res["project"]) / "result.json").read_text())["languages"][0]["language"] == "en"


def test_llm_two_languages_then_resume_without_resubmitting(engine, llm, srt_file):
    o = opts(srt_file, targets=["en", "de"], source_language="es", voices=VOICES)
    res = run(o)
    assert [r["language"] for r in res["languages"]] == ["en", "de"]
    de = Path(res["languages"][1]["subtitles"]).read_text()
    assert "GE: " in de  # "German" from the fake LLM
    calls, jobs = len(llm.requests), len(engine.created)
    assert jobs == 6 and calls >= 2
    run(o)
    assert len(llm.requests) == calls and len(engine.created) == jobs
    sent = json.loads(llm.requests[0]["messages"][-1]["content"])
    assert sent["lines"][0]["max_chars"] >= sent["lines"][0]["target_chars"] > 0
    assert llm.requests[0]["model"] == "fake-model"


def test_llm_retries_skipped_lines_and_shortens_long_ones(engine, llm, srt_file):
    llm.skip_ids = {"2"}
    llm.too_long_once = {"3"}
    res = run(opts(srt_file, targets=["en"], source_language="es", subtitles_only=True))
    text = Path(res["languages"][0]["subtitles"]).read_text()
    assert "very long" not in text and "\nshort\n" in text and "EN: Hoy probamos" in text


def test_subtitles_only_needs_no_voice_key(llm, srt_file, monkeypatch, tmp_path):
    monkeypatch.delenv("MIRAGEA_API_KEY", raising=False)
    monkeypatch.setenv("TRANSLATOR_OUTPUT_DIR", str(tmp_path / "o"))
    res = run(opts(srt_file, targets=["fr"], source_language="es", subtitles_only=True))
    assert "audio" not in res["languages"][0] and Path(res["languages"][0]["subtitles"]).exists()


def test_missing_key_for_dubbing(llm, srt_file, monkeypatch, tmp_path):
    monkeypatch.delenv("MIRAGEA_API_KEY", raising=False)
    monkeypatch.setenv("TRANSLATOR_OUTPUT_DIR", str(tmp_path / "o"))
    with pytest.raises(MirageaError, match="MIRAGEA_API_KEY"):
        run(opts(srt_file, targets=["fr"]))


def test_no_translation_source_explains_both_options(engine, srt_file):
    with pytest.raises(translator.TranslatorError, match="--translation FILE"):
        run(opts(srt_file, targets=["en"], source_language="es", voices=VOICES))


def test_caption_only_source_needs_voices(engine, llm, srt_file):
    with pytest.raises(translator.TranslatorError, match="no source audio"):
        run(opts(srt_file, targets=["en"], source_language="es"))


def test_local_video_transcribes_clones_and_dubs(engine, llm, sample_host, speech_video):
    res = run(opts(speech_video, targets=["en"]))
    assert res["transcript_origin"] == "speech-recognition" and res["source_language"] == "es"
    row = res["languages"][0]
    video = Path(row["video"])
    assert video.exists() and has_video_stream(video)
    assert abs(probe_duration(Path(row["audio"])) - probe_duration(speech_video)) < 0.3
    assert sample_host and all(b["reference_audio_url"].startswith("https://d.host.test/") for b in engine.created)
    assert all(b.get("reference_text") for b in engine.created)


def test_same_language_revoices(engine, srt_file):
    res = run(opts(srt_file, targets=["es"], source_language="es", voices=["*=actor:7"]))
    assert {b["actor_id"] for b in engine.created} == {"7"}
    assert "Hola a todos" in engine.created[0]["text"]
    assert res["languages"][0]["lines"] == 3


def _segs():
    return [Segment(1, 0, 2, "a"), Segment(2, 2, 4, "b"), Segment(3, 4, 6, "c")]


def test_translation_file_formats(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"de": {"1": "eins", "2": "zwei", "3": "drei"}}))
    assert load_translation_file(p, _segs(), "de") == {1: "eins", 2: "zwei", 3: "drei"}
    p.write_text(json.dumps(["x", "y", "z"]))
    assert load_translation_file(p, _segs(), "de")[3] == "z"
    p.write_text(json.dumps([{"id": 2, "text": "  two  "}]))
    assert load_translation_file(p, _segs(), "de") == {2: "two"}
    t = tmp_path / "t.txt"
    t.write_text("one\n\ntwo\n")
    with pytest.raises(TranslationError, match="2 non-empty lines"):
        load_translation_file(t, _segs(), "de")
    s = tmp_path / "t.srt"  # 2 cues for 3 segments -> the transcript follows the translation's timing
    s.write_text("1\n00:00:00,000 --> 00:00:04,000\nAB\n\n2\n00:00:04,000 --> 00:00:06,000\nC\n")
    with pytest.raises(CueCountMismatch) as info:
        load_translation_file(s, _segs(), "de")
    segs = [Segment(1, 0, 2, "a", "X"), Segment(2, 2, 4, "b", "Y"), Segment(3, 4, 6, "c", "Y")]
    aligned = align_to_cues(segs, info.value.cues)
    assert [(a.text, a.speaker, a.start, a.end) for a in aligned] == [("a b", "X", 0, 4), ("c", "Y", 4, 6)]


def test_parse_voice_and_project_id(tmp_path):
    assert translator.parse_voice("Ann=https://x.test/a.wav|hello there") == (
        "Ann", {"reference_audio_url": "https://x.test/a.wav", "reference_text": "hello there"})
    assert translator.parse_voice("Speaker 1=actor:9") == ("Speaker 1", {"actor_id": "9"})
    with pytest.raises(translator.TranslatorError):
        translator.parse_voice("Ann=/local/file.wav")
    a = translator.project_id_for("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3")
    assert a == translator.project_id_for("https://youtu.be/dQw4w9WgXcQ") and a.startswith("dqw4w9wgxcq-")


def test_llm_config_from_env(monkeypatch):
    for n in ("TRANSLATOR_LLM_BASE_URL", "TRANSLATOR_LLM_API_KEY", "TRANSLATOR_LLM_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(n, raising=False)
    assert llm_config() is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-zzz")
    cfg = llm_config()
    assert cfg.base_url == "https://api.openai.com/v1" and cfg.model == "gpt-4o-mini" and "sk-zzz" not in repr(cfg)
    assert llm_config("http://localhost:11434/v1/", "qwen2.5").base_url == "http://localhost:11434/v1"


def test_llm_http_error_never_leaks_key(engine, srt_file, monkeypatch):
    def handler(request):
        return httpx.Response(401, text=f"bad key {LLM_KEY}")

    monkeypatch.setenv("TRANSLATOR_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("TRANSLATOR_LLM_API_KEY", LLM_KEY)
    monkeypatch.setattr(translator, "_LLM_HTTP", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TranslationError) as info:
        run(opts(srt_file, targets=["en"], source_language="es", subtitles_only=True))
    assert "401" in str(info.value) and LLM_KEY not in str(info.value)


def test_cli_end_to_end_and_errors(engine, llm, srt_file, capsys):
    assert cli.main([str(srt_file), "--to", "en", "--from", "es", "--json", "--voice", VOICES[0], "--voice", VOICES[1]]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["languages"][0]["audio"].endswith(".en.mp3")
    assert cli.main(["missing.mp4", "--to", "en", "-q"]) == 1
    assert "Source not found" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["only-source"])
    with pytest.raises(SystemExit):
        cli.main([str(srt_file), "--to", "en", "--max-speedup", "3"])


def test_cli_check(engine, llm, capsys):
    code = cli.main(["--check"])
    report = json.loads(capsys.readouterr().out)
    assert report["voice_engine_key"] == "set" and report["llm"].startswith("fake-model") and code == 0
    assert API_KEY not in json.dumps(report)


def test_translation_srt_with_other_cut_recuts_transcript(engine, srt_file, tmp_path):
    tr = tmp_path / "two.en.srt"
    tr.write_text("1\n00:00:00,000 --> 00:00:05,500\nHi all, today we test two voices.\n\n"
                  "2\n00:00:06,000 --> 00:00:09,000\nAnd I keep my voice.\n", encoding="utf-8")
    res = run(opts(srt_file, targets=["en"], source_language="es", translation_file=str(tr), voices=VOICES))
    assert res["languages"][0]["lines"] == 2 and [b["text"] for b in engine.created][0].startswith("Hi all")


def test_recognition_language_name_becomes_code(engine, llm, sample_host, speech_video, monkeypatch):
    from . import conftest

    monkeypatch.setitem(conftest.RECOGNITION, "language", "Spanish")
    res = run(opts(speech_video, targets=["en"], subtitles_only=True))
    assert res["source_language"] == "es"
