# Credits

## Demo voices: CMU ARCTIC

The example voices in `examples/` were cloned from recordings in the CMU ARCTIC
speech databases (speakers `bdl`, male, and `slt`, female), used as reference audio:

- http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_bdl_arctic/wav/arctic_a0003.wav
- http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_slt_arctic/wav/arctic_a0003.wav

The source "interview" clip was itself generated from the same two references, so
the demo contains no real person's voice.

Source: CMU ARCTIC databases, http://www.festvox.org/cmu_arctic/ . Modifications: the
reference clips were used to clone voices; no recording is redistributed unchanged.
Licence notice, verbatim:

```text
This voice is free for use for any purpose (commercial or otherwise)
subject to the pretty light restrictions detailed below.

############################################################################
###                                                                       ##
###                     Carnegie Mellon University                        ##
###                         Copyright (c) 2003                            ##
###                        All Rights Reserved.                           ##
###                                                                       ##
###  Permission to use, copy, modify,  and licence this software and its  ##
###  documentation for any purpose, is hereby granted without fee,        ##
###  subject to the following conditions:                                 ##
###   1. The code must retain the above copyright notice, this list of    ##
###      conditions and the following disclaimer.                         ##
###   2. Any modifications must be clearly marked as such.                ##
###   3. Original authors' names are not deleted.                         ##
###                                                                       ##
###  THE AUTHORS OF THIS WORK DISCLAIM ALL WARRANTIES WITH REGARD TO      ##
###  THIS SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY   ##
###  AND FITNESS, IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY         ##
###  SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES            ##
###  WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN   ##
###  AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,          ##
###  ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF       ##
###  THIS SOFTWARE.                                                       ##
###                                                                       ##
############################################################################
###                                                                       ##
###  See http://www.festvox.org/cmu_arctic/ for more details              ##
###                                                                       ##
############################################################################
```

## Software

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) for YouTube metadata, captions and media.
- [FFmpeg](https://ffmpeg.org) via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (BSD-2-Clause wrapper).
- [httpx](https://github.com/encode/httpx) (BSD-3-Clause).
- Voice cloning, dubbing and speech recognition: the [Miragea](https://miragea.ai) voice engine.
- The dubbing core (subtitles, timing fit, media, YouTube helpers) is shared with
  [youtube-dubbing-mcp](https://github.com/RTY2345RR/youtube-dubbing-mcp) (MIT, same authors).
