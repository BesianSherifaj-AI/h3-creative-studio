# H3 Creative Studio

A Windows desktop workspace for layered stills, MiniMax H3 video, frame guides, source-frame edits and film delivery. Build a composition, pin different revisions at the start, middle and end of a shot, then compare takes and continue from the ending you choose.

## Run on Windows

Install 64-bit Python 3.11 and Microsoft Edge WebView2. Put a full FFmpeg build with ffmpeg and ffprobe on PATH. Film export uses libx264, AAC, zscale and tonemap. The motion join also supports decoded fallback. The verified machine used Python 3.11 and FFmpeg 9.

```powershell
git clone https://github.com/BesianSherifaj-AI/h3-creative-studio.git
cd h3-creative-studio
.\Setup.ps1
.\Launch.cmd
```

Setup creates the project virtual environment and a **H3 Creative Studio** desktop shortcut. Use `powershell -ExecutionPolicy Bypass -File .\Setup.ps1` if local execution policy blocks the installer. Pass `-NoShortcut` when testing a second clone. On first launch, Copper Orbit provides a local three-layer composition and a five-second shot. The CPU storyboard and editorial tools work with the desktop dependencies alone.

## Make a shot

1. Open a still or add a transparent layer. Transform, mask or paint it and save the revision.
2. Select a shot. **Place guide** attaches the exact current revision to a delivered frame. Use **Media guide** for a reference image, video clip or audio. The window supports first, middle, last and arbitrary frame positions.
3. Use **Render storyboard** for a CPU preview. For generated motion, open **Engines**, choose the H3 video installation, then **Generate → Prepare H3 shot**. Check the guide crops, frame counts, model settings and ordered LoRAs before queuing.
4. In **Queue**, open the completed result in the project. **Try another take** preserves the displayed version. **Continue from this ending** can use the last visible frame, a painted canvas revision or the verified original motion state. **Plan a separate scene** starts a new shot.
5. Reorder the cut, set trims and audio routes and export. The delivery settings specify size, rational frame rate, color and output audio-track count. Source-frame edits use original timestamps and orientation. Hidden/reappears and corrected manual anchors are available for moving subjects. Preview the propagated mask on an unanchored frame before rendering a range edit. Use **Film soundtrack** under Export to mix a portable music or narration file across cuts. Choose its offset, gain, source stream and delivery audio track.

**Generate → Generate or edit a still** offers MiniMax H3 Image, Qwen Image 2.1, Z-Image Turbo and Qwen Image Layered according to the model's supported operations. A grayscale edit mask preserves the black areas and edits the white areas. Paint and layer revisions remain editable. In **Director**, the selected LM Studio model can propose a Film storyboard or Game turn. Keep the storyboard for later, approve individual shots, or use **Approve and render all** to queue the whole board in one click. Lock a selected take before regenerating another. Accepted Game turns preserve their action, response and parent so you can return to an earlier choice or reopen the active branch.

In **Prepare H3 shot**, save shot settings, set project defaults or save and reapply a named preset. These preserve prompts, seeds and ordered LoRAs. Preparing a continuation shows the proposed starting image alongside its other guide tracks. The change takes effect on submission. Replacing an earlier take marks dependent shots for continuity review and keeps the previous cut available.

## Configure engines

The desktop and GPU processes use separate Python environments. Configure an existing compatible ComfyUI installation and its model paths under **Engines**. The video runtime requires MiniMax H3 conditioning/sampling nodes, MMH3 Media and the SLA attention extension used by the recipes. The image runtime uses native H3, Qwen or Z-Image nodes and the matching weights. The graph is validated against the connected server on preparation and on saved video workflow submission. Standard installations under the current user's ComfyUI-Installs folder can be discovered automatically. Override the source, Python interpreter or model-path YAML in Engines for other layouts.

