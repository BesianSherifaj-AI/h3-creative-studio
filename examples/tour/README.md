# Make the Tidal Relay tour

Rebuild the 44-second studio walkthrough for YouTube, GitHub, TikTok and Reels. The landscape cut is 1920 × 1080 and the vertical cut is 1080 × 1920. Both have a 24 fps picture, stereo narration and score, and English captions burned into the picture. The matching SRT is available for editors and uploads.

## Capture the workshop

Use a Windows desktop with WebView2, the app’s media tools and an installed Windows speech voice. The workshop verifier runs the actual Image, Motion, Film, Game, Edit, Voice and AI client controls in a fresh Studio project. It captures the 1440 × 900 interface and player sequences at eight samples per second. For the hosted release, use the extracted portable app and a fresh evidence directory.

From a source checkout with the [Studio dependencies](../../requirements.txt) installed, run these commands in PowerShell. Set `$executable` to the extracted Studio launcher.

```powershell
python -m pip install playwright==1.62.0
$executable = (Resolve-Path ".\dist\extracted\H3 Creative Studio\H3 Creative Studio.exe").Path
python scripts/verify_workshops.py --record --exe $executable --evidence dist/workshops
```

The verifier prints its `Workshops-…` run folder. A completed run contains `verified.json`, chapter screenshots, `record-*` player sequences and `tour-bounds.json`. That last file records the visible export controls in the matching screenshots, so the tour shows the selected framing, crop sliders and caption option at their actual positions. The build commands below select the most recent run in the evidence directory.

## Build and check the films

The builder and verifier need Python 3.11 or later, Pillow, PyAV and FFmpeg with libx264, AAC, loudnorm and ebur128. Studio’s Python environment includes the two Python libraries. For a separate tour-only environment, install them with `python -m pip install pillow==12.3.0 av==17.1.0`. FFmpeg must be on PATH or passed via `--ffmpeg`.

```powershell
$captures = (Get-ChildItem dist/workshops -Directory -Filter 'Workshops-*' | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
python examples/tour/build.py --captures $captures --output dist/showcase --check-inputs
python examples/tour/build.py --captures $captures --output dist/showcase
python examples/tour/verify.py --output dist/showcase
```

To rebuild one aspect, pass `--format vertical` or `--format landscape`. To use a different narration set, pass the same `--audio <directory>` to both scripts. Match the nine chapter IDs in the bundled manifest, include the SHA-256 and PCM sample count for each mono WAV, and supply its transcript and word times in seconds.

The verifier decodes every frame and the entire audio track of both MP4s. It checks presentation time, stereo sample count, caption timing and text, and measures integrated loudness and true peak. Review the two contact sheets alongside the finished films.

- `H3-Studio-Tour-landscape.mp4` and `H3-Studio-Tour-vertical.mp4` contain the two finished edits.
- `H3-Studio-Tour.en.srt` contains the aligned captions. The supplied narration produces 25 cues across 1,060 frames.
- `review-landscape.jpg` and `review-vertical.jpg` contain decoded frames from all nine chapters.
- `tour-verified.json` records each film’s dimensions, duration, frame and sample counts, measured LUFS, true peak in dBTP and SHA-256.

## Original media

The nine mono 24 kHz WAV masters contain original narration written for this example and synthesized with Kokoro-82M’s `af_heart` voice. Word times were aligned against the written script using Whisper medium. The narration files and scripts are included under the [repository licence](../../LICENSE). [Kokoro model card](https://huggingface.co/hexgrad/Kokoro-82M)

The edit uses the bundled Tidal Relay camera moves, original *Listening Line* score, the app’s own interface and its Barlow title font. Credits for the source plates, music and font accompany the [starter media](../../h3studio/starter/README.md).
