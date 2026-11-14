"""Голосовой отпечаток: POST /embed (multipart file=<wav>) -> {"vector": [...], "model", "speech_sec"}.

sherpa-onnx + ONNX-модель эмбеддингов говорящего (CPU). Сервис без состояния: профили и их голоса
хранит шлюз, он же сравнивает векторы (косинус). Модель качается в /models при первом старте.
"""
import contextlib
import io
import os
import urllib.request
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

MODEL = os.getenv("VOICEID_MODEL", "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k")
MODEL_URL = os.getenv("VOICEID_MODEL_URL",
                      "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{model}.onnx")
MODELS_DIR = Path(os.getenv("VOICEID_MODELS_DIR", "/models"))
THREADS = int(os.getenv("VOICEID_THREADS", "2"))
MAX_BYTES = int(os.getenv("MAX_AUDIO_BYTES", "5000000"))
RATE = 16000

_extractor: sherpa_onnx.SpeakerEmbeddingExtractor | None = None


def _model_path() -> Path:
    path = MODELS_DIR / f"{MODEL}.onnx"
    if not path.exists():
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        print(f"Скачиваю {MODEL}", flush=True)
        urllib.request.urlretrieve(MODEL_URL.format(model=MODEL), tmp)
        tmp.replace(path)
    return path


def _load() -> sherpa_onnx.SpeakerEmbeddingExtractor:
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_model_path()), num_threads=THREADS)
    if not cfg.validate():
        raise RuntimeError(f"плохая модель {MODEL}")
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _extractor
    _extractor = await run_in_threadpool(_load)
    yield


app = FastAPI(title="voiceid", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def read_wav(data: bytes) -> np.ndarray:
    """wav (16-bit PCM, моно/стерео, любая частота) -> float32 моно 16 кГц."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("нужен 16-bit PCM")
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != RATE and len(x):
        n = int(len(x) * RATE / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def speech_seconds(x: np.ndarray) -> float:
    """Грубо: сколько секунд заметно громче фона (кадры по 30 мс)."""
    frame = int(RATE * 0.03)
    if len(x) < frame:
        return 0.0
    rms = np.sqrt(np.mean(x[: len(x) // frame * frame].reshape(-1, frame) ** 2, axis=1))
    floor = max(float(np.percentile(rms, 10)), 1e-4)
    return float(np.sum(rms > max(floor * 3, 0.01)) * 0.03)


def embed(x: np.ndarray) -> list[float]:
    s = _extractor.create_stream()
    s.accept_waveform(RATE, x)
    s.input_finished()
    if not _extractor.is_ready(s):
        raise ValueError("слишком коротко")
    return [round(float(v), 6) for v in _extractor.compute(s)]


@app.post("/embed")
async def embed_route(file: UploadFile = File(...)):
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "audio too large")
    try:
        x = read_wav(data)
        vector = await run_in_threadpool(embed, x)
    except (ValueError, wave.Error, EOFError) as e:
        raise HTTPException(422, str(e)) from e
    return {"vector": vector, "model": MODEL, "speech_sec": round(speech_seconds(x), 2)}


@app.get("/health")
def health():
    return {"ok": _extractor is not None, "model": MODEL}
