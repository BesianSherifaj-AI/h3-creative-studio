# Run H3 Creative Studio

1. Extract the complete ZIP into a folder you intend to keep. A short path such as C:\Apps is convenient.
2. Open **H3 Creative Studio.exe**. The desktop window uses Microsoft Edge WebView2. The launcher offers Microsoft's Evergreen Runtime page if Windows needs it.
3. Your first launch opens the editable three-shot **Tidal Relay** example. **Example film** reopens your copy. In **Edit**, click a clip, drag its edges, split at the playhead, and play the film with its selected audio track. Use **Undo** and **Redo** to recover a cut.
4. For media playback, export and frame extraction, the setup panel under **?** can install FFmpeg and ffprobe for Studio. A compatible full FFmpeg build on PATH also works.
5. For generation, open **Engines** and connect your installed H3 video, image, Director or UPSCALE services. Their separate environments and model files can stay in their original locations.

**Create desktop shortcut.cmd** creates an optional shortcut to this portable copy. If you move this folder, run that command again.

Projects and preferences default to %LOCALAPPDATA%\H3 Creative Studio. Open existing project folders from the Project menu. For an independent workspace, run the desktop executable with --home and optionally --project.

    "H3 Creative Studio.exe" --home "D:\Studio Data" --project "D:\Films\Example film"

The ZIP supplies the Windows x64 Python environment. The community launcher is unsigned. Sources and original project notices are inside app. Runtime component notices are in notices.
