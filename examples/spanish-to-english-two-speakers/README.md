# Example: translate a two-speaker Spanish video into English (AI video dubbing)

Real output of `youtube-video-translator` v0.1.0, no edits. The source is an 11.3 s Spanish clip with two speakers, one male and one female.

## 1. Captions with names + chosen reference voices

```bash
youtube-video-translator interview-es.mp4 --to en --from es \
  --captions interview.es.srt --translation interview.en.srt \
  --voice "Marco=http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_bdl_arctic/wav/arctic_a0003.wav|For the twentieth time that evening the two men shook hands." \
  --voice "Lucia=http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_slt_arctic/wav/arctic_a0003.wav|For the twentieth time that evening the two men shook hands."
```

| File | What it is |
| --- | --- |
| [`interview-es.mp4`](interview-es.mp4) | original Spanish clip |
| [`interview.es.srt`](interview.es.srt) | source captions with speaker names (`Marco:` / `Lucia:`) |
| [`interview.en.srt`](interview.en.srt) | the English translation passed with `--translation` |
| [`interview-es.en.mp3`](interview-es.en.mp3) | English dub track, male line + female line, same length as the video |
| [`interview-es.en.mp4`](interview-es.en.mp4) | the video with the English dub and soft English subtitles |
| [`interview-es.en.srt`](interview-es.en.srt) / [`interview-es.en.speakers.srt`](interview-es.en.speakers.srt) | synced English subtitles (plain / with names) |

Timing: Marco's line 4.44 s in a 5.34 s slot, Lucia's 3.87 s in 5.85 s, no speed-up needed. Median pitch of the dub: 118 Hz (male) and 174 Hz (female).

## 2. Zero setup: no captions, voices cloned from the video

```bash
youtube-video-translator interview-es.mp4 --to en --translation interview.en.srt
```

No captions and no `--voice`: the speech was transcribed (3 lines, Spanish), split into two speakers by voice pitch, each speaker was cloned from a sample of their own Spanish lines, and the 2-cue English translation re-timed the dub. Files in [`cloned-from-video/`](cloned-from-video/): the [English dub MP3](cloned-from-video/interview-es.en.mp3), the [dubbed MP4](cloned-from-video/interview-es.en.mp4), the [English subtitles with speakers](cloned-from-video/interview-es.en.speakers.srt) and the [recognized Spanish transcript](cloned-from-video/interview-es.source.speakers.srt).

The demo contains no real person's voice: all voices derive from the CMU ARCTIC databases (speakers bdl and slt, © Carnegie Mellon University). See [CREDITS.md](../../CREDITS.md).
