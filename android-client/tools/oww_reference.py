"""Эталон для WakeWord.kt: тот же упрощённый потоковый конвейер openWakeWord на onnxruntime.

python oww_reference.py <папка с моделями> <папка для эталона> <wav 16 кГц моно>...
Для каждого wav: перед ним 1.5 с тишины (с детерминированным шумом ±2), после — 0.5 с; кадры по 1280.
Пишет в папку эталона padded_<имя>.wav (ровно тот звук, что подан в модель) и reference.json {имя: {"scores": [...], "mel0": [...], "emb0": [...]}} и для сверки — максимум score
оригинального openwakeword.Model на том же звуке (в stderr).
"""
import json
import sys
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort

CHUNK, CONTEXT = 1280, 480


def load(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    rng = np.random.default_rng(0)
    pre = rng.integers(-2, 3, 24000).astype(np.int16)
    post = rng.integers(-2, 3, 8000).astype(np.int16)
    x = np.concatenate([pre, x, post])
    return x[: len(x) // CHUNK * CHUNK]


class Pipeline:
    def __init__(self, models: Path, wake: str):
        o = ort.SessionOptions()
        o.intra_op_num_threads = o.inter_op_num_threads = 1
        self.mel = ort.InferenceSession(str(models / "melspectrogram.onnx"), o, providers=["CPUExecutionProvider"])
        self.emb = ort.InferenceSession(str(models / "embedding_model.onnx"), o, providers=["CPUExecutionProvider"])
        self.ww = ort.InferenceSession(str(models / wake), o, providers=["CPUExecutionProvider"])
        self.raw = np.zeros(0, dtype=np.int16)
        self.mel_buf = np.ones((76, 32), dtype=np.float32)
        self.feat = np.zeros((16, 96), dtype=np.float32)
        self.first = None

    def step(self, chunk):
        self.raw = np.concatenate([self.raw, chunk])[-(CHUNK + CONTEXT):]
        x = self.raw.astype(np.float32)[None, :]
        m = np.squeeze(self.mel.run(None, {self.mel.get_inputs()[0].name: x})[0]) / 10 + 2
        self.mel_buf = np.vstack([self.mel_buf, m])[-970:]
        win = self.mel_buf[-76:].astype(np.float32)[None, :, :, None]
        e = np.squeeze(self.emb.run(None, {self.emb.get_inputs()[0].name: win})[0]).astype(np.float32)
        self.feat = np.vstack([self.feat, e[None]])[-120:]
        s = float(np.squeeze(self.ww.run(None, {self.ww.get_inputs()[0].name: self.feat[-16:][None]})[0]))
        if self.first is None:
            self.first = {"mel0": m.reshape(-1).tolist(), "emb0": e.tolist()}
        return s


def main():
    models, dst, wavs = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    out = {}
    for w in wavs:
        x = load(w)
        with wave.open(str(dst / f"padded_{Path(w).name}"), "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(x.tobytes())
        p = Pipeline(models, "hey_jarvis_v0.1.onnx")
        scores = [p.step(x[i:i + CHUNK]) for i in range(0, len(x), CHUNK)]
        out[Path(w).name] = {"scores": scores, **p.first}
        try:
            from openwakeword.model import Model
            m = Model(wakeword_models=[str(models / "hey_jarvis_v0.1.onnx")], inference_framework="onnx",
                      melspec_model_path=str(models / "melspectrogram.onnx"),
                      embedding_model_path=str(models / "embedding_model.onnx"))
            orig = [list(m.predict(x[i:i + CHUNK]).values())[0] for i in range(0, len(x), CHUNK)]
            print(f"{Path(w).name}: max ours={max(scores):.3f} openwakeword={max(orig):.3f}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"openwakeword для сверки недоступен: {e!r}", file=sys.stderr)
    (dst / "reference.json").write_text(json.dumps(out))


if __name__ == "__main__":
    main()
