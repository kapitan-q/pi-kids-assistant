#!/usr/bin/env python3
"""Клиент колонки-сказочницы для Raspberry Pi.

Слово активации (openWakeWord) -> запись до тишины (webrtcvad) -> POST /ask ->
голосовой ответ (paplay) -> действие с плеером сказок (mpv через IPC-сокет).
Громкость — системная (pactl), таймер сна — локальный, правила — GET /policy раз в минуту.
"""
import base64
import io
import json
import logging
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import webrtcvad
from openwakeword.model import Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("client")

CFG = tomllib.loads(Path(os.getenv("KS_CONFIG", Path(__file__).with_name("config.toml"))).read_text())
SERVER = CFG["server"]["url"].rstrip("/")
HEADERS = {"X-Token": CFG["server"]["token"]} if CFG["server"].get("token") else {}
# basic-auth на сервере (Traefik): им же закрыты /media и /speech, поэтому mpv тоже шлёт заголовок
AUTH = (CFG["server"]["user"], CFG["server"]["password"]) if CFG["server"].get("user") else None
AUTH_HEADER = ("Authorization: Basic " + base64.b64encode(f"{AUTH[0]}:{AUTH[1]}".encode()).decode()
               if AUTH else "")
ASK_TIMEOUT = float(CFG["server"].get("timeout", 45))
DEVICE_ID = CFG["server"].get("device_id", "kids-room")

RATE = 16000
FRAME = 1280          # 80 мс — столько ждёт openWakeWord
SUB = 320             # 20 мс — столько ест webrtcvad
STATE_DIR = Path.home() / ".local/state/kids-speaker"
CACHE_DIR = Path.home() / ".cache/kids-speaker"


# ---------------------------------------------------------------- звуки

def make_tone(path: Path, tones: list[tuple[float, float]], volume: float = 0.3) -> None:
    """tones: [(частота Гц, длительность с)] — короткие сигналы без внешних файлов."""
    parts = []
    for freq, dur in tones:
        t = np.arange(int(RATE * dur)) / RATE
        env = np.minimum(1, np.minimum(t, dur - t) * 40)  # мягкие края без щелчков
        parts.append(np.sin(2 * np.pi * freq * t) * env * volume)
    write_wav(path, (np.concatenate(parts) * 32767).astype(np.int16))


def write_wav(path_or_buf, samples: np.ndarray) -> None:
    with wave.open(path_or_buf if isinstance(path_or_buf, io.BytesIO) else str(path_or_buf), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


CACHE_DIR.mkdir(parents=True, exist_ok=True)
SOUNDS = {
    "wake": CACHE_DIR / "wake.wav",
    "done": CACHE_DIR / "done.wav",
    "error": CACHE_DIR / "error.wav",
    "listen": CACHE_DIR / "listen.wav",
}
make_tone(SOUNDS["wake"], [(880, 0.12)])
make_tone(SOUNDS["listen"], [(990, 0.06)], volume=0.12)   # тихий «слушаю дальше»
make_tone(SOUNDS["done"], [(660, 0.08)])
make_tone(SOUNDS["error"], [(330, 0.15), (220, 0.25)])


def play_file(path: Path | str) -> None:
    subprocess.run(["paplay", str(path)], check=False, timeout=120)


def play_url(url: str) -> None:
    """Скачать ответ и проиграть: paplay стартует быстрее mpv."""
    r = requests.get(url, headers=HEADERS, auth=AUTH, timeout=30)
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".wav", dir=CACHE_DIR) as f:
        f.write(r.content)
        f.flush()
        play_file(f.name)


# ---------------------------------------------------------------- громкость

