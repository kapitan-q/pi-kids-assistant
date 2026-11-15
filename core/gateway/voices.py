"""Голоса профилей: data/core/voices/<id>/ — образцы (wav) и усреднённый эмбеддинг.

Образцы хранятся, чтобы пересчитать эмбеддинг, если сменится модель voiceid.
Удаление профиля удаляет и голос.
"""
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("voices")
DIR = Path(os.getenv("CORE_DATA_DIR", "/data/core")) / "voices"


def _mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(col) / n for col in zip(*vectors)]


def mean(vectors: list[list[float]]) -> list[float]:
    return _mean(vectors)


def cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class VoiceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        self._cache: dict[str, list[float]] | None = None

    def _dir(self, pid: str) -> Path:
        return self.root / pid

    def embeddings(self) -> dict[str, list[float]]:
        """{profile_id: вектор} всех профилей с записанным голосом."""
        with self.lock:
            if self._cache is None:
                self._cache = {}
                for f in self.root.glob("*/embedding.json"):
                    try:
                        self._cache[f.parent.name] = json.loads(f.read_text())["vector"]
                    except (ValueError, KeyError):
                        log.exception("битый %s", f)
            return dict(self._cache)

    def identify(self, vector: list[float] | None) -> tuple[str | None, float]:
        """Ближайший профиль и близость (0..1). Решение «узнала/нет» — по порогу у вызывающего."""
        if not vector:
            return None, 0.0
        best, score = None, 0.0
        for pid, v in self.embeddings().items():
            if len(v) != len(vector):        # голос записан другой моделью voiceid — сравнивать нельзя
                log.warning("голос %s: другая размерность (%d≠%d) — нужна перезапись", pid, len(v), len(vector))
                continue
            s = cos(vector, v)
            if s > score:
                best, score = pid, s
        return best, score

    def save(self, pid: str, samples: list[bytes], vectors: list[list[float]], model: str) -> int:
        """Записать (или дописать) голос профиля: wav-образцы + новый средний эмбеддинг."""
        d = self._dir(pid)
        with self.lock:
            (d / "samples").mkdir(parents=True, exist_ok=True)
            start = len(list((d / "samples").glob("*.wav")))
            for i, wav in enumerate(samples):
                (d / "samples" / f"{start + i:03d}.wav").write_bytes(wav)
            old = []
            emb = d / "embedding.json"
            if emb.exists():
                prev = json.loads(emb.read_text())
                if prev.get("model") == model:
                    old = prev.get("vectors", [])
            allv = old + vectors
            emb.write_text(json.dumps({"model": model, "vector": _mean(allv), "vectors": allv,
                                       "updated_at": time.time()}))
            self._cache = None
            return len(allv)

    def move(self, src: str, dst: str) -> int:
        """Голос src → dst (объединение профилей); у dst старый голос заменяется. Возвращает число образцов."""
        with self.lock:
            s, d = self._dir(src), self._dir(dst)
            if not s.exists():          # у src голоса нет — у dst остаётся свой
                return self._count(d)
            if d.exists():
                shutil.rmtree(d)
            s.rename(d)
            self._cache = None
            return self._count(d)

    @staticmethod
    def _count(d: Path) -> int:
        try:
            return len(json.loads((d / "embedding.json").read_text()).get("vectors", []))
        except (OSError, ValueError):
            return 0

    def delete(self, pid: str) -> None:
        with self.lock:
            shutil.rmtree(self._dir(pid), ignore_errors=True)
            self._cache = None


store = VoiceStore(DIR)
