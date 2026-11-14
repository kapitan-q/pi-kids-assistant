#!/usr/bin/env python3
"""Собрать библиотеку из манифеста: склеить части сказок в один mp3 и положить рядом .json.

    python3 scripts/build_library.py MANIFEST.json INCOMING_DIR [--folder сказки] [--dry-run]

Манифест — список сказок:
    {"slug": "konek-gorbunok", "title": "Конёк-горбунок", "author": "П. П. Ершов",
     "description": "...", "tags": "ершов, в стихах, ...", "age_min": 6, "age_max": 12,
     "paths": ["Пушкин.../7/07_1_1.mp3", ...]}        # пути относительно INCOMING_DIR, по порядку
Одна часть — файл просто копируется; несколько — склеиваются ffmpeg без перекодирования
(-c copy) во временном контейнере alpine, на сервер ничего не ставится.
Результат: library/<folder>/<slug>.mp3 + <slug>.json; затем
    docker compose exec tales python import_library.py --prune
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FFMPEG_IMAGE = "alpine:3.20"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    folder = "сказки"
    if "--folder" in sys.argv:
        folder = sys.argv[sys.argv.index("--folder") + 1]
        args.remove(folder)
    manifest, incoming = Path(args[0]), Path(args[1]).resolve()
    tales = json.loads(manifest.read_text("utf-8-sig"))
    out = ROOT / "library" / folder
    out.mkdir(parents=True, exist_ok=True)

    script, work = [], Path(tempfile.mkdtemp(prefix="ks-build-", dir=ROOT / "library"))
    copied = concat = 0
    for t in tales:
        paths = [incoming / p for p in t["paths"]]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            print(f"!! {t['slug']}: нет файлов {missing}")
            return 1
        meta = {k: t[k] for k in ("title", "author", "description", "tags", "age_min", "age_max") if k in t}
        if not dry:
            (out / f"{t['slug']}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        target = out / f"{t['slug']}.mp3"
        if len(paths) == 1:
            if not dry and not target.exists():
                shutil.copyfile(paths[0], target)
            copied += 1
            continue
        lst = work / f"{t['slug']}.txt"
        # формат concat-списка ffmpeg: апостроф внутри пути экранируется как '\''
        lst.write_text("".join("file '/in/" + p.relative_to(incoming).as_posix().replace("'", "'\\''") + "'\n"
                               for p in paths), "utf-8")
        title = t["title"].replace('"', "")
        script.append(
            f'[ -f "/out/{folder}/{t["slug"]}.mp3" ] || ffmpeg -nostdin -loglevel error -f concat -safe 0 '
            f'-i "/work/{lst.name}" -map 0:a -c copy -map_metadata -1 -id3v2_version 3 '
            f'-metadata title="{title}" -metadata artist="{t.get("author", "")}" '
            f'"/out/{folder}/{t["slug"]}.mp3" && echo "  склеено: {t["slug"]} ({len(paths)} ч.)"')
        concat += 1
    print(f"копируется: {copied}, склеивается: {concat}, папка: library/{folder}")
    if dry or not script:
        shutil.rmtree(work, ignore_errors=True)
        return 0
    (work / "build.sh").write_text("set -e\napk add -q --no-cache ffmpeg\n" + "\n".join(script) + "\n", "utf-8")
    try:
        subprocess.run(["docker", "run", "--rm", "--user", "0",
                        "-v", f"{incoming}:/in:ro", "-v", f"{ROOT / 'library'}:/out",
                        "-v", f"{work}:/work", FFMPEG_IMAGE, "sh", "/work/build.sh"], check=True)
        # файлы созданы root'ом из контейнера — вернуть владельца
        subprocess.run(["docker", "run", "--rm", "-v", f"{out}:/o", FFMPEG_IMAGE,
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/o"], check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
