"""One-command video translation: source -> transcript -> translation -> cloned-voice dub -> MP4/MP3/SRT.

Every step is saved in a project folder whose name is derived from the source,
so running the same command again resumes: finished transcripts, translations
and voiced lines are reused and pending voice jobs are polled, not resubmitted.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from . import media, pipeline, youtube
from .client import MirageaClient, MirageaError
from .config import MISSING_KEY_MESSAGE, Settings, load_settings
from .project import Project, ProjectError, normalize_language, slugify, text_hash
from .subtitles import Segment, to_srt
from .translate import CueCountMismatch, LLMConfig, align_to_cues, TranslationError, llm_config, load_translation_file, translate_title, translate_with_llm

# Test hooks (httpx.MockTransport, a no-op sleep, mocked sample host / LLM clients).
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_SLEEP: Any = None
_SAMPLE_HTTP: Optional[httpx.AsyncClient] = None
_LLM_HTTP: Optional[httpx.AsyncClient] = None


class TranslatorError(Exception):
    pass


@dataclass
class Options:
    source: str
    targets: list[str]
    source_language: Optional[str] = None
    translation_file: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    voices: list[str] = field(default_factory=list)
    num_speakers: Optional[int] = None
    captions_file: Optional[str] = None
    use_youtube_captions: bool = True
    subtitles_only: bool = False
    audio_only: bool = False
    audio_format: str = "mp3"
    max_speedup: float = 1.25
    original_volume: float = 0.0
    model: Optional[str] = None
    output_dir: Optional[str] = None
    fresh: bool = False
    quiet: bool = False


def log(opts: Options, message: str) -> None:
    if not opts.quiet:
        print(f"[youtube-video-translator] {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ voices
def parse_voice(spec: str) -> tuple[str, dict[str, Any]]:
    """`NAME=https://clip.wav`, `NAME=https://clip.wav|what the clip says`, `NAME=actor:42`, `*=...` for all."""
    if "=" not in spec:
        raise TranslatorError(f"--voice {spec!r} must look like NAME=URL, NAME=URL|reference text or NAME=actor:ID.")
    name, value = spec.split("=", 1)
    name, value = name.strip(), value.strip()
    if not name or not value:
        raise TranslatorError(f"--voice {spec!r} needs a speaker name and a voice.")
    if value.lower().startswith("actor:"):
        return name, {"actor_id": value.split(":", 1)[1].strip()}
    url, _, ref = value.partition("|")
    if not url.lower().startswith(("http://", "https://")):
        raise TranslatorError(f"--voice {spec!r}: the reference clip must be a public http(s) URL.")
    voice: dict[str, Any] = {"reference_audio_url": url.strip()}
    if ref.strip():
        voice["reference_text"] = ref.strip()
    return name, voice


def project_id_for(source: str) -> str:
    if youtube.is_youtube_url(source):
        m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{6,})", source)
        stem = m.group(1) if m else "youtube"
        key = stem
    else:
        path = Path(source).expanduser()
        stem = path.stem
        key = str(path.resolve())
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return f"{slugify(stem, 40)}-{digest}".lower()


def _client_factory(settings: Settings):
    def make() -> MirageaClient:
        kwargs: dict[str, Any] = {"transport": _TRANSPORT}
        if _SLEEP is not None:
            kwargs["sleep"] = _SLEEP
        return MirageaClient(settings, **kwargs)

    return make


def _stem(project: Project) -> str:
    title = project.data.get("title") or project.id
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:60] or project.id


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------- run
async def run(opts: Options) -> dict[str, Any]:
    settings = load_settings(opts.output_dir)
    targets = [normalize_language(t) for t in opts.targets]
    if not targets:
        raise TranslatorError("Give at least one target language with --to (for example --to en or --to es,de).")
    if opts.translation_file and len(targets) > 1 and not opts.translation_file.lower().endswith(".json"):
        raise TranslatorError("A .srt/.txt --translation file covers one language. Use one --to, or a JSON file keyed by language.")
    voices = dict(parse_voice(v) for v in opts.voices)
    factory = _client_factory(settings)
    if not opts.subtitles_only and not settings.has_key:
        raise MirageaError("missing_api_key", MISSING_KEY_MESSAGE)

    # 1. project + transcript --------------------------------------------------------
    pid = project_id_for(opts.source)
    root = settings.projects_dir / pid
    if opts.fresh and root.exists():
        shutil.rmtree(root)
    if (root / "project.json").exists():
        project = Project.load(settings.projects_dir, pid)
        log(opts, f"Resuming {root}")
    else:
        kind = "youtube" if youtube.is_youtube_url(opts.source) else "file"
        name = Path(opts.source).stem if kind == "file" else pid
        project = Project.create(settings.projects_dir, name, opts.source, kind, project_id=pid)
    if not project.segments:
        async def progress(msg: str) -> None:
            log(opts, msg)

        info = await pipeline.ingest(
            project,
            opts.source,
            client_factory=factory,
            source_language=opts.source_language,
            captions_file=opts.captions_file,
            prefer_captions=opts.use_youtube_captions,
            download_video=not opts.audio_only and not opts.subtitles_only,
            num_speakers=opts.num_speakers,
            progress=progress,
        )
        method = info["speaker_detection"].get("method")
        log(opts, f"Transcript: {len(project.segments)} lines, speakers {project.speaker_names()} ({method})")
    src_lang = project.data.get("source_language") or opts.source_language or "auto"
    stem = _stem(project)
    out_dir = project.root
    source_srt = _write(out_dir / f"{stem}.source.srt", to_srt(project.segments))
    _write(out_dir / f"{stem}.source.speakers.srt", to_srt(project.segments, with_speaker=True))

    # 2. voices (once, shared by every language) -------------------------------------
    if not opts.subtitles_only:
        for name, voice in voices.items():
            targets_for = project.speaker_names() if name == "*" else [name]
            for speaker in targets_for:
                pipeline.set_voice(project, speaker, voice)
        missing = project.missing_voices()
        if missing:
            if not project.media_path("audio"):
                raise TranslatorError(
                    "No voice for " + ", ".join(missing) + " and no source audio to clone from. "
                    'Pass --voice "NAME=https://clip.wav|transcript of the clip" or --voice "NAME=actor:ID".'
                )
            log(opts, f"Cloning voices from the source: {', '.join(missing)}")
            await pipeline.extract_samples(project, missing, share=True, http=_SAMPLE_HTTP)

    # 3. per language ------------------------------------------------------------------
    cfg: Optional[LLMConfig] = None
    if not opts.translation_file:
        cfg = llm_config(opts.llm_base_url, opts.llm_model)
    results: list[dict[str, Any]] = []
    for lang in targets:
        row: dict[str, Any] = {"language": lang}
        if pipeline.is_same_language(src_lang, lang):
            log(opts, f"{lang} is the source language: re-voicing the original lines")
            lines = {s.id: s.text for s in project.segments}
            pipeline.set_translation(project, lang, lines, max_speedup=opts.max_speedup)
        elif opts.translation_file:
            path = Path(opts.translation_file).expanduser()
            try:
                lines = load_translation_file(path, project.segments, lang)
            except CueCountMismatch as mismatch:
                log(opts, f"{path.name} has {len(mismatch.cues)} cues for {len(project.segments)} transcript lines: "
                          "following the translation's timing")
                project.set_segments(align_to_cues(project.segments, mismatch.cues))
                project.save()
                lines = {s.id: re.sub(r"\s+", " ", c.text).strip() for s, c in zip(project.segments, mismatch.cues)}
            report = pipeline.set_translation(project, lang, lines, max_speedup=opts.max_speedup)
            if report["missing"]:
                raise TranslatorError(f"The translation file has no line for segments {report['missing'][:15]}.")
        else:
            have = project.translation(lang)
            fingerprint = text_hash([s.text for s in project.segments], cfg.model if cfg else None)
            meta = (project.data.get("metadata") or {}).get(lang) or {}
            if all(have.get(s.id) for s in project.segments) and meta.get("fingerprint") == fingerprint:
                log(opts, f"Reusing the saved {lang} translation")
            else:
                if cfg is None:
                    raise TranslatorError(
                        f"No translation for {lang}. Either pass --translation FILE (translate {source_srt} line by line), "
                        "or point the tool at any OpenAI-compatible LLM: export TRANSLATOR_LLM_BASE_URL=... "
                        "TRANSLATOR_LLM_API_KEY=... TRANSLATOR_LLM_MODEL=... (or just OPENAI_API_KEY)."
                    )
                log(opts, f"Translating into {lang} with {cfg.model}")

                async def tprogress(msg: str) -> None:
                    log(opts, msg)

                http = _LLM_HTTP or httpx.AsyncClient()
                try:
                    lines = await translate_with_llm(
                        project.segments,
                        source_language=src_lang,
                        target_language=lang,
                        cfg=cfg,
                        duration=project.duration(),
                        max_speedup=opts.max_speedup,
                        title=project.data.get("title") or "",
                        http=http,
                        progress=tprogress,
                    )
                    title = await translate_title(http, cfg, project.data.get("title") or "", lang)
                finally:
                    if http is not _LLM_HTTP:
                        await http.aclose()
                pipeline.set_translation(project, lang, lines, title=title, max_speedup=opts.max_speedup)
                project.data["metadata"][lang]["fingerprint"] = fingerprint
                project.save()
        tr = project.translation(lang)
        timed = [Segment(s.id, s.start, s.end, tr[s.id], s.speaker) for s in project.segments]
        row["subtitles"] = str(_write(out_dir / f"{stem}.{lang}.srt", to_srt(timed)))

        if opts.subtitles_only:
            results.append(row)
            continue

        async with factory() as client:
            async def vprogress(msg: str) -> None:
                log(opts, f"[{lang}] {msg}")

            clips = await pipeline.synthesize_lines(
                project, lang, client, model=opts.model, concurrency=settings.concurrency, progress=vprogress
            )
        background = project.media_path("audio") if opts.original_volume > 0 else None
        rendered = pipeline.assemble(
            project,
            lang,
            clips,
            max_speedup=opts.max_speedup,
            out_format=opts.audio_format,
            background=background,
            background_volume=opts.original_volume,
            preview=not opts.audio_only,
        )
        track = Path(rendered["track"])
        row["audio"] = str(shutil.copyfile(track, out_dir / f"{stem}.{lang}{track.suffix}"))
        row["subtitles"] = str(shutil.copyfile(rendered["captions"], out_dir / f"{stem}.{lang}.srt"))
        shutil.copyfile(rendered["captions_with_speakers"], out_dir / f"{stem}.{lang}.speakers.srt")
        if rendered.get("preview_video"):
            row["video"] = str(shutil.copyfile(rendered["preview_video"], out_dir / f"{stem}.{lang}.mp4"))
        row.update(
            lines=rendered["lines"],
            sped_up_lines=rendered["sped_up_lines"],
            max_tempo=rendered["max_tempo"],
            overflowing_lines=len(rendered["overflowing_lines"]),
            advice=rendered["advice"],
        )
        results.append(row)

    package = None
    if not opts.subtitles_only and project.data.get("youtube"):
        package = pipeline.export_package(project, targets)["folder"]

    summary = {
        "project": str(project.root),
        "title": project.data.get("title"),
        "source_language": src_lang,
        "transcript_origin": project.data.get("transcript_origin"),
        "speakers": project.speaker_names(),
        "source_subtitles": str(source_srt),
        "languages": results,
        "youtube_upload_package": package,
    }
    _write(out_dir / "result.json", json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def check(opts_llm_base: Optional[str] = None, opts_llm_model: Optional[str] = None) -> dict[str, Any]:
    """Read-only environment check: ffmpeg, yt-dlp, voice-engine key, LLM endpoint."""
    report: dict[str, Any] = {}
    try:
        report["ffmpeg"] = media.ffmpeg_exe()
    except media.MediaError as exc:
        report["ffmpeg"] = f"missing: {exc}"
    try:
        import yt_dlp  # type: ignore[import-untyped]

        report["yt_dlp"] = yt_dlp.version.__version__
    except ImportError:
        report["yt_dlp"] = "missing"
    settings = load_settings()
    report["voice_engine_key"] = "set" if settings.has_key else "missing (export MIRAGEA_API_KEY)"
    try:
        cfg = llm_config(opts_llm_base, opts_llm_model)
        report["llm"] = f"{cfg.model} @ {cfg.base_url}" if cfg else "not configured (use --translation FILE or set TRANSLATOR_LLM_BASE_URL / OPENAI_API_KEY)"
    except TranslationError as exc:
        report["llm"] = f"invalid: {exc}"
    report["ready"] = settings.has_key and not str(report["ffmpeg"]).startswith("missing")
    return report


__all__ = ["Options", "TranslatorError", "run", "check", "parse_voice", "project_id_for", "ProjectError", "TranslationError"]
