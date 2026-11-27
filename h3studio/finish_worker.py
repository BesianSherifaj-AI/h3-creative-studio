"""Isolated Studio worker. Its spool and cancellation token belong to one job."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path


def atomic(path, value):
    tmp = path.with_suffix("." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


async def _storyboard(payload):
    import httpx
    from vendor.prompt.resources import local_url
    base = local_url(payload["url"]).removesuffix("/v1") + "/v1"
    model = str(payload["model"]).strip()
    if not model:
        raise ValueError("Select the exact local Director model.")
    rule = {"game": "game-engine.txt", "film": "cinematic-fl2va.txt"}[payload.get("mode", "film")]
    root = Path(__file__).resolve().parents[1]
    instruction = (root / "vendor" / "prompt" / "system-prompts" / rule).read_text(encoding="utf-8-sig")
    if payload.get("director_request"):
        instruction += ("\nThis Studio request is a production planning step. Use the user's requested JSON schema "
                        "instead of the single-video prompt format. Return only JSON. Preserve the directing and continuity rules.")
    async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
        models = await client.get(base + "/models")
        models.raise_for_status()
        if model not in {item["id"] for item in models.json()["data"]}:
            raise ValueError("The selected Director model is unavailable: " + model)
        messages = [{"role": "system", "content": instruction}, {"role": "user", "content": payload["prompt"]}]
        pieces = []
        model_confirmed = False
        async with client.stream("POST", base + "/chat/completions", json={"model": model, "messages": messages, "stream": True}) as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                if event.get("model"):
                    if event["model"] != model:
                        raise ValueError("The Director responded using a different model.")
                    model_confirmed = True
                for choice in event.get("choices", []):
                    pieces.append(choice.get("delta", {}).get("content") or "")
    if not model_confirmed:
        raise ValueError("The Director did not identify the model that produced the response.")
    result = "".join(pieces).strip()
    if not result:
        raise ValueError("The selected Director returned an empty storyboard.")
    return {"model": model, "mode": payload.get("mode", "film"), "storyboard": result, "approved": False}


def direct(payload, cancel):
    async def run_selected():
        if cancel.is_set():
            raise InterruptedError("Director cancelled.")
        task = asyncio.create_task(_storyboard(payload))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.15)
                if cancel.is_set():
                    raise InterruptedError("Director cancelled.")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(run_selected())


def run(folder):
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    # A duplicate launcher racing recovery cannot enter process_video or LM Studio.
    try:
        with (folder / "claim").open("x") as claim:
            claim.write(str(os.getpid()))
    except FileExistsError:
        return
    cancel, stop = threading.Event(), threading.Event()
    def watch():
        while not stop.wait(.25):
            if (folder / "cancel").exists():
                cancel.set()
                return
    threading.Thread(target=watch, daemon=True).start()
    def progress(percent, message):
        atomic(folder / "progress.json", {"progress": percent, "message": message})
    try:
        p = request["payload"]
        if (folder / "cancel").exists():
            raise InterruptedError("Cancelled before starting.")
        if request["kind"] == "finish":
            sys.dont_write_bytecode = True
            sys.path.insert(0, p["installation"])
            import backend
            settings = backend.Settings(**{"workflow": "auto", **p.get("settings", {})})
            destination = (folder / "outputs").resolve()
            hardware = {}
            if hasattr(backend, "environment_info"):
                # Resolve installed binaries before directing per-stage logs into this job.
                hardware = backend.environment_info()
                if hasattr(backend, "APP_DIR"):
                    backend.APP_DIR = folder
            result = backend.process_video(p["input"], settings, destination, progress, cancel)
            output = Path(result["output_path"]).resolve(strict=True)
            if result.get("status") != "success" or not output.is_relative_to(destination):
                raise ValueError("UPSCALE did not return a successful output for the owned job.")
            with output.open("rb") as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result["sha256"] = digest.hexdigest()
            if result.get("report_path"):
                report = Path(result["report_path"]).resolve(strict=True)
                if not report.is_relative_to(destination):
                    raise ValueError("UPSCALE returned a report outside the owned job.")
                result["report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
            result["worker_devices"] = hardware.get("gpus", [])
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            result = direct(p, cancel)
        receipt = {"status": "succeeded", "result": result}
    except Exception as exc:
        receipt = {"status": "cancelled" if cancel.is_set() or isinstance(exc, InterruptedError) else "failed", "error": str(exc)}
    finally:
        stop.set()
    atomic(folder / "result.json", {**receipt, "job_id": request["id"], "pid": os.getpid(), "completed_at": time.time()})


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve(strict=True))
