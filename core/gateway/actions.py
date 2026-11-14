"""Команда плееру Pi — единая схема. Всё, что пришло от агента (и модулей через него), проверяется здесь."""
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("actions")
ID_RE = r"^[\w-]{1,64}$"


class PlayerAction(BaseModel):
    type: Literal["play", "stop", "resume", "seek", "volume"]
    # play
    source: str | None = Field(default=None, pattern=ID_RE)
    item_id: str | None = Field(default=None, pattern=ID_RE)
    title: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=50)
    age_min: int | None = Field(default=None, ge=0, le=99)
    path: str | None = Field(default=None, max_length=300)
    start: float | None = Field(default=None, ge=0)
    # seek
    seconds: int | None = Field(default=None, ge=-36000, le=36000)
    mode: Literal["relative", "absolute"] | None = None
    # volume
    level: int | None = Field(default=None, ge=0, le=100)
    change: int | None = Field(default=None, ge=-100, le=100)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, v):
        if v is not None and (not re.fullmatch(r"[\w][\w./-]*", v) or ".." in v):
            raise ValueError("path должен быть относительным, без ..")
        return v

    @field_validator("tags")
    @classmethod
    def _short_tags(cls, v):
        return [t[:64] for t in v]


def validate(raw: dict | None, modules: dict) -> dict | None:
    """Сырой action → проверенный dict без пустых полей; иначе None (в лог)."""
    if not raw:
        return None
    try:
        act = PlayerAction.model_validate(raw)
    except ValidationError as e:
        log.warning("отброшена команда %s: %s", raw, e.errors()[:3])
        return None
    if act.type == "play" and (act.source not in modules or not act.item_id or not act.path):
        log.warning("отброшен play от неизвестного/неполного модуля: %s", raw)
        return None
    if act.type == "seek" and act.seconds is None:
        return None
    if act.type == "volume" and (act.level is None) == (act.change is None):
        return None
    return act.model_dump(exclude_none=True, exclude_defaults=False)
