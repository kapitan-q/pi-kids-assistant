#!/usr/bin/env python3
"""Разовый переезд библиотеки на раскладку Audiobookshelf.

    python3 modules/books/scripts/migrate_to_abs.py [library/сказки] [--dry-run]

Было:  library/сказки/<slug>.mp3 + <slug>.json   (наш формат: title, author, tags-строкой, age_min/max)
Стало: library/<slug>/<slug>.mp3 + metadata.json (формат Audiobookshelf, возраст — тегом «возраст:a-b»)
Файлы переносятся (rename), не копируются. Старая папка удаляется, если опустела.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_library import abs_metadata  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    src = Path(args[0]) if args else ROOT / "library" / "сказки"
    lib = ROOT / "library"
    moved = 0
    for meta_path in sorted(src.glob("*.json")):
        mp3 = meta_path.with_suffix(".mp3")
        if not mp3.exists():
            print(f"!! нет аудио для {meta_path.name}")
            continue
        slug = meta_path.stem
        book = lib / slug
        if book.exists() and any(book.iterdir()):
            print(f"!! {book} уже существует — пропускаю")
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        meta["slug"] = slug
        if not dry:
            book.mkdir(exist_ok=True)
            mp3.rename(book / mp3.name)
            (book / "metadata.json").write_text(json.dumps(abs_metadata(meta), ensure_ascii=False, indent=1), "utf-8")
            meta_path.unlink()
        moved += 1
    print(f"перенесено: {moved}{' (dry-run)' if dry else ''}")
    if not dry and src.exists() and not any(src.iterdir()):
        src.rmdir()
    return 0


if __name__ == "__main__":
    sys.exit(main())
