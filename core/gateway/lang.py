"""Фразы ядра из config/lang/<LANG_CODE>.yaml (перечитываются при изменении файла)."""
import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger("lang")
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
CODE = os.getenv("LANG_CODE", "ru")

_data: dict = {}
_mtime = 0.0


def _load() -> dict:
    global _data, _mtime
    path = CONFIG_DIR / "lang" / f"{CODE}.yaml"
    try:
        mtime = path.stat().st_mtime
        if mtime != _mtime:
            _data, _mtime = yaml.safe_load(path.read_text("utf-8")) or {}, mtime
    except (OSError, yaml.YAMLError):
        log.exception("не читается %s — оставляю прежние фразы", path)
    return _data


def t(key: str, **kw) -> str:
    """t("parent.ok_mode", title=...) — фраза по ключу с подстановками; нет ключа — сам ключ."""
    node = _load()
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, str):
        log.warning("нет фразы %s", key)
        return key
    return node.format(**kw) if kw else node
