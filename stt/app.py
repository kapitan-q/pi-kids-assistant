"""Распознавание речи: faster-whisper на CPU. POST /transcribe (multipart file) -> {"text": ...}"""
import io
import os
import threading
import time

from fastapi import FastAPI, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from faster_whisper import WhisperModel

MODEL = os.getenv("WHISPER_MODEL", "small")
COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
LANG = os.getenv("WHISPER_LANG", "ru")
THREADS = int(os.getenv("WHISPER_THREADS", "4"))
# подсказка словаря — помогает с именами героев
PROMPT = os.getenv("WHISPER_PROMPT", "Включи сказку про Колобка. Стоп. Продолжай. Давай другую.")

app = FastAPI(title="stt")
_model: WhisperModel | None = None
_lock = threading.Lock()


@app.on_event("startup")
def load_model() -> None:
    global _model
    # при первом запуске модель скачается в /models (volume)
    _model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE,
                          cpu_threads=THREADS, download_root="/models")


def _transcribe(data: bytes) -> dict:
    t0 = time.monotonic()
    with _lock:  # одна модель — по одному запросу за раз
        segments, info = _model.transcribe(
            io.BytesIO(data), language=LANG, beam_size=1,
            vad_filter=True, initial_prompt=PROMPT,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
    return {"text": text, "duration": round(info.duration, 2),
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    return await run_in_threadpool(_transcribe, await file.read())


@app.get("/health")
def health():
    return {"ok": _model is not None, "model": MODEL}
