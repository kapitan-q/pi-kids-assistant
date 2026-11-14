"""Подключённые модули (config/modules.yaml) и вызов их служебных MCP-инструментов."""
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("modules")
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
SVC_TIMEOUT = float(os.getenv("MODULE_SVC_TIMEOUT", "5"))

_cfg: dict = {}
_mtime = 0.0


def enabled() -> dict[str, dict]:
    """{имя: {"url", "title", ...}} включённых модулей; файл перечитывается при изменении."""
    global _cfg, _mtime
    path = CONFIG_DIR / "modules.yaml"
    try:
        mtime = path.stat().st_mtime
        if mtime != _mtime:
            data = yaml.safe_load(path.read_text("utf-8")) or {}
            _cfg, _mtime = data.get("modules") or {}, mtime
    except (OSError, yaml.YAMLError):
        log.exception("не читается %s — оставляю прежний список", path)
    return {name: m for name, m in _cfg.items() if m.get("enabled", True)}


def base_url(name: str) -> str | None:
    m = enabled().get(name)
    return m["url"].rstrip("/") if m else None


def titles(names: list[str] | None = None) -> dict[str, str]:
    mods = enabled()
    return {n: m.get("title", n) for n, m in mods.items() if names is None or n in names}


async def call_svc(name: str, tool: str, args: dict) -> dict | None:
    """Служебный инструмент модуля ({url}/svc/mcp). Ошибка/таймаут — None (в лог)."""
    base = base_url(name)
    if not base:
        return None
    try:
        async with streamablehttp_client(f"{base}/svc/mcp", timeout=timedelta(seconds=SVC_TIMEOUT)) as (r, w, _):
            async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=SVC_TIMEOUT)) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
    except Exception as e:  # noqa: BLE001 — модуль не должен ронять ядро
        log.warning("модуль %s: %s не ответил: %r", name, tool, e)
        return None
    if res.isError:
        log.warning("модуль %s: %s ошибка: %s", name, tool, res.content)
        return None
    if isinstance(res.structuredContent, dict):
        return res.structuredContent
    for block in res.content:
        try:
            data = json.loads(getattr(block, "text", "") or "")
            if isinstance(data, dict):
                return data
        except ValueError:
            continue
    return {}