class Volume:
    """Системная громкость Bluetooth-синка, шкала 10..100. Хранится между перезапусками."""

    def __init__(self) -> None:
        self.state_file = STATE_DIR / "volume.json"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self.level = json.loads(self.state_file.read_text())["level"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            self.level = int(CFG["player"].get("default_volume", 50))
        self.max_level = 100
        self.apply()

    def apply(self) -> None:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{self.level}%"], check=False)
        self.state_file.write_text(json.dumps({"level": self.level}))

    def set(self, level: int) -> None:
        self.level = max(10, min(self.max_level, round(level / 10) * 10))
        log.info("громкость %s (максимум %s)", self.level, self.max_level)
        self.apply()

    def change(self, delta: int) -> None:
        self.set(self.level + delta)

    def limit(self, max_level: int) -> None:
        self.max_level = max(10, min(100, max_level))
        if self.level > self.max_level:
            self.set(self.max_level)


# ---------------------------------------------------------------- плеер сказок

class TalePlayer:
    """Постоянный mpv в режиме idle, управление через JSON IPC."""

    def __init__(self) -> None:
        self.sock = str(STATE_DIR / "mpv.sock")
        Path(self.sock).unlink(missing_ok=True)
        args = ["mpv", "--idle=yes", "--no-video", "--no-terminal", "--volume=100",
                f"--input-ipc-server={self.sock}"]
        if AUTH_HEADER:
            args.append(f"--http-header-fields={AUTH_HEADER}")
        self.proc = subprocess.Popen(args)
        for _ in range(50):
            if Path(self.sock).exists():
                break
            time.sleep(0.1)
        self.lock = threading.Lock()
        self.stop_at: float | None = None   # monotonic-время автостопа
        self.tale_id: int | None = None     # что сейчас загружено — сервер проверяет правила при «продолжай»
        self.fade_sec = float(CFG["player"].get("fade_sec", 20))

    def cmd(self, *args):
        """Отправить команду и дождаться ответа (строки с полем "error")."""
        with self.lock:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2)
                    s.connect(self.sock)
                    s.sendall((json.dumps({"command": list(args)}) + "\n").encode())
                    buf = b""
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        for line in buf.split(b"\n"):
                            if b'"error"' in line:
                                return json.loads(line)
            except (OSError, json.JSONDecodeError) as e:
                log.warning("mpv IPC: %s", e)
        return None

    def get(self, prop: str):
        r = self.cmd("get_property", prop)
        return r.get("data") if r and r.get("error") == "success" else None

    def loaded(self) -> bool:
        return self.get("idle-active") is False

    def playing(self) -> bool:
        return self.loaded() and not self.get("pause")

    def play(self, url: str, stop_after_sec: int | None, tale_id: int | None = None) -> None:
        self.tale_id = tale_id
        self.cmd("set_property", "volume", 100)
        self.cmd("loadfile", url, "replace")
        self.cmd("set_property", "pause", False)
        self.set_timer(stop_after_sec)

    def pause(self) -> None:
        self.cmd("set_property", "pause", True)

    def resume(self) -> None:
        if self.loaded():
            self.cmd("set_property", "pause", False)

    def stop(self) -> None:
        self.cmd("stop")
        self.cmd("set_property", "volume", 100)
        self.stop_at = None
        self.tale_id = None

    def seek(self, seconds: int, mode: str) -> None:
        if self.loaded():
            self.cmd("seek", seconds, "absolute" if mode == "absolute" else "relative")

    def set_timer(self, sec: int | None) -> None:
        self.stop_at = time.monotonic() + sec if sec else None
        if sec:
            log.info("таймер сна: %s мин", round(sec / 60))

    def tick(self) -> None:
        """Вызывается часто: плавно гасим звук перед автостопом."""
        if self.stop_at is None:
            return
        left = self.stop_at - time.monotonic()
        if left <= 0:
            log.info("таймер сна: стоп")
            self.stop()
        elif left < self.fade_sec:
            self.cmd("set_property", "volume", max(0, int(100 * left / self.fade_sec)))


# ---------------------------------------------------------------- клиент

