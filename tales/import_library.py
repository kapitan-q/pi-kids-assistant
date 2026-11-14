"""Сканирует /library и заносит аудиофайлы в базу.

Рядом с файлом можно положить одноимённый .json с метаданными:
    kolobok.mp3
    kolobok.json  -> {"title": "Колобок", "author": "народная", "tags": "животные, короткая",
                      "age_min": 3, "age_max": 8, "description": "..."}
и/или .txt с текстом сказки. Без json название берётся из ID3-тега или имени файла.

Запуск: docker compose exec tales python import_library.py
"""
import json
import os
import sys
from pathlib import Path

from mutagen import File as MutagenFile

import db

LIBRARY = Path(os.getenv("LIBRARY_DIR", "/library"))
AUDIO_EXT = {".mp3", ".ogg", ".opus", ".m4a", ".flac", ".wav"}


def audio_info(path: Path) -> tuple[int | None, str | None]:
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return None, None
    if audio is None:
        return None, None
    duration = int(audio.info.length) if getattr(audio, "info", None) else None
    title = None
    if audio.tags and "title" in audio.tags:
        title = str(audio.tags["title"][0]).strip() or None
    return duration, title


def main() -> int:
    db.init()
    added = 0
    for path in sorted(LIBRARY.rglob("*")):
        if path.suffix.lower() not in AUDIO_EXT or not path.is_file():
            continue
        rel = path.relative_to(LIBRARY).as_posix()
        duration, id3_title = audio_info(path)

        meta = {}
        meta_path = path.with_suffix(".json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        text = None
        txt_path = path.with_suffix(".txt")
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8")

        # папка верхнего уровня — тоже тег («народные», «пушкин»...)
        folder_tag = rel.split("/")[0] if "/" in rel else ""
        tags = meta.get("tags", "")
        if folder_tag and folder_tag.lower() not in tags.lower():
            tags = ", ".join(t for t in (tags, folder_tag) if t)

        title = meta.get("title") or id3_title or path.stem.replace("_", " ")
        tale_id = db.upsert(
            title=title,
            author=meta.get("author", ""),
            description=meta.get("description", ""),
            tags=tags,
            age_min=meta.get("age_min"),
            age_max=meta.get("age_max"),
            duration_sec=duration,
            file=rel,
            text=text,
        )
        added += 1
        print(f"[{tale_id}] {title}  ({rel}, {duration or '?'} с)")
    print(f"Готово: {added} файлов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
