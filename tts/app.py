"""Синтез речи: Piper. POST /synthesize {"text": ...} -> audio/wav"""
import contextlib
import io
import os
import threading
import urllib.request
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from piper import PiperVoice
from pydantic import BaseModel

VOICE = os.getenv("PIPER_VOICE", "ru_RU-irina-medium")
LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.1"))
VOICES_DIR = Path(os.getenv("PIPER_VOICES_DIR", "/voices"))
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "1000"))


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _voice
    _voice = await run_in_threadpool(lambda: PiperVoice.load(str(_download_voice(VOICE))))
    yield


app = FastAPI(title="tts", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
_voice: PiperVoice | None = None
_lock = threading.Lock()


def _download_voice(name: str) -> Path:
    """ru_RU-irina-medium -> ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx(.json)"""
    lang_region, speaker, quality = name.split("-")
    lang = lang_region.split("_")[0]
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    onnx = VOICES_DIR / f"{name}.onnx"
    for suffix in (".onnx", ".onnx.json"):
        target = VOICES_DIR / f"{name}{suffix}"
        if not target.exists():
            url = f"{HF_BASE}/{lang}/{lang_region}/{speaker}/{quality}/{name}{suffix}"
            print(f"Скачиваю {url}")
            urllib.request.urlretrieve(url, target)
    return onnx


# piper-tts 1.3+ (synthesize_wav + SynthesisConfig) и 1.2 (synthesize с kwargs)
try:
    from piper import SynthesisConfig

    def _synth(voice: PiperVoice, text: str, wf: wave.Wave_write) -> None:
        voice.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=LENGTH_SCALE))
except ImportError:
    def _synth(voice: PiperVoice, text: str, wf: wave.Wave_write) -> None:
        voice.synthesize(text, wf, length_scale=LENGTH_SCALE)


class SynthIn(BaseModel):
    text: str


def _render(text: str) -> bytes:
    buf = io.BytesIO()
    with _lock, wave.open(buf, "wb") as wf:
        _synth(_voice, text, wf)
    return buf.getvalue()


@app.post("/synthesize")
async def synthesize(body: SynthIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty text")
    text = text[:MAX_CHARS]
    audio = await run_in_threadpool(_render, text)
    return Response(content=audio, media_type="audio/wav")


@app.get("/health")
def health():
    return {"ok": _voice is not None, "voice": VOICE}
