"""Serve a separately installed CPU speech model to Studio over local HTTP.

In an environment with kokoro-onnx, provide its downloaded model and voices:
    python local_speech.py --kokoro-model kokoro-v1.0.onnx --kokoro-voices voices-v1.0.bin
In the VibeVoice environment, provide its checkout, cached weights and official demo presets:
    python local_speech.py --checkout PATH_TO_VIBEVOICE

Uses CPU, one request at a time. Ctrl+C closes the server and releases the model.
"""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit


VIBEVOICE = "microsoft/VibeVoice-Realtime-0.5B"
KOKORO = "hexgrad/Kokoro-82M"


def load_service(checkout, model_path, threads):
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    script = checkout / "demo" / "web" / "app.py"
    if not script.is_file():
        raise ValueError("Choose the VibeVoice checkout containing demo/web/app.py and its official demo voice presets.")
    sys.path.insert(0, str(checkout))
    import torch
    torch.set_num_threads(threads)
    spec = importlib.util.spec_from_file_location("h3_vibevoice_upstream", script)
    upstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    service = upstream.StreamingTTSService(model_path, device="cpu")
    service.load()
    service.model_id = VIBEVOICE
    return service


class KokoroService:
    model_id = KOKORO
    sample_rate = 24000

    def __init__(self, model, voices, threads):
        from kokoro_onnx import Kokoro
        from onnxruntime import InferenceSession, SessionOptions
        options = SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        session = InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self.model = Kokoro.from_session(session, str(voices))
        self.voice_presets = {key for key in self.model.get_voices() if key.startswith(("af_", "am_", "bf_", "bm_"))}

    def pcm(self, text, voice, speed):
        import numpy as np
        language = "en-us" if voice.startswith("a") else "en-gb"
        samples, rate = self.model.create(text, voice=voice, speed=speed, lang=language)
        if not len(samples) or rate != self.sample_rate or not np.isfinite(samples).all():
            raise RuntimeError("Kokoro returned invalid audio.")
        return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def synthesize(service, payload):
    if not isinstance(payload, dict) or payload.get("model") != service.model_id:
        raise ValueError(f"Use model {service.model_id}.")
    voice, text = payload.get("voice"), payload.get("input")
    if not isinstance(voice, str) or voice not in service.voice_presets:
        raise ValueError("Choose an exact official preset name listed by the server.")
    if not isinstance(text, str) or not text.strip() or len(text) > 700:
        raise ValueError("Use between 1 and 700 characters per request.")
    if payload.get("response_format", "wav") != "wav":
        raise ValueError("Choose WAV as the response format.")
    speed = float(payload.get("speed", 1))
    if not 0.5 <= speed <= 2:
        raise ValueError("Speaking speed must be between 0.5 and 2.")
    if service.model_id == KOKORO:
        pcm = service.pcm(text, voice, speed)
    else:
        pcm = vibevoice_pcm(service, text, voice, speed)
    sink = io.BytesIO()
    with wave.open(sink, "wb") as out:
        out.setparams((1, 2, service.sample_rate, 0, "NONE", ""))
        out.writeframes(pcm)
    return sink.getvalue()


def vibevoice_pcm(service, text, voice, speed):
    ffmpeg = shutil.which("ffmpeg") if speed != 1 else None
    if speed != 1 and not ffmpeg:
        raise ValueError("Install FFmpeg on PATH for rate adjustment, or use 1× speed.")
    parts = []
    total = 0
    for chunk in service.stream(text, voice_key=voice):
        samples = service.chunk_to_pcm16(chunk)
        total += len(samples)
        if total > 2 * service.sample_rate * 240:
            raise ValueError("The generated passage exceeded four minutes.")
        parts.append(samples)
    pcm = b"".join(parts)
    if not pcm:
        raise RuntimeError("VibeVoice returned no audio.")
    if ffmpeg:
        rate = str(service.sample_rate)
        pcm = subprocess.run([ffmpeg, "-v", "error", "-f", "s16le", "-ar", rate, "-ac", "1",
                              "-i", "pipe:0", "-af", f"atempo={speed}", "-f", "s16le", "pipe:1"],
                             input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=True, timeout=30).stdout
    return pcm


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_reply(self, code, value):
        self.reply(code, json.dumps(value).encode(), "application/json")

    def is_local(self):
        return (self.headers.get("Origin") is None
                and self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}",
                                                   f"localhost:{self.server.server_port}"})

    def do_GET(self):
        if self.is_local() and self.path == "/v1/models":
            self.json_reply(200, {"data": [{"id": self.server.service.model_id, "device": "cpu",
                                           "voices": sorted(self.server.service.voice_presets)}]})
        else:
            self.json_reply(404, {"error": "Unknown resource."})

    def do_POST(self):
        if not self.is_local() or urlsplit(self.path).path != "/v1/audio/speech":
            return self.json_reply(404, {"error": "Unknown resource."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if self.headers.get_content_type() != "application/json" or not 0 < length <= 16384:
                raise ValueError("Send a small JSON speech request.")
            result = synthesize(self.server.service, json.loads(self.rfile.read(length)))
        except (ValueError, TypeError) as exc:
            return self.json_reply(400, {"error": str(exc)})
        except Exception as exc:
            print(f"Inference error: {exc}", file=sys.stderr, flush=True)
            return self.json_reply(500, {"error": "Local speech inference failed. Check the service console."})
        self.reply(200, result, "audio/wav")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--checkout", type=Path, help="VibeVoice checkout containing demo/web.")
    source.add_argument("--kokoro-model", type=Path, help="Kokoro ONNX model file.")
    parser.add_argument("--kokoro-voices", type=Path, help="Kokoro voice pack.")
    parser.add_argument("--model-path", default=VIBEVOICE, help="VibeVoice cached checkpoint ID or snapshot directory.")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--threads", type=int, default=4, help="CPU workers, 1–16.")
    args = parser.parse_args()
    if not 1 <= args.threads <= 16 or not 1 <= args.port <= 65535:
        parser.error("Use 1–16 CPU workers and a valid TCP port.")
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        if args.checkout:
            server.service = load_service(args.checkout.resolve(), args.model_path, args.threads)
        elif args.kokoro_voices:
            server.service = KokoroService(args.kokoro_model, args.kokoro_voices, args.threads)
        else:
            parser.error("Supply --kokoro-voices with --kokoro-model.")
        print(f"Listening at http://127.0.0.1:{args.port}/v1 · CPU · {server.service.model_id}", flush=True)
        print("Available voices: " + ", ".join(sorted(server.service.voice_presets)), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