The default Studio-managed ports are 8021 for video and 8022 for images. A dedicated engine writes its inputs, outputs, temporary files and ownership receipt into the Studio home. You can also supply a loopback URL for an existing managed-elsewhere renderer. The shared queue records job identifiers and verifies output hashes. The game guard pauses new dispatch and handles owned work on supported games. Director requires a running LM Studio endpoint with the exact selected model. Finishing requires a compatible [UPSCALE installation](https://github.com/BesianSherifaj-AI/UPSCALE) with its own environment. Auto and manual finishing modes run through its callable worker. Compare source and finished takes on a shared playback clock, including material finished at a different frame rate.

## Frame and project formats

H3 timing uses 24 fps. A five-second delivery contains 120 frames with indices 0–119. A new H3 shot renders 124 frames. With a 39-frame carried prefix, five new seconds render 175 frames and the displayed last frame maps to raw frame 158. The raw final indices would be 123 and 174 respectively. Five to 21 guide frames retain five frames, with longer guides rounded down to the 5 + 17k grid. A four-frame guide supplies one still. A retained video guide must fit inside the delivered shot, and guide audio is cropped to the remaining time. Timed video guides sample the source presentation timestamps at 24 fps before retention. The review shows the source, sampled and retained counts, dimensions and center crop.

A project folder contains a versioned SQLite database named `studio.h3project` and content-addressed assets. Move or back up the whole folder to keep layer history, footage, guides, takes and verified `.mmh3` motion packets together. Exported films are ordinary media files. The default local data folder is `%LOCALAPPDATA%\H3 Creative Studio`. Open the recovery tool for Prompt Studio Film/Game histories, Atelier SQLite state, ZIP archives and missing-file relinking. Under **Shot → H3 motion masters**, assemble a connected chain of verified motion packets and inspect the 40 Hz audio boundary check. The native motion master includes model padding. Export film applies the editorial trims. The working canvas supports 8,192 pixels per side and 24 megapixels. The media decoder accepts up to 33,554,432 pixels per frame. AI model working sizes vary by operation.

## Windows acceptance

The desktop shortcut was used to edit and reopen a layered still, place three guide revisions and export a five-second, 120-frame storyboard. A separate RTX 4090 session rendered three real H3 variations and a continuation, then the exported six-second audiovisual film was decoded through all 144 frames. With a fixed seed the mean pixel error against the middle guide at frame 36 was 4.86/255 when placed there and 25.99/255 when omitted. Moving the guide to frame 52 reduced the error there from 21.92 to 4.77/255. The exported shot endings matched the intended raw frames, including the continuation's 39-frame prefix. A separate five-second endpoint test decoded the complete 10-second film. Delivered frames 119 and 239 matched raw frames 119 and 158. A 13-second version added a new GPU-rendered shot from a painted sail on the visible ending. The first frame of the new shot retained that edit. Native packet assembly also ran on the GPU. The two-shot latent route produced 158 raw master frames. A separate three-shot chain (73, 124 and 209 raw frames) triggered the one-tick 40 Hz audio shortfall and used the streaming decoded route. Its 328 master frames and audio decoded in full. Its editorial cut contains 312 frames.

The GPU image session generated a still, removed a lemon beside a teapot and recolored a leather jacket. All protected pixels matched the originals. The revised smooth-surface repair reduced the mean color error on a patch of the tabletop from 7.63 to 1.25 levels on an 8-bit scale. Textured regions can use the generated fill. The verified leather recolor preserved luminance within 0.5 levels. A separate moving-subject fixture preserved 3,401,600 protected pixels across 60 frames, including ten hidden frames, and retained the three flash/click pairs during desktop playback. It used manually corrected tracking anchors on a rigid subject.

UPSCALE Auto produced a 2× spatial CUDA/NVENC result with all 60 test frames and matching audio. In a separate native desktop session, the selected Qwen model returned a two-shot Film storyboard and three Game turns. Both sibling Game choices and the selected branch survived reopening. These machine-specific checks are separate from the automated tests below.

## Test the clone

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

The tests cover portable revisions and recovery, frame math, workflow reload, worker ownership, masks, tracked edits, mixed frame rates, audio routing and delivery decode. Real GPU creative quality and native desktop interaction are separate acceptance checks. The optional `scripts/verify_desktop.py` driver uses Playwright and a debugging-enabled WebView2 test window on a disposable project. The [publication allowlist](SOURCE_ALLOWLIST.txt) names every file in the public source tree. See [source and runtime notices](THIRD_PARTY_NOTICES.md).
