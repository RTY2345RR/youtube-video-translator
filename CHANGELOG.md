# Changelog

## 0.1.0 (2026-11-15)

First release.

- `youtube-video-translator SOURCE --to LANGS`: YouTube URL, local video/audio or SRT/VTT in; dubbed MP4, MP3/M4A/WAV track and translated SRT out.
- Transcript from YouTube captions (manual, then automatic) or Miragea speech recognition; speakers from caption names or voice pitch.
- Translation from a user file (SRT/VTT/TXT/JSON; a differently cut SRT re-times the dub) or any OpenAI-compatible LLM, with per-line length budgets, retry of skipped lines and a shortening pass.
- One cloned voice per speaker, sampled from the source; `--voice` overrides with a clip URL or saved voice.
- Length-matched dub track, speed-up only on overlap (`--max-speedup`), optional original audio bed (`--original-volume`).
- `--subtitles-only`, `--audio-only`, `--json`, `--check`; resumable projects with no duplicate jobs on retry.
- YouTube Studio upload folder for multi-language audio tracks.
