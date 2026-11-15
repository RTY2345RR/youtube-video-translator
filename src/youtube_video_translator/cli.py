"""youtube-video-translator: translate and dub a YouTube video (or any video) in one command.

    youtube-video-translator "https://youtu.be/VIDEO_ID" --to es
    youtube-video-translator talk.mp4 --to en,de --translation talk.en.srt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__

EPILOG = """examples:
  youtube-video-translator "https://www.youtube.com/watch?v=VIDEO_ID" --to es
  youtube-video-translator interview.mp4 --to en --translation interview.en.srt
  youtube-video-translator lecture.mp4 --to de,fr,pt-BR --audio-only
  youtube-video-translator talk.mp4 --to en --voice "Speaker 1=https://example.com/me.wav|What I say in the clip"
  youtube-video-translator "https://youtu.be/VIDEO_ID" --to ja --subtitles-only

environment:
  MIRAGEA_API_KEY            voice engine key (voice cloning, dubbing, speech recognition)
  TRANSLATOR_LLM_BASE_URL    any OpenAI-compatible endpoint, e.g. https://api.openai.com/v1,
                             https://openrouter.ai/api/v1, http://localhost:11434/v1 (Ollama)
  TRANSLATOR_LLM_API_KEY     its key (falls back to OPENAI_API_KEY)
  TRANSLATOR_LLM_MODEL       model name (default gpt-4o-mini)
  YTDLP_COOKIES_FROM_BROWSER chrome|firefox|edge|safari, when YouTube asks to sign in

Run the same command again to resume: finished steps are reused, nothing is submitted twice.
Docs: https://github.com/RTY2345RR/youtube-video-translator
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube-video-translator",
        description="YouTube video translator and AI video dubbing CLI: transcript, translation, dubbed MP4/MP3 "
        "in each speaker's cloned voice, and translated SRT subtitles. No GPU needed.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", nargs="?", help="YouTube URL, local video/audio file, or an SRT/VTT transcript")
    p.add_argument("--to", dest="targets", help="target language code(s), comma separated: en, es, de, pt-BR, ja...")
    p.add_argument("--from", dest="source_language", help="source language code (auto-detected when omitted)")
    p.add_argument("-t", "--translation", dest="translation_file",
                   help="your own translation: SRT/VTT, TXT (one line per segment) or JSON {id: line} / {lang: {id: line}}")
    p.add_argument("--llm-base-url", help="OpenAI-compatible endpoint for machine translation (overrides TRANSLATOR_LLM_BASE_URL)")
    p.add_argument("--llm-model", help="LLM model for translation (overrides TRANSLATOR_LLM_MODEL)")
    p.add_argument("--voice", action="append", default=[], metavar="NAME=VOICE",
                   help='set a speaker voice instead of cloning it from the video: "NAME=URL", "NAME=URL|clip transcript", '
                   '"NAME=actor:ID"; NAME "*" means every speaker. Repeatable')
    p.add_argument("--speakers", dest="num_speakers", type=int, help="number of speakers when captions carry no names (default: auto)")
    p.add_argument("--captions", dest="captions_file", help="use this SRT/VTT as the source transcript")
    p.add_argument("--no-youtube-captions", action="store_true", help="ignore YouTube captions and run speech recognition")
    p.add_argument("--subtitles-only", action="store_true", help="only write translated SRT subtitles (no voices)")
    p.add_argument("--audio-only", action="store_true", help="skip the video download and the dubbed MP4")
    p.add_argument("--format", dest="audio_format", choices=("mp3", "m4a", "wav"), default="mp3", help="dub track format (default mp3)")
    p.add_argument("--max-speedup", type=float, default=1.25, help="max speed-up for lines longer than their slot (default 1.25)")
    p.add_argument("--original-volume", type=float, default=0.0,
                   help="mix the original audio under the dub at this volume, 0-1 (default 0 = dub only)")
    p.add_argument("--model", help="voice model id (see --check / the voice engine's capabilities)")
    p.add_argument("-o", "--output", dest="output_dir", help="output folder (default ./translated)")
    p.add_argument("--fresh", action="store_true", help="start over instead of resuming the saved project")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress messages")
    p.add_argument("--check", action="store_true", help="check ffmpeg, yt-dlp, the voice engine key and the LLM settings")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _print_summary(result: dict) -> None:
    print(f"\nDone: {result['title']}  ({result['source_language']} -> "
          f"{', '.join(r['language'] for r in result['languages'])})")
    print(f"  speakers: {', '.join(result['speakers'])}   transcript: {result['transcript_origin']}")
    print(f"  source subtitles: {result['source_subtitles']}")
    for r in result["languages"]:
        print(f"  [{r['language']}]")
        for key in ("video", "audio", "subtitles"):
            if r.get(key):
                print(f"    {key:9} {r[key]}")
        if r.get("overflowing_lines"):
            print(f"    note      {r['advice']}")
    if result.get("youtube_upload_package"):
        print(f"  YouTube Studio package: {result['youtube_upload_package']}")
    print(f"  project: {result['project']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from .translator import Options, check, run

    if args.check:
        report = check(args.llm_base_url, args.llm_model)
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 2
    if not args.source or not args.targets:
        parser.error("give a SOURCE and --to LANG (for example: youtube-video-translator URL --to es)")
    if not 1.0 <= args.max_speedup <= 2.0:
        parser.error("--max-speedup must be between 1.0 and 2.0")
    if not 0.0 <= args.original_volume <= 1.0:
        parser.error("--original-volume must be between 0 and 1")
    opts = Options(
        source=args.source,
        targets=[t.strip() for t in args.targets.split(",") if t.strip()],
        source_language=args.source_language,
        translation_file=args.translation_file,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        voices=args.voice,
        num_speakers=args.num_speakers,
        captions_file=args.captions_file,
        use_youtube_captions=not args.no_youtube_captions,
        subtitles_only=args.subtitles_only,
        audio_only=args.audio_only,
        audio_format=args.audio_format,
        max_speedup=args.max_speedup,
        original_volume=args.original_volume,
        model=args.model,
        output_dir=args.output_dir,
        fresh=args.fresh,
        quiet=args.quiet or args.json,
    )
    try:
        result = asyncio.run(run(opts))
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to resume.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - every error becomes one readable line
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
