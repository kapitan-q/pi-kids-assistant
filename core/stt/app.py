"""Распознавание речи: faster-whisper на CPU. POST /transcribe (multipart file) -> {"text": ...}

Whisper на тишине и шуме «галлюцинирует» (на русском — «Продолжение следует...»,
«Субтитры сделал ...»), а initial_prompt может вернуться дословно. Поэтому:
- в подсказке только словарь имён, без командных фраз;
- сегменты с высокой вероятностью «не речи» или низкой уверенностью отбрасываются;
- известные галлюцинации вырезаются по стоп-листу.
"""
import contextlib
import io
import os
import re
import threading
import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from faster_whisper import WhisperModel

MODEL = os.getenv("WHISPER_MODEL", "small")
COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
LANG = os.getenv("WHISPER_LANG", "ru")
THREADS = int(os.getenv("WHISPER_THREADS", "4"))
MAX_BYTES = int(os.getenv("MAX_AUDIO_BYTES", "5000000"))
NO_SPEECH_MAX = float(os.getenv("WHISPER_NO_SPEECH_MAX", "0.6"))
LOGPROB_MIN = float(os.getenv("WHISPER_LOGPROB_MIN", "-1.0"))
# подсказка словаря — только имена героев и авторов, никаких команд
PROMPT = os.getenv("WHISPER_PROMPT",
                   "Колобок, Баба-яга, Емеля, Кощей, Маша и медведь, Теремок, Репка, "
                   "Пушкин, Чуковский, Айболит, Мойдодыр, Буратино, Чебурашка.")

HALLUCINATIONS = [
    r"продолжение следует",
    r"редактор субтитров.*",
    r"корректор.*",
    r"субтитры? (сделал|создавал|делал)\w*.*",
    r"субтитр\w*",
    r"спасибо за просмотр\w*",
    r"подписывайтесь на канал\w*",
    r"ставьте лайк\w*",
    r"dimatorzok",
    r"amara\.org",
    r"музыка",
    r"аплодисменты",
]
_HALLU_RE = re.compile(r"\b(" + "|".join(HALLUCINATIONS) + r")\b", re.IGNORECASE)

_model: WhisperModel | None = None
_lock = threading.Lock()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _model
    # при первом запуске модель скачается в /models (volume)
    _model = await run_in_threadpool(
        WhisperModel, MODEL, device="cpu", compute_type=COMPUTE,
        cpu_threads=THREADS, download_root="/models")
    yield


app = FastAPI(title="stt", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def clean(text: str) -> str:
    text = _HALLU_RE.sub(" ", text)
    text = re.sub(r"[\s.,!?…-]+$", "", re.sub(r"\s+", " ", text)).strip()
    # остались одни знаки препинания — это не речь
    return text if re.search(r"\w", text) else ""


def _transcribe(data: bytes) -> dict:
    t0 = time.monotonic()
    with _lock:  # одна модель — по одному запросу за раз
        segments, info = _model.transcribe(
            io.BytesIO(data), language=LANG, beam_size=1,
            vad_filter=True, initial_prompt=PROMPT,
            condition_on_previous_text=False,
        )
        kept, dropped = [], 0
        for s in segments:
            if s.no_speech_prob > NO_SPEECH_MAX or s.avg_logprob < LOGPROB_MIN:
                dropped += 1
                continue
            kept.append(s.text.strip())
    text = clean(" ".join(kept))
    return {"text": text, "duration": round(info.duration, 2), "dropped_segments": dropped,
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "audio too large")
    return await run_in_threadpool(_transcribe, data)


@app.get("/health")
def health():
    return {"ok": _model is not None, "model": MODEL}
