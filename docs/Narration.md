# Narration and captions

Open **Edit → Narration** and enter a script. **Film → Narrate shot** uses the selected storyboard row. **Game → Narrate turn** uses the current scene and keeps its available turn, shot and take references with the clip. **Examples → Voice the signal** opens an editable lesson.

1. Give the clip a title and speaker. Choose a voice and speaking speed. On Windows, the first installed desktop voice is selected automatically. New lines for the same speaker reuse the most recent saved voice settings.
2. Expand **Pronunciation guide** for names the engine mispronounces. Write one entry per line, such as **Tidal = Tide-al**. Matching is case-sensitive and applies to whole names. Captions retain the spelling in your script.
3. Select **Generate voice**. Play the WAV in the narration library. Choose **Edit script** and generate another version to audition a different voice or reading. Each version is a separate clip.
4. Set the start time in seconds, gain in dB and output audio track. Choose **Add to timeline**. A narration opened from Edit starts at the playhead. A narration opened from an active Film or Game shot starts at that shot’s position in the cut. **Update placement** moves the same clip. The film soundtrack controls also let you remove its placement.
5. **Save SRT** exports the selected clip’s timed captions. An unplaced clip starts at zero. A placed clip uses its saved film offset. **Export film** can include placed narration captions as selectable subtitles in the MP4. The export dialog initially includes every assigned audio track. A smaller output count includes only the first tracks.

The WAV, script, voice, pronunciation guide, source and cue timings are saved inside the project. You can reopen or move the project and play existing narration without the speech engine. Each caption passage has the actual sample boundaries of its synthesized audio. For tighter editorial control, put each desired cue on its own line before generating.

## Local voices

**Installed Windows voice** uses installed SAPI desktop voices. It works offline and requires no speech model download. Available names and languages depend on Windows.

**Local speech server** sends the script to an HTTP server on the same computer. Enter its address and port, then use **Find installed models**. The Studio adapter advertises both models and voice IDs. Other compatible servers can be configured by entering an exact model and voice ID. They must accept POST requests to /v1/audio/speech with model, voice, input, speed and response_format set to wav. They must return uncompressed 16-bit PCM WAV. The entire clip must keep the same channels and sample rate.

### Kokoro on CPU

Use an isolated Python environment with **kokoro-onnx** installed. Download the publisher-linked **kokoro-v1.0.onnx** and **voices-v1.0.bin** files. The Studio adapter reads these files when it starts. The tested environment used Python 3.12 and kokoro-onnx 0.6.1. [Kokoro model card](https://huggingface.co/hexgrad/Kokoro-82M) · [ONNX setup and model downloads](https://github.com/thewh1teagle/kokoro-onnx#setup)

Run this command in the Studio folder using that environment’s Python. Replace the paths with your local files.

~~~powershell
python scripts/local_speech.py --kokoro-model "C:\Models\Kokoro\kokoro-v1.0.onnx" --kokoro-voices "C:\Models\Kokoro\voices-v1.0.bin"
~~~

In Studio select **Local speech server**, enter **http://127.0.0.1:8880** and click **Find installed models**. Select **hexgrad/Kokoro-82M** and **af_heart**, or another English voice returned by the server. The community tour uses the synthetic af_heart voice. Hexgrad publishes the model under Apache-2.0 and documents its training sources and attributions. [Model credits](https://huggingface.co/hexgrad/Kokoro-82M#creative-commons-attribution) · [Available voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)

### VibeVoice Realtime research setup

Use the VibeVoice environment’s Python with the official code checkout, voice presets, model weights and tokenizer already cached. Follow Microsoft’s intended-use guidance for this research model. [Official realtime setup](https://github.com/microsoft/VibeVoice/blob/main/docs/vibevoice-realtime-0.5b.md) · [Model card and intended use](https://huggingface.co/microsoft/VibeVoice-Realtime-0.5B#responsible-usage)

~~~powershell
python scripts/local_speech.py --checkout "C:\Tools\VibeVoice"
~~~

Select **microsoft/VibeVoice-Realtime-0.5B** and a listed voice, such as **en-Grace_woman** when installed. Adjusting speed away from 1× with this adapter requires FFmpeg on the service’s PATH. Both adapters bind to loopback and use four CPU threads by default. Set **--port** or **--threads** to change these values. Close the server with Ctrl+C to release its model.
