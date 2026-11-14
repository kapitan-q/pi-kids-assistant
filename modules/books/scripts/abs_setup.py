#!/usr/bin/env python3
"""Первичная настройка Audiobookshelf для колонки (можно запускать повторно).

Запуск внутри compose-сети (порт ABS наружу не открыт):
    docker compose run --rm --no-deps -v ./modules/books/scripts:/s -e ABS_ROOT_PASSWORD gateway python /s/abs_setup.py

Делает:
  1. создаёт администратора root (ABS_ROOT_PASSWORD), если сервер ещё не инициализирован;
  2. библиотеку «Сказки» на /audiobooks;
  3. пользователя speaker — только чтение и прогресс прослушивания (без правки, загрузки, удаления);
  4. новый API-ключ для speaker;
  5. сканирование библиотеки.
Печатает строки ABS_API_KEY=… и ABS_LIBRARY_ID=… для .env.
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

ABS = os.getenv("ABS_URL", "http://audiobookshelf").rstrip("/")
ROOT_USER = os.getenv("ABS_ROOT_USER", "root")
ROOT_PASSWORD = os.environ["ABS_ROOT_PASSWORD"]
LIBRARY_NAME = os.getenv("ABS_LIBRARY_NAME", "Сказки")
SPEAKER_USER = "speaker"


def call(method: str, path: str, body=None, token: str | None = None):
    req = urllib.request.Request(ABS + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            try:
                return json.loads(raw)
            except ValueError:
                return raw.decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: HTTP {e.code} {e.read().decode()[:200]}")


def main() -> None:
    for _ in range(30):
        try:
            status = call("GET", "/status")
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(2)
    else:
        sys.exit("Audiobookshelf не отвечает")
    if not status.get("isInit"):
        call("POST", "/init", {"newRoot": {"username": ROOT_USER, "password": ROOT_PASSWORD}})
        print(f"# создан администратор {ROOT_USER}", file=sys.stderr)
    token = call("POST", "/login", {"username": ROOT_USER, "password": ROOT_PASSWORD})["user"]["accessToken"]

    libs = call("GET", "/api/libraries", token=token)["libraries"]
    lib = next((lb for lb in libs if lb["name"] == LIBRARY_NAME), None)
    if not lib:
        lib = call("POST", "/api/libraries", {"name": LIBRARY_NAME, "folders": [{"fullPath": "/audiobooks"}],
                                              "mediaType": "book", "provider": "google"}, token)
        print(f"# создана библиотека {LIBRARY_NAME}", file=sys.stderr)

    users = call("GET", "/api/users", token=token)["users"]
    speaker = next((u for u in users if u["username"] == SPEAKER_USER), None)
    if not speaker:
        speaker = call("POST", "/api/users", {
            "username": SPEAKER_USER, "password": secrets.token_urlsafe(24), "type": "user",
            "permissions": {"download": True, "update": False, "delete": False, "upload": False,
                            "accessAllLibraries": True, "accessAllTags": True, "accessExplicitContent": False},
        }, token)
        speaker = speaker.get("user", speaker)
        print(f"# создан пользователь {SPEAKER_USER}", file=sys.stderr)
    # созданный через API пользователь неактивен — его ключи не работают, пока не включить
    call("PATCH", f"/api/users/{speaker['id']}", {"isActive": True}, token)

    key = call("POST", "/api/api-keys", {"name": f"kids-speaker-{time.strftime('%Y%m%d-%H%M')}",
                                         "userId": speaker["id"], "isActive": True}, token)["apiKey"]["apiKey"]
    call("POST", f"/api/libraries/{lib['id']}/scan", token=token)
    print(f"ABS_LIBRARY_ID={lib['id']}")
    print(f"ABS_API_KEY={key}")


if __name__ == "__main__":
    main()