class Client:
    def __init__(self) -> None:
        self.audio: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.volume = Volume()
        self.player = TalePlayer()
        self.vad = webrtcvad.Vad(int(CFG["audio"].get("vad_aggressiveness", 2)))
        ww = CFG["wakeword"]
        self.threshold = float(ww.get("threshold", 0.5))
        self.oww = Model(wakeword_models=[ww.get("model", "hey_jarvis")], inference_framework="onnx")
        self.busy = threading.Event()

    # --- микрофон

    def _on_audio(self, indata, _frames, _time, status) -> None:
        if status:
            log.debug("audio status: %s", status)
        try:
            self.audio.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # пока заняты ответом — старые кадры не нужны

    def _read_stream(self, url: str) -> None:
        """Микрофон по сети (например, старый телефон с IP Webcam: http://IP:8080/audio.wav).
        ffmpeg приводит поток к 16 кГц моно s16le; при обрыве — переподключение."""
        while True:
            proc = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                 "-i", url, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                stdout=subprocess.PIPE)
            log.info("сетевой микрофон: %s", url)
            try:
                while True:
                    data = proc.stdout.read(FRAME * 2)
                    if len(data) < FRAME * 2:
                        break
                    try:
                        self.audio.put_nowait(np.frombuffer(data, dtype=np.int16).copy())
                    except queue.Full:
                        pass
            finally:
                proc.kill()
            log.warning("сетевой микрофон отвалился, переподключаюсь через 3 с")
            time.sleep(3)

    def drain(self) -> None:
        while not self.audio.empty():
            self.audio.get_nowait()

    def record_utterance(self, no_speech: float | None = None) -> np.ndarray | None:
        a = CFG["audio"]
        end_silence = int(a.get("end_silence_ms", 900))
        if no_speech is None:
            no_speech = float(a.get("no_speech_timeout_sec", 4))
        max_sec = float(a.get("max_record_sec", 10))
        frames, started, silence_ms, t0 = [], False, 0, time.monotonic()
        while True:
            frame = self.audio.get(timeout=2)
            frames.append(frame)
            voiced = sum(self.vad.is_speech(frame[i:i + SUB].tobytes(), RATE)
                         for i in range(0, FRAME, SUB))
            if voiced >= 2:
                started, silence_ms = True, 0
            else:
                silence_ms += 80
            elapsed = time.monotonic() - t0
            if not started and elapsed > no_speech:
                return None
            if started and silence_ms >= end_silence:
                break
            if elapsed > max_sec:
                break
        return np.concatenate(frames)

    # --- сервер

    def ask(self, samples: np.ndarray) -> dict:
        buf = io.BytesIO()
        write_wav(buf, samples)
        now_playing = self.player.tale_id if self.player.loaded() else None
        r = requests.post(f"{SERVER}/ask", headers=HEADERS, auth=AUTH, timeout=ASK_TIMEOUT,
                          files={"audio": ("q.wav", buf.getvalue(), "audio/wav")},
                          data={"device_id": DEVICE_ID, "now_playing": now_playing or ""})
        r.raise_for_status()
        return r.json()

    def apply_limits(self, lim: dict | None) -> None:
        if not lim:
            return
        self.volume.limit(int(lim.get("max_volume", 100)))
        if lim.get("allow_playback") is False and self.player.loaded():
            log.info("режим %s: сказки нельзя — стоп", lim.get("mode"))
            self.player.stop()
        # сказка началась до ночного режима — ставим таймер задним числом
        if lim.get("stop_after_sec") is not None and self.player.stop_at is None and self.player.playing():
            self.player.set_timer(lim["stop_after_sec"])

    def apply_action(self, act: dict | None, lim: dict | None, was_playing: bool) -> None:
        if not act:
            return
        kind = act.get("type")
        log.info("действие: %s", act)
        if kind == "play":
            self.player.play(act["url"], (lim or {}).get("stop_after_sec"), act.get("tale_id"))
        elif kind == "stop":
            self.player.stop()
        elif kind == "resume":
            self.player.resume()
        elif kind == "seek":
            self.player.seek(int(act.get("seconds", 30)), act.get("mode", "relative"))
            self.player.resume()
        elif kind == "volume":
            if act.get("level") is not None:
                self.volume.set(int(act["level"]))
            else:
                self.volume.change(int(act.get("change", 0)))
            if was_playing:
                self.player.resume()

    # --- один диалог

    def _exchange(self, samples: np.ndarray, was_playing: bool) -> dict | None:
        """Отправить фразу, проиграть ответ, выполнить команду. None — сервер не ответил по делу."""
        play_file(SOUNDS["done"])
        try:
            resp = self.ask(samples)
        except Exception as e:  # noqa: BLE001
            log.error("сервер недоступен: %s", e)
            play_file(SOUNDS["error"])
            return None
        log.info("услышал: %r -> %r (%s)", resp.get("heard"), resp.get("reply"), resp.get("timing_ms"))
        if not resp.get("heard"):  # ложное срабатывание
            return None
        self.apply_limits(resp.get("limits"))
        if resp.get("speech_url"):
            try:
                play_url(resp["speech_url"])
            except Exception as e:  # noqa: BLE001
                log.error("не удалось проиграть ответ: %s", e)
        self.apply_action(resp.get("action"), resp.get("limits"), was_playing)
        return resp

    def on_wake(self) -> dict | None:
        was_playing = self.player.playing()
        if was_playing:
            self.player.pause()
        resp = None  # ответ по делу — плеером управляет его action
        try:
            play_file(SOUNDS["wake"])
            self.drain()  # не отправлять на сервер свой же сигнал
            samples = self.record_utterance()
            if samples is None:
                log.info("тишина после слова активации")
                return None
            resp = self._exchange(samples, was_playing)
            return resp
        finally:
            # любая неудача (тишина, сеть, пропал микрофон) — сказка не должна зависнуть на паузе
            if was_playing and resp is None:
                self.player.resume()

    def _wants_follow_up(self, resp: dict | None) -> bool:
        """Продолжаем слушать без слова активации, только если это был разговор:
        колонка что-то сказала, сказку не включали, режим не блокирующий."""
        return bool(resp and resp.get("reply") and not resp.get("action")
                    and not resp.get("blocked") and not resp.get("parent_command")
                    and not self.player.playing())

    def follow_up(self, wait_sec: float) -> dict | None:
        """Ещё одна реплика без «Hey Jarvis»: тихий сигнал и ждём речь wait_sec секунд."""
        d = CFG.get("dialog", {})
        time.sleep(float(d.get("echo_guard_ms", 400)) / 1000)  # хвост ответа ещё звучит в Bluetooth
        self.drain()
        if d.get("follow_up_tone", True):
            play_file(SOUNDS["listen"])
            self.drain()
        samples = self.record_utterance(no_speech=wait_sec)
        if samples is None:
            log.info("продолжения нет — жду слово активации")
            return None
        return self._exchange(samples, was_playing=False)

    def dialog(self) -> None:
        resp = self.on_wake()
        d = CFG.get("dialog", {})
        wait_sec = float(d.get("follow_up_sec", 6))
        for _ in range(int(d.get("max_follow_ups", 5))):
            if wait_sec <= 0 or not self._wants_follow_up(resp):
                return
            resp = self.follow_up(wait_sec)

    # --- фоновые задачи

    def _check_policy(self) -> None:
        try:
            r = requests.get(f"{SERVER}/policy", headers=HEADERS, auth=AUTH, timeout=10)
            r.raise_for_status()
            self.apply_limits(r.json())
        except Exception as e:  # noqa: BLE001
            log.warning("policy: %s", e)

    def _check_bluetooth(self, mac: str) -> None:
        try:
            info = subprocess.run(["bluetoothctl", "info", mac], capture_output=True,
                                  text=True, timeout=10)
            if "Connected: yes" in info.stdout:
                return
            log.info("Bluetooth: переподключаюсь к %s", mac)
            subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=20)
            time.sleep(2)
            self.volume.apply()  # новый синк — выставить громкость заново
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("Bluetooth: %s", e)

    def background(self) -> None:
        """Таймер сна, правила и Bluetooth. Ни одна ошибка не должна убить этот поток:
        без него ночью не сработает автостоп."""
        last_policy = last_bt = 0.0
        mac = CFG.get("bluetooth", {}).get("speaker_mac", "")
        while True:
            try:
                now = time.monotonic()
                if now - last_policy > 60 and not self.busy.is_set():
                    last_policy = now
                    self._check_policy()
                if mac and now - last_bt > 30:
                    last_bt = now
                    self._check_bluetooth(mac)
                self.player.tick()
            except Exception:  # noqa: BLE001
                log.exception("ошибка в фоновом цикле")
            time.sleep(0.5)

    # --- главный цикл

    def listen_loop(self) -> None:
        log.info("слушаю (слово активации: %s, порог %.2f)", CFG["wakeword"].get("model"), self.threshold)
        while True:
            frame = self.audio.get()
            scores = self.oww.predict(frame)
            score = max(scores.values()) if scores else 0
            if score >= self.threshold:
                log.info("слово активации (%.2f)", score)
                self.busy.set()
                try:
                    self.dialog()
                except Exception:  # noqa: BLE001
                    log.exception("ошибка в диалоге")
                finally:
                    self.busy.clear()
                    self.oww.reset()
                    self.drain()

    def run(self) -> None:
        threading.Thread(target=self.background, daemon=True).start()
        stream_url = CFG["audio"].get("stream_url", "")
        if stream_url:
            threading.Thread(target=self._read_stream, args=(stream_url,), daemon=True).start()
            self.listen_loop()
            return
        mic = CFG["audio"].get("mic") or None
        if isinstance(mic, str) and mic.isdigit():
            mic = int(mic)
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                            device=mic, callback=self._on_audio):
            self.listen_loop()


if __name__ == "__main__":
    Client().run()
